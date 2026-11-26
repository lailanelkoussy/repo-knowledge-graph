"""
Graph construction pipeline.

1. `analyze_file`  : split one file into chunks, extract and map its entities (pure, per file).
2. `describe_chunks`: optional LLM summaries, bounded concurrency over the whole repository.
3. `assemble_graph`: build the networkx graph (root / directories / files / chunks / entities)
   and the entity registry from the analysed files.

Because `assemble_graph` only needs file and chunk nodes, an incremental update can keep the
analysis of unchanged files and re-assemble the graph cheaply (see RepoKnowledgeGraph.update_from_path).
"""
import asyncio
import hashlib
import logging
import posixpath
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import networkx as nx

from .code_parser import CodeParser
from .entity_chunk_mapper import EntityChunkMapper
from .entity_extractor import HybridEntityExtractor
from .entity_registry import EntityRegistry
from .languages import get_extension, get_language_name, get_language_spec
from .node import ChunkNode, DirectoryNode, EntityNode, FileNode, Node

logger = logging.getLogger(__name__)

ROOT_ID = 'root'

# Relations used in the graph
CONTAINS = 'contains'        # root/directory -> directory/file, file -> chunk
DECLARES = 'declares'        # chunk -> entity
CALLED_BY = 'called_by'      # entity -> chunk using it
IMPORTS = 'imports'          # file -> file
INHERITS = 'inherits'        # entity (class) -> entity (base class / interface)
MODIFIES = 'modifies'        # commit -> file (git history)
CALLS = 'calls'              # entity (method) -> entity (method), type-resolved call
USES_TYPE = 'uses_type'      # entity -> entity (class): parameter / return / field / generic type
IMPLEMENTED_BY = 'implemented_by'  # entity (interface) -> entity (concrete class implementing it)


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode('utf-8', errors='replace')).hexdigest()


def chunk_id_for(file_path: str, index: int) -> str:
    """Chunk ids keep the historical '<file path>_<index>' format; the file path is stored on the node."""
    return f'{file_path}_{index}'


def merge_declared_entities(target: List[dict], source: Iterable[dict]) -> None:
    """Merge entity dicts, avoiding duplicates by (name, type)."""
    existing = {(e.get('name'), e.get('type')) for e in target}
    for e in source:
        key = (e.get('name'), e.get('type'))
        if key not in existing:
            target.append(e)
            existing.add(key)


def merge_called_entities(target: List[str], source: Iterable[str]) -> None:
    """Merge called-entity names, avoiding duplicates while preserving order."""
    existing = set(target)
    for e in source:
        if e not in existing:
            target.append(e)
            existing.add(e)


def chunk_line_ranges(content: str, chunks: List[str]) -> List[Tuple[int, int]]:
    """1-based (start_line, end_line) of each chunk in the file ((0, 0) when a chunk cannot be located)."""
    ranges = []
    position = 0
    for text in chunks:
        index = content.find(text, position)
        if index < 0:
            index = content.find(text)
        if index < 0:
            stripped = text.strip()
            index = content.find(stripped, position) if stripped else -1
            text = stripped
        if index < 0:
            ranges.append((0, 0))
            continue
        start = content.count('\n', 0, index) + 1
        ranges.append((start, start + text.count('\n')))
        position = index + 1
    return ranges


def chunk_for_line(ranges: List[Tuple[int, int]], line: int) -> Optional[int]:
    for i, (start, end) in enumerate(ranges):
        if start and start <= line <= end:
            return i
    return None


def map_entities_by_line(declared: List[dict], call_sites: List[Tuple[str, int]], ranges: List[Tuple[int, int]]
                         ) -> Optional[Tuple[Dict[int, List[dict]], Dict[int, List[str]]]]:
    """Exact entity -> chunk mapping from source positions; None when positions are missing."""
    if not ranges or not declared and not call_sites:
        return None
    if not all(r[0] for r in ranges) or not all(e.get('start_line') for e in declared):
        return None
    chunk_declared: Dict[int, List[dict]] = {i: [] for i in range(len(ranges))}
    chunk_called: Dict[int, List[str]] = {i: [] for i in range(len(ranges))}
    for entity in declared:
        index = chunk_for_line(ranges, int(entity['start_line']))
        if index is not None:
            chunk_declared[index].append(entity)
    for name, line in call_sites:
        index = chunk_for_line(ranges, line)
        if index is not None and name not in chunk_called[index]:
            chunk_called[index].append(name)
    return chunk_declared, chunk_called


