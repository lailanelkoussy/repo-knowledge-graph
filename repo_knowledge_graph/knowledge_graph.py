import json
import logging
import os
import subprocess
import tempfile
from typing import Any, Dict, List, Optional

import networkx as nx

from . import queries
from .builder import CONTAINS, MODIFIES, FileAnalysis, GraphBuilder, content_hash, run_sync
from .entity_registry import EntityRegistry
from .git_history import _git_auth_env, _git_clone, _redact_url  # noqa: F401 (re-exported)
from .node import ChunkNode, CommitNode, DirectoryNode, EntityNode, FileNode, Node, ResourceNode
from .serialization import graph_from_dict, graph_to_dict
from .settings import get_settings
from .utils.parsing_utils import DEFAULT_SKIP_PATTERN, read_directory_files_recursively
from .utils.path_utils import prepare_input_path

logger = logging.getLogger(__name__)

MODEL_SERVICE_TYPES = ['openai', 'sentence-transformers']


class RepoKnowledgeGraph:
    """
    Knowledge graph of a code repository: a tree (root -> directories -> files -> chunks)
    enriched with entity nodes ('declares' / 'called_by' edges), 'imports' edges between files,
    'inherits' edges between classes and, optionally, commit nodes ('modifies' edges).

    Build it with `from_path`, `from_repo` or `load_graph_from_file`.
    """

    def __init__(self, graph: Optional[nx.DiGraph] = None, entities: Optional[Dict[str, dict]] = None,
                 model_service=None, code_index=None, model_service_kwargs: Optional[dict] = None,
                 repo_path: Optional[str] = None, metadata: Optional[dict] = None):
        self.graph: nx.DiGraph = graph if graph is not None else nx.DiGraph()
        self.entities: Dict[str, dict] = entities if entities is not None else {}
        self.code_index = code_index
        self.repo_path = repo_path
        self.metadata: Dict[str, Any] = dict(metadata or {})
        self._model_service = model_service
        self._model_service_kwargs = dict(model_service_kwargs or {})
        self.logger = logger

    # ------------------------------------------------------------------ helpers

    @property
    def model_service(self):
        """Created on first use, so that no embedding model is loaded when it is not needed."""
        if self._model_service is None:
            from .model_service import create_model_service
            self._model_service = create_model_service(**dict(self._model_service_kwargs))
        return self._model_service

    @model_service.setter
    def model_service(self, value):
        self._model_service = value

    @property
    def knowledge_graph(self) -> nx.DiGraph:  # backward compatibility
        return self.graph

    def __iter__(self):
        # Yield only the 'data' attribute from each node
        return (node_data['data'] for _, node_data in self.graph.nodes(data=True))

    def __getitem__(self, node_id):
        return self.graph.nodes[node_id]['data']

    def __contains__(self, node_id) -> bool:
        return node_id in self.graph

    # ------------------------------------------------------------------ construction

    @classmethod
    def from_path(cls, path: str, skip_dirs: Optional[list] = None, index_nodes: bool = True,
                  describe_nodes: bool = False, extract_entities: bool = False,
                  model_service_kwargs: Optional[dict] = None, code_index_kwargs: Optional[dict] = None,
                  include_git_history: bool = False, max_commits: int = 200,
                  skip_pattern: Optional[str] = DEFAULT_SKIP_PATTERN, extensions: Optional[List[str]] = None,
                  max_file_size: Optional[int] = None, respect_gitignore: bool = True,
                  show_progress: bool = False, model_service=None,
                  enrichers: Optional[str] = None) -> 'RepoKnowledgeGraph':
        """
        Build a RepoKnowledgeGraph from a directory, a single file, or a .zip / .tar.gz archive.

        Args:
            path: Repository root (or archive / file).
            skip_dirs: Directory names to skip.
            index_nodes: Build a code index (search) over the chunks.
            describe_nodes: Generate an LLM description for every chunk.
            extract_entities: Extract code entities and build 'declares' / 'called_by' relations.
            model_service_kwargs: Arguments for `create_model_service` (embedder_type, embed_model_name...).
            code_index_kwargs: Arguments for `CodeIndex` (backend, index_type, db_path...).
            include_git_history: Add commit nodes when `path` is a git repository.
            max_commits: Number of commits read when include_git_history is True.
            skip_pattern / extensions / max_file_size / respect_gitignore: file selection.
            show_progress: Display tqdm progress bars.
            model_service: An already-built model service (takes precedence over model_service_kwargs).
            enrichers: Framework enrichers: 'auto' (default, ENRICHERS setting), 'none' or a comma-separated
                list (build, supply_chain, spring_config, spring_beans, openapi, spring_web, web_routes, graphql,
                spring_data_jpa, mybatis, spring_messaging, spring_tests, spring_security, security). See enrichers/.
        """
        options = dict(skip_dirs=skip_dirs, index_nodes=index_nodes, describe_nodes=describe_nodes,
                       extract_entities=extract_entities, model_service_kwargs=model_service_kwargs,
                       code_index_kwargs=code_index_kwargs, include_git_history=include_git_history,
                       max_commits=max_commits, skip_pattern=skip_pattern, extensions=extensions,
                       max_file_size=max_file_size, respect_gitignore=respect_gitignore,
                       show_progress=show_progress, model_service=model_service, enrichers=enrichers)
        return run_sync(lambda: cls.from_path_async(path, **options))

    @classmethod
    async def from_path_async(cls, path: str, skip_dirs: Optional[list] = None, index_nodes: bool = True,
                              describe_nodes: bool = False, extract_entities: bool = False,
                              model_service_kwargs: Optional[dict] = None,
                              code_index_kwargs: Optional[dict] = None,
                              include_git_history: bool = False, max_commits: int = 200,
                              skip_pattern: Optional[str] = DEFAULT_SKIP_PATTERN,
                              extensions: Optional[List[str]] = None, max_file_size: Optional[int] = None,
                              respect_gitignore: bool = True, show_progress: bool = False,
                              model_service=None, enrichers: Optional[str] = None) -> 'RepoKnowledgeGraph':
        """Async variant of `from_path` (use it from notebooks or async applications)."""
        instance = cls(model_service=model_service, model_service_kwargs=model_service_kwargs)
        prepared_path = prepare_input_path(path)
        instance.logger.info(f'Building knowledge graph from {prepared_path}')

        build_options = {
            'skip_dirs': list(skip_dirs or []),
            'skip_pattern': skip_pattern,
            'extensions': list(extensions) if extensions else None,
            'max_file_size': max_file_size if max_file_size is not None else get_settings().max_file_size,
            'respect_gitignore': respect_gitignore,
            'extract_entities': extract_entities,
            'describe_nodes': describe_nodes,
            'enrichers': enrichers if enrichers is not None else get_settings().enrichers,
        }
        files = instance._read_files(prepared_path, build_options)
        instance.logger.info(f'{len(files)} files to process')

        builder = GraphBuilder(enrichers=build_options['enrichers'])
        analyses = builder.analyze_files(files, extract_entities=extract_entities,
                                         root_path=prepared_path, show_progress=show_progress)
        if describe_nodes:
            chunks = [c for a in analyses.values() for c in a.chunks]
            await builder.describe_chunks(chunks, instance.model_service, get_settings().llm_concurrency)

        instance.graph, instance.entities = builder.assemble_graph(analyses)
        instance.repo_path = os.path.abspath(prepared_path)
        instance.metadata.update({'build_options': build_options,
                                  'enrichers': list(instance.graph.graph.get('enrichers', []))})

        if include_git_history:
            instance.add_git_history(max_commits=max_commits)

        if index_nodes:
            instance.build_code_index(**(code_index_kwargs or {}))
        instance.metadata['code_index_kwargs'] = dict(code_index_kwargs or {})
        instance.logger.info('Knowledge graph construction completed')
        return instance

    @staticmethod
    def _read_files(path: str, build_options: dict) -> Dict[str, str]:
        return read_directory_files_recursively(
            path,
            skip_dirs=build_options['skip_dirs'],
            skip_pattern=build_options['skip_pattern'],
            max_file_size=build_options['max_file_size'],
            extensions=build_options['extensions'],
            respect_gitignore=build_options['respect_gitignore'],
        )

    @classmethod
    def from_repo(cls, repo_url: str, skip_dirs: Optional[list] = None, index_nodes: bool = True,
                  describe_nodes: bool = False, extract_entities: bool = False,
                  model_service_kwargs: Optional[dict] = None, code_index_kwargs: Optional[dict] = None,
                  github_token: Optional[str] = None, allow_unauthenticated_clone: bool = True,
                  clone_dir: Optional[str] = None, **from_path_kwargs) -> 'RepoKnowledgeGraph':
        """
        Build a RepoKnowledgeGraph from a remote git repository URL (SSH or HTTPS).

        Args:
            github_token: Token for private repositories (defaults to GITHUB_TOKEN / GITHUB_OAUTH_TOKEN).
            allow_unauthenticated_clone: If False, raise when no token is available.
            clone_dir: Keep the clone in this (new or empty) directory instead of a temporary one.
                Needed for git history tools that read the repository after construction.
            Other arguments: see `from_path`.
        """
        token = github_token or get_settings().github_token
        safe_url = _redact_url(repo_url)
        if not token and not allow_unauthenticated_clone:
            raise ValueError(
                "GitHub token not provided and unauthenticated clone is disabled. "
                "Set allow_unauthenticated_clone=True or provide a token."
            )

        clone_url = repo_url
        if repo_url.startswith('git@'):
            # Convert git@github.com:owner/repo.git -> https://github.com/owner/repo.git
            clone_url = 'https://' + repo_url[len('git@'):].replace(':', '/', 1)

        def build(target_dir: str) -> 'RepoKnowledgeGraph':
            logger.info(f'Cloning {safe_url}')
            try:
                _git_clone(clone_url, target_dir, token=token)
            except subprocess.CalledProcessError as e:
                # Do not log `e` itself: its message embeds the full command line.
                logger.error(f'Failed to clone repository {safe_url}: git exited with code {e.returncode}')
                raise RuntimeError(f'git clone failed for {safe_url} (exit code {e.returncode})') from None
            kg = cls.from_path(target_dir, skip_dirs=skip_dirs, index_nodes=index_nodes,
                               describe_nodes=describe_nodes, extract_entities=extract_entities,
                               model_service_kwargs=model_service_kwargs, code_index_kwargs=code_index_kwargs,
                               **from_path_kwargs)
            kg.metadata['repo_url'] = safe_url
            return kg

        if clone_dir:
            kg = build(clone_dir)
            kg.repo_path = os.path.abspath(clone_dir)
            return kg
        with tempfile.TemporaryDirectory() as tmpdirname:
            kg = build(tmpdirname)
            kg.repo_path = None  # the clone is deleted
            return kg

    # ------------------------------------------------------------------ incremental update

    def update_from_path(self, path: Optional[str] = None, rebuild_index: bool = True) -> Dict[str, List[str]]:
        """
        Re-synchronise the graph with the files on disk, re-analysing only added or modified files
        (content hash). Descriptions and embeddings of unchanged chunks are kept.

        Returns:
            {'added': [...], 'modified': [...], 'deleted': [...], 'unchanged': [...]}
        """
        return run_sync(lambda: self.update_from_path_async(path, rebuild_index=rebuild_index))

    async def update_from_path_async(self, path: Optional[str] = None,
                                     rebuild_index: bool = True) -> Dict[str, List[str]]:
        path = path or self.repo_path
        if not path:
            raise ValueError('No path given and the graph does not know its repository path')
        if not os.path.isdir(path):
            # Never treat a missing directory (e.g. a deleted temporary clone) as "every file was deleted"
            raise ValueError(f'Repository directory not found: {path}')
        build_options = dict(self.metadata.get('build_options') or {})
        build_options.setdefault('skip_dirs', [])
        build_options.setdefault('skip_pattern', DEFAULT_SKIP_PATTERN)
        build_options.setdefault('extensions', None)
        build_options.setdefault('max_file_size', get_settings().max_file_size)
        build_options.setdefault('respect_gitignore', True)
        build_options.setdefault('extract_entities', True)
        build_options.setdefault('describe_nodes', False)
        build_options.setdefault('enrichers', get_settings().enrichers)

        files = self._read_files(path, build_options)
        existing = {n.path: n for n in self.get_all_files()}

        summary = {'added': [], 'modified': [], 'deleted': [], 'unchanged': []}
        builder = GraphBuilder(enrichers=build_options['enrichers'])
        analyses: Dict[str, FileAnalysis] = {}
        new_chunks: List[ChunkNode] = []
        for file_path in sorted(files):
            content = files[file_path]
            old = existing.get(file_path)
            if old is not None and (old.content_hash or content_hash(old.content)) == content_hash(content):
                old.content_hash = old.content_hash or content_hash(old.content)
                analyses[file_path] = FileAnalysis(file_node=old, chunks=self.get_chunks_of_file(file_path))
                summary['unchanged'].append(file_path)
                continue
            analysis = builder.analyze_file(file_path, content, build_options['extract_entities'], path)
            analyses[file_path] = analysis
            new_chunks.extend(analysis.chunks)
            summary['modified' if old is not None else 'added'].append(file_path)
        summary['deleted'] = sorted(set(existing) - set(files))

        if not (summary['added'] or summary['modified'] or summary['deleted']):
            return summary

        if build_options['describe_nodes'] and new_chunks:
            await builder.describe_chunks(new_chunks, self.model_service, get_settings().llm_concurrency)

        commits = [n for n in self if isinstance(n, CommitNode)]
        self.graph, self.entities = builder.assemble_graph(analyses)
        self.metadata['enrichers'] = list(self.graph.graph.get('enrichers', []))
        if commits:
            self._add_commit_nodes(commits)

        if rebuild_index and self.code_index is not None:
            self.close_code_index()
            self.build_code_index(**(self.metadata.get('code_index_kwargs') or {}))
        self.logger.info(f"Incremental update: {', '.join(f'{k}={len(v)}' for k, v in summary.items())}")
        return summary

    # ------------------------------------------------------------------ git history

    def add_git_history(self, max_commits: int = 200, repo_path: Optional[str] = None) -> int:
        """Add commit nodes ('modifies' edges to files). Returns the number of commits added."""
        from .git_history import collect_commits, current_revision, is_git_repository
        repo_path = repo_path or self.repo_path
        if not is_git_repository(repo_path):
            self.logger.warning(f'{repo_path} is not a git repository: no history added')
            return 0
        known_files = {n.path for n in self.get_all_files()}
        commits = collect_commits(repo_path, max_commits=max_commits, known_files=known_files)
        self._add_commit_nodes(commits)
        self.metadata['git_revision'] = current_revision(repo_path)
        return len(commits)

    def _add_commit_nodes(self, commits: List[CommitNode]) -> None:
        for commit in commits:
            self.graph.add_node(commit.id, data=commit, level=4)
            for file_path in commit.files_changed:
                if file_path in self.graph:
                    self.graph.add_edge(commit.id, file_path, relation=MODIFIES)

    def get_file_history(self, file_path: str) -> List[CommitNode]:
        """Commits that modified a file, most recent first."""
        commits = [self[u] for u, _, d in self.graph.in_edges(file_path, data=True)
                   if d.get('relation') == MODIFIES]
        return sorted(commits, key=lambda c: c.date, reverse=True)

    # ------------------------------------------------------------------ code index

    def build_code_index(self, **code_index_kwargs):
        from .code_index import CodeIndex
        self.code_index = CodeIndex(self.get_all_chunks(), model_service=self._index_model_service(code_index_kwargs),
                                    **code_index_kwargs)
        self.metadata['code_index_kwargs'] = dict(code_index_kwargs)
        return self.code_index

    def _index_model_service(self, code_index_kwargs: dict):
        # A keyword-only index never needs an embedding model: avoid loading one
        if code_index_kwargs.get('index_type') == 'keyword-only' and self._model_service is None:
            return None
        return self.model_service

    def close_code_index(self) -> None:
        if self.code_index is not None and hasattr(self.code_index, 'close'):
            self.code_index.close()
        self.code_index = None

    # ------------------------------------------------------------------ entities

    def get_entity_by_alias(self, alias: str) -> Optional[str]:
        """Canonical entity name for an alias (e.g. 'MyClass' or 'module.MyClass')."""
        return EntityRegistry(self.entities).lookup(alias)

    def resolve_entity(self, name: str) -> Optional[str]:
        """Canonical entity for an exact name, an alias or an unambiguous simple name."""
        if name in self.entities:
            return name
        return EntityRegistry(self.entities).resolve(name)

    def resolve_entity_references(self) -> Dict[str, str]:
        """Map called-but-undeclared entity names to the declared entity they resolve to."""
        registry = EntityRegistry(self.entities)
        resolutions = {}
        for entity_name, info in self.entities.items():
            if not info.get('declaring_chunk_ids') and info.get('calling_chunk_ids'):
                registry.alias_map.pop(entity_name, None)  # do not resolve the name to itself
                resolved = registry.resolve(entity_name)
                registry.alias_map[entity_name] = entity_name
                if resolved and resolved != entity_name:
                    resolutions[entity_name] = resolved
        return resolutions

    # ------------------------------------------------------------------ serialization

    def to_dict(self) -> dict:
        return graph_to_dict(self.graph, self.entities, metadata={
            **self.metadata,
            'repo_path': self.repo_path,
        })

    @classmethod
    def from_dict(cls, data_dict: dict, index_nodes: bool = True, use_embed: bool = True,
                  model_service_kwargs: Optional[dict] = None, code_index_kwargs: Optional[dict] = None,
                  model_service=None) -> 'RepoKnowledgeGraph':
        graph, entities, metadata = graph_from_dict(data_dict)
        instance = cls(graph=graph, entities=entities, model_service=model_service,
                       model_service_kwargs=model_service_kwargs, metadata=metadata,
                       repo_path=metadata.get('repo_path'))
        if index_nodes:
            kwargs = dict(code_index_kwargs or {})
            kwargs.setdefault('use_embed', use_embed)
            instance.build_code_index(**kwargs)
        return instance

    def save_graph_to_file(self, filepath: str) -> None:
        self.logger.info(f'Saving graph to file: {filepath}')
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)

    @classmethod
    def load_graph_from_file(cls, filepath: str, index_nodes: bool = True, use_embed: bool = True,
                             model_service_kwargs: Optional[dict] = None,
                             code_index_kwargs: Optional[dict] = None, model_service=None) -> 'RepoKnowledgeGraph':
        with open(filepath, encoding='utf-8') as f:
            data = json.load(f)
        logger.info(f'Loaded graph data from file: {filepath}')
        return cls.from_dict(data, use_embed=use_embed, index_nodes=index_nodes,
                             model_service_kwargs=model_service_kwargs, code_index_kwargs=code_index_kwargs,
                             model_service=model_service)

    # ------------------------------------------------------------------ navigation

    def print_tree(self, max_depth=None, start_node_id='root', level=0, prefix=""):
        """Print the repository tree ('contains' edges)."""
        queries.print_tree(self, max_depth, start_node_id, level, prefix)

    def get_neighbors(self, node_id) -> List[Node]:
        """Nodes directly connected to node_id, in either direction and for any relation."""
        neighbors = set(self.graph.successors(node_id)) | set(self.graph.predecessors(node_id))
        return [self.graph.nodes[n]['data'] for n in neighbors if 'data' in self.graph.nodes[n]]

    def get_previous_chunk(self, node_id: str) -> Optional[ChunkNode]:
        node = self[node_id]
        if not isinstance(node, ChunkNode):
            raise ValueError(f'Cannot get previous chunk on node of type {type(node).__name__}')
        if node.order_in_file == 0:
            return None
        previous_chunk_id = f'{node.path}_{node.order_in_file - 1}'
        if previous_chunk_id not in self.graph:
            raise KeyError(f'Previous chunk {previous_chunk_id} not found in graph')
        return self[previous_chunk_id]

    def get_next_chunk(self, node_id: str) -> Optional[ChunkNode]:
        node = self[node_id]
        if not isinstance(node, ChunkNode):
            raise ValueError(f'Cannot get next chunk on node of type {type(node).__name__}')
        next_chunk_id = f'{node.path}_{node.order_in_file + 1}'
        if next_chunk_id not in self.graph:
            return None
        return self[next_chunk_id]

    def get_all_chunks(self) -> List[ChunkNode]:
        return [node for node in self if isinstance(node, ChunkNode)]

    def get_all_files(self) -> List[FileNode]:
        # Exact FileNode type check: ChunkNode inherits from FileNode
        return [node for node in self if isinstance(node, FileNode) and node.node_type == 'file']

    def get_chunks_of_file(self, file_node_id: str) -> List[ChunkNode]:
        chunks = []
        for node_id in self.graph.successors(file_node_id):
            node = self.graph.nodes[node_id]['data']
            if isinstance(node, ChunkNode) and self.graph.edges[file_node_id, node_id].get('relation') == CONTAINS:
                chunks.append(node)
        return sorted(chunks, key=lambda c: c.order_in_file)

    def find_path(self, source_id: str, target_id: str, max_depth: int = 5) -> dict:
        """Shortest (directed) path between two nodes."""
        return queries.find_path(self, source_id, target_id, max_depth)

    def get_subgraph(self, node_id: str, depth: int = 2, edge_types: Optional[List[str]] = None) -> dict:
        """Nodes and edges within `depth` hops of node_id (both directions), optionally filtered by relation."""
        return queries.get_subgraph(self, node_id, depth, edge_types)

    # ------------------------------------------------------------------ framework knowledge

    def get_resources(self, node_type: str) -> List[ResourceNode]:
        """Resource nodes of a type: endpoint, config_property, table, module, dependency, channel."""
        return [n for n in self if isinstance(n, ResourceNode) and n.node_type == node_type]

    def get_endpoints(self, http_method: Optional[str] = None, path_prefix: Optional[str] = None,
                      include_external: bool = False) -> List[ResourceNode]:
        """Endpoint nodes served by the repository (plus outgoing ones with include_external)."""
        from . import framework_queries
        return framework_queries.list_endpoints(self, http_method, path_prefix, include_external)

    def trace_flow(self, start: str, max_depth: int = 6, limit: int = 60) -> dict:
        """Execution flow from an endpoint, a method or a channel (see framework_queries.trace_flow)."""
        from . import framework_queries
        return framework_queries.trace_flow(self, start, max_depth=max_depth, limit=limit)

    def get_project_overview(self) -> dict:
        """Languages, modules, Java / Spring Boot versions, counts of controllers, endpoints, tables..."""
        from . import framework_queries
        return framework_queries.project_overview(self)


__all__ = ['RepoKnowledgeGraph', 'Node', 'DirectoryNode', 'FileNode', 'ChunkNode', 'EntityNode', 'CommitNode',
           'ResourceNode']