@dataclass
class FileAnalysis:
    file_node: FileNode
    chunks: List[ChunkNode] = field(default_factory=list)


class GraphBuilder:
    def __init__(self, code_parser: Optional[CodeParser] = None,
                 entity_extractor: Optional[HybridEntityExtractor] = None,
                 entity_mapper: Optional[EntityChunkMapper] = None,
                 enrichers: Optional[str] = None, mask_config_values: Optional[bool] = None,
                 max_call_edges: Optional[int] = None):
        """
        enrichers: 'auto' (default, from the ENRICHERS setting), 'none' or a comma-separated list of
            enricher names (see enrichers/__init__.py).
        mask_config_values: mask secret-looking configuration values (MASK_CONFIG_VALUES).
        max_call_edges: cap on 'calls' / 'uses_type' edges, 0 = unlimited (MAX_CALL_EDGES).
        """
        from .settings import get_settings
        settings = get_settings()
        self.code_parser = code_parser or CodeParser()
        self.entity_extractor = entity_extractor or HybridEntityExtractor()
        self.entity_mapper = entity_mapper or EntityChunkMapper()
        self.enrichers = enrichers if enrichers is not None else settings.enrichers
        self.mask_config_values = settings.mask_config_values if mask_config_values is None else mask_config_values
        self.max_call_edges = settings.max_call_edges if max_call_edges is None else max_call_edges

    # ------------------------------------------------------------------ 1. per file

    def analyze_file(self, file_path: str, content: str, extract_entities: bool = True,
                     root_path: Optional[str] = None) -> FileAnalysis:
        language = get_language_name(file_path)
        chunks_text = self.code_parser.parse(file_name=file_path, file_content=content)
        ranges = chunk_line_ranges(content, chunks_text)

        declared, called, imports = [], [], []
        chunk_declared = {i: [] for i in range(len(chunks_text))}
        chunk_called = {i: [] for i in range(len(chunks_text))}
        if extract_entities and self.entity_extractor.supports(file_path):
            try:
                # Full path is needed for C/C++ include resolution
                extraction_path = posixpath.join(root_path.replace('\\', '/'), file_path) if root_path else file_path
                declared, called, imports = self.entity_extractor.extract_file_info(
                    content, extraction_path, include_dirs=[root_path] if root_path else None)
                if root_path:
                    # Aliases must be computed from the repository-relative path
                    from .utils.path_utils import generate_entity_aliases, with_extra_aliases
                    for entity in declared:
                        if entity.get('name'):
                            entity['aliases'] = with_extra_aliases(
                                generate_entity_aliases(entity['name'], file_path), entity.get('extra_aliases'),
                                entity.get('exclude_aliases'))
                for entity in declared:
                    if entity.get('start_line'):
                        entity['file_path'] = file_path
                by_line = map_entities_by_line(declared, self.entity_extractor.last_call_sites, ranges)
                if by_line is not None:
                    chunk_declared, chunk_called = by_line
                else:
                    chunk_declared, chunk_called = self.entity_mapper.map_entities_to_chunks(
                        declared, called, chunks_text, file_name=file_path)
            except Exception as e:
                logger.error(f'Error extracting entities from {file_path}: {e}', exc_info=True)
                declared, called, imports = [], [], []
                chunk_declared = {i: [] for i in range(len(chunks_text))}
                chunk_called = {i: [] for i in range(len(chunks_text))}

        chunks = [
            ChunkNode(
                id=chunk_id_for(file_path, i),
                name=chunk_id_for(file_path, i),
                path=file_path,
                content=text,
                order_in_file=i,
                declared_entities=list(chunk_declared.get(i, [])),
                called_entities=list(chunk_called.get(i, [])),
                language=language,
                description='',
                start_line=ranges[i][0],
                end_line=ranges[i][1],
            )
            for i, text in enumerate(chunks_text)
        ]
        stored_content, file_attributes = content, {}
        if self.mask_config_values:
            stored_content, file_attributes = self._mask_secrets(content, declared, chunks)
        file_node = FileNode(
            id=file_path,
            name=posixpath.basename(file_path),
            path=file_path,
            content=stored_content,
            declared_entities=list(declared),
            called_entities=list(called),
            language=language,
            content_hash=content_hash(content),  # hash of the file on disk (incremental updates)
            imports=list(imports),
            attributes=file_attributes,
        )
        return FileAnalysis(file_node=file_node, chunks=chunks)

    @staticmethod
    def _mask_secrets(content: str, declared: List[dict], chunks: List[ChunkNode]) -> Tuple[str, dict]:
        """Mask secrets hard-coded in the file, its chunks and its declarations (see secrets_masking.py)."""
        from .secrets_masking import MASK, looks_secret, mask_code, scrub, secret_values
        masked, findings = mask_code(content)
        values = secret_values(content)
        for entity in declared:  # constants whose name designates a secret
            attributes = entity.get('attributes') or {}
            value = attributes.get('constant_value')
            name = str(entity.get('name', '')).rsplit('::', 1)[-1]
            if isinstance(value, str) and (value in values or looks_secret(name, value)):
                values.add(value)
                attributes['hardcoded_secret'] = True
                if not any(f.get('line') == entity.get('start_line') for f in findings):
                    findings.append({'line': entity.get('start_line'), 'kind': 'hardcoded_secret', 'name': name})
        if not findings and not values:
            return content, {}
        for i, entity in enumerate(declared):
            declared[i].update(scrub({k: v for k, v in entity.items() if k not in ('name', 'aliases')}, values))
        for chunk in chunks:
            chunk.content = mask_code(scrub(chunk.content, values))[0]
            for entity in chunk.declared_entities:
                if entity.get('attributes', {}).get('constant_value') in values:
                    entity['attributes']['constant_value'] = MASK
        masked = scrub(masked, values)
        return masked, {'hardcoded_secrets': sorted(findings, key=lambda f: f.get('line') or 0)}

    def analyze_files(self, files: Dict[str, str], extract_entities: bool = True,
                      root_path: Optional[str] = None, show_progress: bool = False) -> Dict[str, FileAnalysis]:
        paths = sorted(files)
        iterator = paths
        if show_progress:
            from tqdm import tqdm
            iterator = tqdm(paths, desc='Analysing files')
        analyses = {}
        for path in iterator:
            logger.debug(f'Analysing {path}')
            analyses[path] = self.analyze_file(path, files[path], extract_entities, root_path)
        return analyses

    # ------------------------------------------------------------------ 2. descriptions

    @staticmethod
    async def describe_chunks(chunks: List[ChunkNode], model_service, concurrency: int = 8) -> None:
        """Generate an LLM summary for each chunk without a description (bounded concurrency)."""
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def describe(chunk: ChunkNode):
            async with semaphore:
                try:
                    chunk.description = await model_service.query_async(
                        f'Summarize this {chunk.language or "code"} code chunk in a few sentences: {chunk.content}')
                except Exception as e:
                    logger.error(f'Error generating description for chunk {chunk.id}: {e}')
                    chunk.description = ''

        await asyncio.gather(*(describe(c) for c in chunks if not c.description))

    # ------------------------------------------------------------------ 3. assembly

    def assemble_graph(self, analyses: Dict[str, FileAnalysis]) -> Tuple[nx.DiGraph, Dict[str, dict]]:
        graph = nx.DiGraph()
        graph.add_node(ROOT_ID, data=Node(id=ROOT_ID, name=ROOT_ID, node_type='root'), level=0)

        dir_agg: Dict[str, dict] = {}
        for path in sorted(analyses):
            analysis = analyses[path]
            file_node = analysis.file_node
            graph.add_node(path, data=file_node, level=1)
            for chunk in sorted(analysis.chunks, key=lambda c: c.order_in_file):
                graph.add_node(chunk.id, data=chunk, level=2)
                graph.add_edge(path, chunk.id, relation=CONTAINS)

            parent = ROOT_ID
            accumulated = ''
            for part in path.split('/')[:-1]:
                accumulated = f'{accumulated}/{part}' if accumulated else part
                if accumulated not in graph:
                    graph.add_node(accumulated, data=DirectoryNode(id=accumulated, name=part, path=accumulated), level=1)
                    graph.add_edge(parent, accumulated, relation=CONTAINS)
                agg = dir_agg.setdefault(accumulated, {'declared': [], 'called': []})
                merge_declared_entities(agg['declared'], file_node.declared_entities)
                merge_called_entities(agg['called'], file_node.called_entities)
                parent = accumulated
            graph.add_edge(parent, path, relation=CONTAINS)

        root = graph.nodes[ROOT_ID]['data']
        for dir_path, agg in dir_agg.items():
            node = graph.nodes[dir_path]['data']
            merge_declared_entities(node.declared_entities, agg['declared'])
            merge_called_entities(node.called_entities, agg['called'])
        for analysis in analyses.values():
            merge_declared_entities(root.declared_entities, analysis.file_node.declared_entities)
            merge_called_entities(root.called_entities, analysis.file_node.called_entities)

        registry = self.build_registry(analyses)
        from .call_resolution import resolve_chained_invocations
        resolve_chained_invocations(analyses, registry, self._import_prefixes)
        self.add_entity_nodes(graph, registry.entities)
        self.add_inheritance_edges(graph, registry, analyses)
        self.add_implementation_edges(graph, registry)
        self.add_import_edges(graph, analyses)
        self.add_call_edges(graph, registry, analyses)
        from .enrichers import run_enrichers
        run_enrichers(graph, registry, analyses, self.enrichers, mask_config_values=self.mask_config_values)
        return graph, registry.entities

    def build_registry(self, analyses: Dict[str, FileAnalysis]) -> EntityRegistry:
        registry = EntityRegistry()
        ordered = [analyses[p] for p in sorted(analyses)]
        # First pass: every declaration, so that calls can be resolved against the whole repository
        for analysis in ordered:
            for chunk in sorted(analysis.chunks, key=lambda c: c.order_in_file):
                for entity in chunk.declared_entities:
                    registry.register_declared(entity, chunk.id)
        # Second pass: calls, resolved with aliases and the file imports
        for analysis in ordered:
            imports = self._import_prefixes(analysis.file_node)
            for chunk in sorted(analysis.chunks, key=lambda c: c.order_in_file):
                for called_name in chunk.called_entities:
                    registry.register_called(called_name, chunk.id, imports)
        return registry

    @staticmethod
    def _import_prefixes(file_node: FileNode) -> List[str]:
        """Absolute module prefixes usable to qualify called names (relative imports resolved)."""
        prefixes = []
        package = file_node.path.split('/')[:-1]
        is_java = get_extension(file_node.path) == 'java'
        if is_java:
            # Java: the types of the file's own package are visible without import
            java_package = next((e['attributes']['package'] for e in file_node.declared_entities
                                 if isinstance(e, dict) and (e.get('attributes') or {}).get('package')), None)
            if java_package:
                prefixes.append(java_package)
        for imp in getattr(file_node, 'imports', []) or []:
            if is_java and imp.endswith('.*'):
                prefixes.append(imp[:-2])
                continue
            if imp.startswith('.') and get_extension(file_node.path) in ('py', 'pyw'):
                level = len(imp) - len(imp.lstrip('.'))
                base = package[:len(package) - (level - 1)] if level - 1 <= len(package) else []
                rest = imp.lstrip('.')
                prefixes.append('.'.join(base + ([rest] if rest else [])))
            elif not imp.startswith('.'):
                prefixes.append(imp)
        return [p for p in prefixes if p]

    @staticmethod
    def add_entity_nodes(graph: nx.DiGraph, entities: Dict[str, dict]) -> None:
        for entity_name, info in entities.items():
            types = info.get('type') or []
            declaring = info.get('declaring_chunk_ids', [])
            calling = info.get('calling_chunk_ids', [])
            if entity_name in graph and not isinstance(graph.nodes[entity_name].get('data'), EntityNode):
                logger.warning(f"Entity '{entity_name}' collides with a node id of another type, skipped")
                continue
            if not isinstance(info.get('attributes'), dict):
                info['attributes'] = {}
            graph.add_node(entity_name, level=3, data=EntityNode(
                id=entity_name,
                name=entity_name,
                entity_type=types[0] if types else '',
                declaring_chunk_ids=list(declaring),
                calling_chunk_ids=list(calling),
                aliases=list(info.get('aliases', [])),
                attributes=info['attributes'],  # shared with the registry record: enrichers update both
                file_path=info.get('file_path') or '',
                start_line=int(info.get('start_line') or 0),
                end_line=int(info.get('end_line') or 0),
            ))
            declaring_set = set(declaring)
            for chunk_id in declaring:
                if chunk_id in graph:
                    graph.add_edge(chunk_id, entity_name, relation=DECLARES)
            for chunk_id in calling:
                if chunk_id in graph and chunk_id not in declaring_set:
                    graph.add_edge(entity_name, chunk_id, relation=CALLED_BY)

    def add_inheritance_edges(self, graph: nx.DiGraph, registry: EntityRegistry,
                              analyses: Dict[str, FileAnalysis]) -> None:
        for path in sorted(analyses):
            file_node = analyses[path].file_node
            imports = self._import_prefixes(file_node)
            for entity in file_node.declared_entities:
                bases = entity.get('bases') or []
                if not bases:
                    continue
                child = registry.lookup(entity.get('name', ''))
                for base in bases:
                    parent = registry.resolve(base, imports)
                    if child and parent and child != parent and child in graph and parent in graph:
                        graph.add_edge(child, parent, relation=INHERITS)

    def add_call_edges(self, graph: nx.DiGraph, registry: EntityRegistry,
                       analyses: Dict[str, FileAnalysis]) -> None:
        """'calls' (method -> method) and 'uses_type' (entity -> type) edges from declaration attributes.

        A 'calls' edge lists its call sites (`sites`: file, line, and for each argument the callee parameter
        it binds to with the argument's shape and origins), so that a data-flow path can be shown step by step.
        """
        budget = self.max_call_edges or None
        added = 0
        for path in sorted(analyses):
            file_node = analyses[path].file_node
            imports = self._import_prefixes(file_node)
            for entity in file_node.declared_entities:
                attributes = entity.get('attributes') if isinstance(entity, dict) else None
                if not attributes:
                    continue
                source = registry.lookup(entity.get('name', ''))
                if not source or source not in graph:
                    continue
                for invocation in attributes.get('invocations') or []:
                    target = registry.resolve(invocation['name'], imports) if invocation.get('name') else None
                    if (not target or not registry.entities.get(target, {}).get('declaring_chunk_ids'))                             and invocation.get('receiver_type') and invocation.get('member') != '<init>':
                        from .call_resolution import resolve_member  # inherited method (BaseEntity.getId)
                        target = resolve_member(registry, invocation['receiver_type'], invocation['member'], imports)
                    if (not target or target == source or target not in graph
                            or not registry.entities[target]['declaring_chunk_ids']):
                        continue
                    site = self._call_site(path, invocation, registry.entities[target])
                    if graph.has_edge(source, target):
                        data = graph.edges[source, target]
                        if data.get('relation') == CALLS and site not in data.setdefault('sites', []):
                            data['sites'].append(site)
                        continue
                    if budget is not None and added >= budget:
                        logger.warning(f'max_call_edges={budget} reached, remaining call edges skipped')
                        return
                    graph.add_edge(source, target, relation=CALLS, sites=[site])
                    added += 1
                for relation, key in ((CALLS, 'calls'), (USES_TYPE, 'uses_types')):
                    for name in attributes.get(key) or []:
                        target = registry.resolve(name, imports)
                        if (not target or target == source or target not in graph
                                or not registry.entities[target]['declaring_chunk_ids']
                                or graph.has_edge(source, target)):
                            continue
                        if budget is not None and added >= budget:
                            logger.warning(f'max_call_edges={budget} reached, remaining call edges skipped')
                            return
                        graph.add_edge(source, target, relation=relation)
                        added += 1

    @staticmethod
    def _call_site(path: str, invocation: dict, target_record: dict) -> dict:
        """Evidence of one call: where it is and which argument binds to which callee parameter."""
        params = [p.get('name') for p in (target_record.get('attributes') or {}).get('params') or []]
        site: Dict[str, Any] = {'file': path, 'line': invocation.get('line'), 'site': invocation.get('site')}
        bindings = []
        for index, shape in enumerate(invocation.get('arg_shapes') or []):
            binding = {'index': index, 'shape': shape.get('shape'), 'constant': bool(shape.get('constant'))}
            if index < len(params):
                binding['param'] = params[index]
            if shape.get('origins'):
                binding['origins'] = shape['origins']
            bindings.append(binding)
        if bindings:
            site['args'] = bindings
        return site

    @staticmethod
    def add_implementation_edges(graph: nx.DiGraph, registry: EntityRegistry) -> None:
        """interface -implemented_by-> every concrete class implementing it, directly or through a parent."""
        parents: Dict[str, set] = {}
        for u, v, data in graph.edges(data=True):
            if data.get('relation') == INHERITS:
                parents.setdefault(u, set()).add(v)

        def types(name: str) -> List[str]:
            return (registry.entities.get(name) or {}).get('type') or []

        for child in sorted(parents):
            if 'interface' in types(child) or 'annotation' in types(child):
                continue
            seen: set = set()
            stack = list(parents[child])
            while stack:
                ancestor = stack.pop()
                if ancestor in seen:
                    continue
                seen.add(ancestor)
                stack.extend(parents.get(ancestor, ()))
                if 'interface' in types(ancestor) and not graph.has_edge(ancestor, child):
                    graph.add_edge(ancestor, child, relation=IMPLEMENTED_BY, direct=ancestor in parents[child])

    @staticmethod
    def add_import_edges(graph: nx.DiGraph, analyses: Dict[str, FileAnalysis]) -> None:
        resolver = ImportResolver(analyses.keys())
        for path in sorted(analyses):
            for imp in getattr(analyses[path].file_node, 'imports', []) or []:
                target = resolver.resolve(imp, path)
                if target and target != path and not graph.has_edge(path, target):
                    graph.add_edge(path, target, relation=IMPORTS)


class ImportResolver:
    """Map an import string (Python module, Java class, JS/TS relative path) to a repository file."""

    JS_EXTENSIONS = ('', '.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.d.ts',
                     '/index.ts', '/index.tsx', '/index.js', '/index.jsx')

    def __init__(self, file_paths: Iterable[str]):
        self.files = set(file_paths)
        self._dotted: Dict[str, set] = {}
        for path in self.files:
            ext = get_extension(path)
            if ext not in ('py', 'pyw', 'java'):
                continue
            parts = path[: -(len(ext) + 1)].split('/')
            for i in range(len(parts)):
                self._dotted.setdefault('.'.join(parts[i:]), set()).add(path)

    def _unique(self, key: str) -> Optional[str]:
        matches = self._dotted.get(key)
        return next(iter(matches)) if matches and len(matches) == 1 else None

    def resolve(self, imp: str, importer: str) -> Optional[str]:
        if not imp:
            return None
        ext = get_extension(importer)
        spec = get_language_spec(importer)
        if spec and spec.extractor in ('javascript', 'typescript'):
            if not imp.startswith('.'):
                return None  # package import (node_modules)
            base = posixpath.normpath(posixpath.join(posixpath.dirname(importer), imp))
            for suffix in self.JS_EXTENSIONS:
                if base + suffix in self.files:
                    return base + suffix
            return None
        if ext in ('py', 'pyw'):
            if imp.startswith('.'):
                level = len(imp) - len(imp.lstrip('.'))
                package = importer.split('/')[:-1]
                if level - 1 > len(package):
                    return None
                base = package[:len(package) - (level - 1)]
                rest = imp.lstrip('.')
                parts = base + (rest.split('.') if rest else [])
                for candidate in ('/'.join(parts) + '.py', '/'.join(parts + ['__init__']) + '.py'):
                    if candidate in self.files:
                        return candidate
                return None
            return self._unique(imp) or self._unique(imp + '.__init__')
        if ext == 'java':
            if imp.endswith('.*'):
                return None
            return self._unique(imp)
        return None


def run_sync(coro_factory: Callable[[], 'asyncio.Future']):
    """
    Run a coroutine from synchronous code, even when an event loop is already running
    (Jupyter, async frameworks): in that case it runs in a separate thread with its own loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro_factory())).result()
