"""
MCP server exposing a RepoKnowledgeGraph to LLM agents.

Graph model reminder (useful to read the tool descriptions):
  node types : root, directory, file, chunk (piece of a file), entity (class, function, variable...),
               commit (only when built with git history),
               endpoint, config_property, table, module, dependency, channel (framework enrichment)
  relations  : contains (root/directory -> directory/file, file -> chunk)
               declares (chunk -> entity), called_by (entity -> chunk that uses it)
               imports (file -> file), inherits (class entity -> base entity)
               calls (method -> method), uses_type (entity -> type), implemented_by (interface -> class)
               modifies (commit -> file)
               injects, handled_by, calls_endpoint, handles, persists, maps_to, relates_to, queries, migrates,
               reads_config, defined_in, publishes, listens_to, tests, depends_on, builds, provides
               (framework enrichment, see enrichers/ and framework_tools.py)
"""
import difflib
import functools
import logging
import re
from typing import Annotated, Any, Dict, List, Literal, Optional

from fastmcp import FastMCP
from pydantic import Field

from . import content_safety, observability
from .builder import CALLED_BY, CONTAINS, DECLARES
from .knowledge_graph import RepoKnowledgeGraph
from .node import ChunkNode, CommitNode, EntityNode, FileNode, ResourceNode
from .utils.chunk_utils import join_organized_chunks, organize_chunks_by_file_name

logger = logging.getLogger(__name__)

MAX_LIMIT = 200
PREVIEW_CHARS = 200
MAX_CONTENT_CHARS = 20_000
MAX_TREE_NODES = 500

NodeType = Literal['root', 'directory', 'file', 'chunk', 'entity', 'commit',
                   'endpoint', 'config_property', 'table', 'module', 'dependency', 'channel']


# Custom Exceptions
class MCPServerError(Exception):
    """Base exception for MCP server errors"""


class NodeNotFoundError(MCPServerError):
    """Raised when a node is not found"""


class EntityNotFoundError(MCPServerError):
    """Raised when an entity is not found"""


class InvalidInputError(MCPServerError):
    """Raised when input validation fails"""


def _preview(text: Optional[str], size: int = PREVIEW_CHARS) -> Optional[str]:
    if not text:
        return text
    return text[:size] + "..." if len(text) > size else text


class KnowledgeGraphMCPServer:
    """
    MCP Server for interacting with a codebase knowledge graph.

    Attributes:
        knowledge_graph (RepoKnowledgeGraph): The loaded knowledge graph object.
        app (FastMCP): The FastMCP application instance for tool registration and serving.
    """

    def __init__(self, knowledge_graph: Optional[RepoKnowledgeGraph] = None,
                 knowledge_graph_path: Optional[str] = None, server_name: str = "knowledge-graph-mcp-server"):
        if knowledge_graph is None:
            if knowledge_graph_path is None:
                raise ValueError("Provide a knowledge_graph or a knowledge_graph_path")
            knowledge_graph = RepoKnowledgeGraph.load_graph_from_file(knowledge_graph_path)
        self.knowledge_graph = knowledge_graph
        self.osv_client: Any = None  # vulnerability lookup client (vulnerability_lookup.OsvClient), set on registration
        self.app = FastMCP(server_name)
        self._indexes: Optional[Dict[str, Dict[str, List[str]]]] = None
        self.register_tools()

    # ------------------------------------------------------------------ constructors

    @classmethod
    def from_path(cls, path: str, server_name: str = "knowledge-graph-mcp-server", **kwargs):
        """Build the server from a repository path (kwargs: see RepoKnowledgeGraph.from_path)."""
        kwargs.setdefault('extract_entities', True)
        return cls(knowledge_graph=RepoKnowledgeGraph.from_path(path, **kwargs), server_name=server_name)

    @classmethod
    def from_file(cls, filepath: str, server_name: str = "knowledge-graph-mcp-server", **kwargs):
        """Build the server from a serialized graph (kwargs: see RepoKnowledgeGraph.load_graph_from_file)."""
        return cls(knowledge_graph=RepoKnowledgeGraph.load_graph_from_file(filepath, **kwargs),
                   server_name=server_name)

    @classmethod
    def from_repo(cls, repo_url: str, server_name: str = "knowledge-graph-mcp-server", **kwargs):
        """Build the server from a git URL (kwargs: see RepoKnowledgeGraph.from_repo)."""
        kwargs.setdefault('extract_entities', True)
        return cls(knowledge_graph=RepoKnowledgeGraph.from_repo(repo_url, **kwargs), server_name=server_name)

    def run(self, **kwargs):
        try:
            self.app.run(**kwargs)
        finally:
            observability.flush()

    # ------------------------------------------------------------------ indexes & validation

    @property
    def graph(self):
        return self.knowledge_graph.graph

    def refresh_indexes(self) -> None:
        """Call after mutating the graph (e.g. incremental update)."""
        self._indexes = None

    @property
    def indexes(self) -> Dict[str, Dict[str, List[str]]]:
        if self._indexes is None:
            by_type: Dict[str, List[str]] = {}
            by_path: Dict[str, List[str]] = {}
            for node_id, attrs in self.graph.nodes(data=True):
                node = attrs['data']
                by_type.setdefault(node.node_type, []).append(node_id)
                path = getattr(node, 'path', None)
                if path:
                    by_path.setdefault(path, []).append(node_id)
            self._indexes = {'type': by_type, 'path': by_path}
        return self._indexes

    def _suggestions(self, name: str, candidates) -> str:
        close = difflib.get_close_matches(name, list(candidates), n=5, cutoff=0.6)
        return f" Did you mean: {', '.join(close)}?" if close else ""

    def _validate_node_exists(self, node_id: str) -> bool:
        if node_id not in self.graph:
            raise NodeNotFoundError(f"Node '{node_id}' not found in knowledge graph."
                                    + self._suggestions(node_id, self.indexes['path'].keys()))
        return True

    def _resolve_entity(self, entity_name: str) -> str:
        canonical = self.knowledge_graph.resolve_entity(entity_name)
        if canonical is None:
            raise EntityNotFoundError(f"Entity '{entity_name}' not found in knowledge graph."
                                      + self._suggestions(entity_name, self.knowledge_graph.entities.keys()))
        return canonical

    def _validate_entity_exists(self, entity_name: str) -> bool:
        self._resolve_entity(entity_name)
        return True

    def _resolve_node(self, node_id: str) -> str:
        """Node id, or the canonical entity for an entity name / alias."""
        if node_id in self.graph:
            return node_id
        canonical = self.knowledge_graph.resolve_entity(node_id)
        if canonical and canonical in self.graph:
            return canonical
        self._validate_node_exists(node_id)
        return node_id

    @staticmethod
    def _validate_positive_int(value: int, param_name: str, maximum: Optional[int] = MAX_LIMIT) -> bool:
        if value <= 0:
            raise InvalidInputError(f"{param_name} must be a positive integer, got {value}")
        if maximum is not None and value > maximum:
            raise InvalidInputError(f"{param_name} must be <= {maximum}, got {value}")
        return True

    def _page(self, items: list, limit: int, offset: int):
        self._validate_positive_int(limit, "limit")
        if offset < 0:
            raise InvalidInputError(f"offset must be >= 0, got {offset}")
        page = items[offset:offset + limit]
        return page, {"total": len(items), "offset": offset, "limit": limit,
                      "has_more": offset + len(page) < len(items)}

    @staticmethod
    def _sanitize_chunk_dict(chunk_dict: dict) -> dict:
        """Remove embedding data from chunk dictionary before returning to user"""
        sanitized = chunk_dict.copy()
        sanitized.pop('embedding', None)
        return sanitized

    @staticmethod
    def _sanitize_node_dict(node_dict: dict) -> dict:
        """Remove embedding data from node dictionary before returning to user"""
        sanitized = node_dict.copy()
        if isinstance(sanitized.get('data'), dict):
            sanitized['data'] = sanitized['data'].copy()
            sanitized['data'].pop('embedding', None)
        sanitized.pop('embedding', None)
        return sanitized

    @staticmethod
    def _handle_error(error: Exception, context: str = "") -> dict:
        """Structured error response (never a traceback)."""
        from .framework_queries import LookupError_
        from .git_history import GitError
        if isinstance(error, (NodeNotFoundError, LookupError_)):
            error_type = "node_not_found"
        elif isinstance(error, EntityNotFoundError):
            error_type = "entity_not_found"
        elif isinstance(error, (InvalidInputError, ValueError)):
            error_type = "invalid_input"
        elif isinstance(error, GitError):
            error_type = "git_error"
        else:
            logger.exception(f"Unexpected error in tool {context}")
            error_type = "internal_error"
        return {"error": str(error), "error_type": error_type, "context": context}

    def _tool(self, description: str):
        """Register an async method as an MCP tool with uniform error handling and optional tracing."""
        def decorator(fn):
            name = fn.__name__

            @functools.wraps(fn)
            async def wrapper(*args, **kwargs):
                try:
                    result = await fn(*args, **kwargs)
                except Exception as e:
                    result = self._handle_error(e, name)
                content_safety.annotate(result)  # repository content is data, never instructions
                observability.annotate_current({
                    "tool": name,
                    "arguments": {k: v for k, v in kwargs.items()},
                    "response_chars": observability.response_size(result),
                    "error_type": result.get("error_type") if isinstance(result, dict) else None,
                })
                return result

            self.app.tool(name=name, description=description)(observability.observe_tool(name)(wrapper))
            return fn
        return decorator

    def _entity_declared_types(self, node) -> List[dict]:
        out = []
        for entity in getattr(node, 'declared_entities', []) or []:
            if isinstance(entity, dict):
                out.append({"name": entity.get('name', '?'), "type": entity.get('type', '?')})
            else:
                out.append({"name": str(entity), "type": "Unknown"})
        return out

    def _chunk_ref(self, chunk_id: str) -> Optional[dict]:
        if chunk_id not in self.graph:
            return None
        chunk = self.graph.nodes[chunk_id]['data']
        return {"chunk_id": chunk_id, "file_path": getattr(chunk, 'path', ''),
                "order_in_file": getattr(chunk, 'order_in_file', 0),
                "content_preview": _preview(getattr(chunk, 'content', ''), 150)}

    # ------------------------------------------------------------------ tools

    def register_tools(self):
        kg = self.knowledge_graph

        @self._tool("Get information about a node: type, name, description, declared and called entities and a "
                    "content preview (or the full content with include_content=true). Entity names and aliases "
                    "are accepted as node ids.")
        async def get_node_info(
            node_id: Annotated[str, Field(description="Node id (file path, chunk id, entity name...).")],
            include_content: Annotated[bool, Field(description="Return the full content instead of a preview.")] = False,
        ) -> dict:
            node_id = self._resolve_node(node_id)
            node = self.graph.nodes[node_id]['data']
            declared = getattr(node, 'declared_entities', [])
            called = getattr(node, 'called_entities', [])
            content = getattr(node, 'content', None)
            result = {
                "node_id": node_id,
                "class": node.__class__.__name__,
                "name": node.name,
                "type": node.node_type,
                "description": node.description,
                "declared_entities": declared,
                "called_entities": called,
                "content_preview": _preview(content),
                "text": f"Node {node_id} ({node.name}) — {node.node_type} with {len(declared)} declared "
                        f"and {len(called)} called entities.",
            }
            if include_content and content is not None:
                result["content"] = content[:MAX_CONTENT_CHARS]
                result["content_truncated"] = len(content) > MAX_CONTENT_CHARS
            if isinstance(node, EntityNode):
                result.update(entity_type=node.entity_type, aliases=node.aliases[:10])
                if node.attributes:
                    result["attributes"] = node.attributes
                if node.file_path:
                    result["location"] = f"{node.file_path}:{node.start_line}" if node.start_line else node.file_path
                    result["text"] += f" Declared at {result['location']}."
                annotations = [a.get('name') for a in node.attributes.get('annotations') or [] if isinstance(a, dict)]
                if annotations:
                    result["text"] += " Annotations: " + ", ".join('@' + a for a in annotations) + "."
                spring = node.attributes.get('spring') or {}
                if spring.get('stereotype'):
                    result["text"] += f" Spring {spring['stereotype']} bean '{spring.get('bean_name')}'."
            if isinstance(node, ResourceNode):
                result.update(key=node.key, attributes=node.attributes)
                if node.file_path:
                    result["location"] = f"{node.file_path}:{node.start_line}" if node.start_line else node.file_path
            if isinstance(node, ChunkNode) and node.start_line:
                result["lines"] = [node.start_line, node.end_line]
            if isinstance(node, CommitNode):
                result.update(commit_hash=node.commit_hash, author=node.author, date=node.date,
                              message=node.message, files_changed=node.files_changed)
            return result

        @self._tool("List the incoming and outgoing edges of a node, optionally filtered by relation (contains, "
                    "declares, called_by, imports, inherits, calls, uses_type, implemented_by, modifies, and with "
                    "framework enrichment injects, handled_by, calls_endpoint, persists, maps_to, relates_to, queries, "
                    "reads_config, publishes, listens_to, tests, depends_on...).")
        async def get_node_edges(
            node_id: Annotated[str, Field(description="Node id whose edges to list.")],
            relation: Annotated[Optional[str], Field(description="Only keep edges with this relation.")] = None,
            limit: Annotated[int, Field(description="Maximum number of edges per direction.")] = 50,
            offset: Annotated[int, Field(description="Number of edges to skip per direction.")] = 0,
        ) -> dict:
            node_id = self._resolve_node(node_id)

            def edges(it):
                return [{"source": s, "target": t, "relation": d.get("relation", "?"),
                         **{k: v for k, v in d.items() if k != "relation"}}
                        for s, t, d in it if relation is None or d.get("relation") == relation]

            incoming, in_page = self._page(edges(self.graph.in_edges(node_id, data=True)), limit, offset)
            outgoing, out_page = self._page(edges(self.graph.out_edges(node_id, data=True)), limit, offset)
            text = f"Node '{node_id}' has {in_page['total']} incoming and {out_page['total']} outgoing edges.\n"
            text += "".join(f"← {e['source']} [{e['relation']}]\n" for e in incoming)
            text += "".join(f"→ {e['target']} [{e['relation']}]\n" for e in outgoing)
            return {"node_id": node_id, "incoming": incoming, "outgoing": outgoing,
                    "incoming_count": in_page['total'], "outgoing_count": out_page['total'],
                    "has_more": in_page['has_more'] or out_page['has_more'], "text": text}

        @self._tool("Search code chunks relevant to a natural-language or keyword query, using the code index "
                    "(semantic, keyword or hybrid search depending on how the index was built).")
        async def search_nodes(
            query: Annotated[str, Field(description="What to look for.")],
            limit: Annotated[int, Field(description="Maximum number of results.")] = 10,
        ) -> dict:
            self._validate_positive_int(limit, "limit")
            if kg.code_index is None:
                raise InvalidInputError("No code index: build the graph with index_nodes=True to enable search.")
            results = [r.to_dict() for r in kg.code_index.search(query, n_results=limit)]
            if not results:
                return {"query": query, "results": [], "count": 0, "text": f"No results found for '{query}'."}
            text = f"Found {len(results)} result(s) for query '{query}':\n"
            text += "".join(f"- {r['id']} (score {r['score']:.3f})\n" for r in results)
            return {"query": query, "count": len(results), "results": results, "text": text}

        @self._tool("Get statistics about the knowledge graph: node counts by type and edge counts by relation.")
        async def get_graph_stats() -> dict:
            node_types = {t: len(ids) for t, ids in self.indexes['type'].items()}
            edge_relations: Dict[str, int] = {}
            for _, _, attrs in self.graph.edges(data=True):
                relation = attrs.get('relation', 'Unknown')
                edge_relations[relation] = edge_relations.get(relation, 0) + 1
            num_nodes, num_edges = self.graph.number_of_nodes(), self.graph.number_of_edges()
            return {"total_nodes": num_nodes, "total_edges": num_edges, "node_types": node_types,
                    "edge_relations": edge_relations, "total_entities": len(kg.entities),
                    "has_code_index": kg.code_index is not None,
                    "text": f"Graph with {num_nodes} nodes, {num_edges} edges, {len(node_types)} node types, "
                            f"and {len(edge_relations)} relation types."}

        @self._tool("List nodes of a given type (root, directory, file, chunk, entity, commit, and with framework "
                    "enrichment endpoint, config_property, table, module, dependency, channel). For entities, "
                    "entity_type filters on the kind of code entity (class, function, method, variable...).")
        async def list_nodes_by_type(
            node_type: Annotated[NodeType, Field(description="Node type.")],
            entity_type: Annotated[Optional[str], Field(description="For node_type='entity': class, function, "
                                                                    "method, variable, interface...")] = None,
            limit: Annotated[int, Field(description="Maximum number of nodes.")] = 20,
            offset: Annotated[int, Field(description="Number of nodes to skip.")] = 0,
        ) -> dict:
            ids = sorted(self.indexes['type'].get(node_type, []))
            if entity_type:
                ids = [i for i in ids if entity_type in (kg.entities.get(i, {}).get('type') or [])]
            page, info = self._page(ids, limit, offset)
            results = [{"id": i, "name": self.graph.nodes[i]['data'].name} for i in page]
            label = f"{node_type}" + (f" ({entity_type})" if entity_type else "")
            if not results:
                return {"node_type": node_type, "results": [], "count": 0, **info,
                        "text": f"No nodes found of type '{label}'."}
            return {"node_type": node_type, "count": len(results), "results": results, **info,
                    "text": f"Found {info['total']} node(s) of type '{label}', showing {len(results)}."}

        @self._tool("Get the nodes directly connected to a node, with the relation type and direction.")
        async def get_neighbors(
            node_id: Annotated[str, Field(description="Node id whose neighbors to retrieve.")],
            limit: Annotated[int, Field(description="Maximum number of neighbors.")] = 20,
            offset: Annotated[int, Field(description="Number of neighbors to skip.")] = 0,
        ) -> dict:
            node_id = self._resolve_node(node_id)
            neighbors = []
            for _, target, d in self.graph.out_edges(node_id, data=True):
                neighbors.append((target, d.get('relation', 'Unknown'), "outgoing"))
            for source, _, d in self.graph.in_edges(node_id, data=True):
                neighbors.append((source, d.get('relation', 'Unknown'), "incoming"))
            page, info = self._page(neighbors, limit, offset)
            neighbor_list = []
            text = f"Neighbors of '{node_id}' ({info['total']} total):\n\n"
            for other, relation, direction in page:
                node = self.graph.nodes[other]['data']
                neighbor_list.append({"id": other, "name": node.name, "type": node.node_type,
                                      "relation": relation, "direction": direction})
                arrow = "→" if direction == "outgoing" else "←"
                text += f"- {other}: {node.name} ({node.node_type})\n  {arrow} Relation: {relation}\n"
            if info['has_more']:
                text += f"\n... and {info['total'] - offset - len(page)} more neighbors\n"
            return {"node_id": node_id, "total_neighbors": info['total'], "neighbors": neighbor_list,
                    "has_more": info['has_more'], "text": text}

        @self._tool("Find where an entity (class, function, variable...) is declared. Accepts simple names, "
                    "qualified names and aliases.")
        async def go_to_definition(
            entity_name: Annotated[str, Field(description="Entity name, e.g. 'Cart', 'Cart.total', 'pkg.mod.func'.")],
            limit: Annotated[int, Field(description="Maximum number of declarations.")] = 5,
        ) -> dict:
            canonical = self._resolve_entity(entity_name)
            info = kg.entities[canonical]
            declaring = info.get('declaring_chunk_ids', [])
            if not declaring:
                return {"entity_name": canonical, "declarations": [], "total_declarations": 0,
                        "text": f"Entity '{canonical}' is used in the code but not declared in this repository "
                                f"(external library or unresolved name)."}
            page, page_info = self._page(declaring, limit, 0)
            declarations = [ref for ref in (self._chunk_ref(c) for c in page) if ref]
            text = f"Definition(s) for '{canonical}':\n\nType: {', '.join(info.get('type') or ['Unknown'])}\n"
            if info.get('dtype'):
                text += f"Data Type: {info['dtype']}\n"
            text += f"\nDeclared in {len(declaring)} location(s):\n\n"
            for d in declarations:
                text += (f"- Chunk: {d['chunk_id']}\n  File: {d['file_path']}\n  Order: {d['order_in_file']}\n"
                         f"  Content: {d['content_preview']}\n\n")
            if page_info['has_more']:
                text += f"... and {len(declaring) - len(page)} more locations\n"
            result = {"entity_name": canonical, "requested_name": entity_name, "type": info.get('type', []),
                      "dtype": info.get('dtype'), "total_declarations": len(declaring),
                      "declarations": declarations, "has_more": page_info['has_more'], "text": text}
            if info.get('file_path'):
                location = f"{info['file_path']}:{info['start_line']}" if info.get('start_line') else info['file_path']
                result["location"] = location
                result["text"] = f"Location: {location}\n" + text
            signature = (info.get('attributes') or {}).get('signature')
            if signature:
                result["signature"] = signature
            return result

        @self._tool("Find the chunks that use / call an entity. Accepts simple names, qualified names and aliases.")
        async def find_usages(
            entity_name: Annotated[str, Field(description="Entity name.")],
            limit: Annotated[int, Field(description="Maximum number of usages.")] = 20,
            offset: Annotated[int, Field(description="Number of usages to skip.")] = 0,
        ) -> dict:
            canonical = self._resolve_entity(entity_name)
            calling = kg.entities[canonical].get('calling_chunk_ids', [])
            # Typed usages: callers (calls), beans injecting it (injects), declarations using the type (uses_type)
            typed: Dict[str, List[str]] = {}
            if canonical in self.graph:
                for source, _, d in self.graph.in_edges(canonical, data=True):
                    relation = d.get('relation')
                    if relation in ('calls', 'injects', 'uses_type', 'implemented_by', 'persists', 'tests',
                                    'handles', 'relates_to'):
                        typed.setdefault(relation, []).append(source)
            if not calling:
                return {"entity_name": canonical, "usages": [], "total_usages": 0, "typed_usages": typed,
                        "text": f"Entity '{canonical}' found but no usages identified."
                                + "".join(f"\n{r}: {', '.join(s[:10])}" for r, s in typed.items())}
            page, info = self._page(calling, limit, offset)
            usages = [ref for ref in (self._chunk_ref(c) for c in page) if ref]
            text = f"Usages of '{canonical}' ({len(calling)} total):\n\n"
            for u in usages:
                text += f"- {u['file_path']} (chunk {u['order_in_file']})\n  Content: {u['content_preview']}\n\n"
            if info['has_more']:
                text += f"\n... and {len(calling) - offset - len(page)} more usages\n"
            for relation, sources in typed.items():
                text += f"\n{relation} ({len(sources)}): {', '.join(sources[:10])}" + (" ..." if len(sources) > 10 else "")
            return {"entity_name": canonical, "total_usages": len(calling), "usages": usages, "typed_usages": typed,
                    "has_more": info['has_more'], "text": text}

        @self._tool("Overview of a file: language, chunks (with descriptions), declared entities and imported files.")
        async def get_file_structure(
            file_path: Annotated[str, Field(description="Repository-relative file path, e.g. 'src/app.py'.")],
            max_entities: Annotated[int, Field(description="Maximum number of entities listed.")] = 30,
            max_chunks: Annotated[int, Field(description="Maximum number of chunks listed.")] = 30,
        ) -> dict:
            self._validate_node_exists(file_path)
            file_node = self.graph.nodes[file_path]['data']
            if not isinstance(file_node, FileNode) or file_node.node_type != 'file':
                raise InvalidInputError(f"'{file_path}' is a {file_node.node_type}, not a file.")
            self._validate_positive_int(max_entities, "max_entities")
            self._validate_positive_int(max_chunks, "max_chunks")
            chunks = kg.get_chunks_of_file(file_path)
            all_entities = self._entity_declared_types(file_node)
            declared = all_entities[:max_entities]
            imports = [t for _, t, d in self.graph.out_edges(file_path, data=True) if d.get('relation') == 'imports']
            chunk_list = [{"id": c.id, "order_in_file": c.order_in_file, "description": _preview(c.description, 80)}
                          for c in chunks[:max_chunks]]

            text = (f"File Structure: {file_node.name}\nPath: {file_path}\nLanguage: {file_node.language}\n"
                    f"Total Chunks: {len(chunks)}\n\n")
            if declared:
                text += f"Declared Entities ({len(all_entities)}):\n"
                text += "".join(f"  - {e['name']} ({e['type']})\n" for e in declared)
                if len(all_entities) > max_entities:
                    text += f"  ... and {len(all_entities) - max_entities} more\n"
            if imports:
                text += "\nImports:\n" + "".join(f"  - {i}\n" for i in imports)
            text += "\nChunks:\n"
            for c in chunk_list:
                text += f"  [{c['order_in_file']}] {c['id']}\n"
                if c['description']:
                    text += f"      {c['description']}\n"
            if len(chunks) > max_chunks:
                text += f"  ... and {len(chunks) - max_chunks} more chunks\n"
            return {"file_path": file_path, "file_name": file_node.name, "language": file_node.language,
                    "total_chunks": len(chunks), "total_declared_entities": len(all_entities),
                    "declared_entities": declared, "imports": imports, "chunks": chunk_list,
                    "has_more_entities": len(all_entities) > max_entities,
                    "has_more_chunks": len(chunks) > max_chunks, "text": text}

        @self._tool("Chunks related to a chunk through entities. relation_type='calls': chunks declaring the "
                    "entities this chunk uses. relation_type='called_by': chunks using the entities this chunk declares.")
        async def get_related_chunks(
            chunk_id: Annotated[str, Field(description="Chunk id, e.g. 'src/app.py_0'.")],
            relation_type: Annotated[Literal['calls', 'called_by'], Field(description="Direction.")] = "calls",
            limit: Annotated[int, Field(description="Maximum number of related chunks.")] = 15,
        ) -> dict:
            self._validate_node_exists(chunk_id)
            if not isinstance(self.graph.nodes[chunk_id]['data'], ChunkNode):
                raise InvalidInputError(f"'{chunk_id}' is not a chunk.")
            related: Dict[str, set] = {}
            if relation_type == "calls":
                # chunk <-called_by- entity <-declares- other chunk
                for entity, _, d in self.graph.in_edges(chunk_id, data=True):
                    if d.get('relation') != CALLED_BY:
                        continue
                    for other, _, d2 in self.graph.in_edges(entity, data=True):
                        if d2.get('relation') == DECLARES and other != chunk_id:
                            related.setdefault(other, set()).add(entity)
            else:
                # chunk -declares-> entity -called_by-> other chunk
                for _, entity, d in self.graph.out_edges(chunk_id, data=True):
                    if d.get('relation') != DECLARES:
                        continue
                    for _, other, d2 in self.graph.out_edges(entity, data=True):
                        if d2.get('relation') == CALLED_BY and other != chunk_id:
                            related.setdefault(other, set()).add(entity)
            items = [{"id": cid, "file_path": getattr(self.graph.nodes[cid]['data'], 'path', 'Unknown'),
                      "entities": sorted(entities)} for cid, entities in sorted(related.items())]
            page, info = self._page(items, limit, 0)
            if not items:
                return {"chunk_id": chunk_id, "relation_type": relation_type, "related_chunks": [],
                        "total_related": 0,
                        "text": f"No chunks found with '{relation_type}' relationship from '{chunk_id}'"}
            text = f"Chunks related to '{chunk_id}' via '{relation_type}' ({len(items)} total):\n\n"
            for c in page:
                text += f"- {c['id']}\n  File: {c['file_path']}\n  Entities: {', '.join(c['entities'])}\n"
            if info['has_more']:
                text += f"\n... and {len(items) - len(page)} more\n"
            return {"chunk_id": chunk_id, "relation_type": relation_type, "total_related": len(items),
                    "related_chunks": page, "has_more": info['has_more'], "text": text}

        @self._tool("List the entities of the knowledge graph with their types, declaration and usage counts.")
        async def list_all_entities(
            limit: Annotated[int, Field(description="Maximum number of entities.")] = 50,
            offset: Annotated[int, Field(description="Number of entities to skip.")] = 0,
            entity_type: Annotated[Optional[str], Field(description="Only this type (class, function...).")] = None,
            declared_only: Annotated[bool, Field(description="Only entities declared in the repository.")] = False,
        ) -> dict:
            names = sorted(kg.entities)
            if entity_type:
                names = [n for n in names if entity_type in (kg.entities[n].get('type') or [])]
            if declared_only:
                names = [n for n in names if kg.entities[n].get('declaring_chunk_ids')]
            if not names:
                return {"entities": [], "total_entities": 0, "text": "No entities found in the knowledge graph."}
            page, info = self._page(names, limit, offset)
            entities = [{"name": n, "types": kg.entities[n].get('type') or ['Unknown'],
                         "declaration_count": len(kg.entities[n].get('declaring_chunk_ids', [])),
                         "usage_count": len(kg.entities[n].get('calling_chunk_ids', []))} for n in page]
            text = f"All Entities ({len(names)} total):\n\n"
            for i, e in enumerate(entities, offset + 1):
                text += (f"{i}. {e['name']}\n   Types: {', '.join(e['types'])}\n"
                         f"   Declarations: {e['declaration_count']}\n   Usages: {e['usage_count']}\n\n")
            if info['has_more']:
                text += f"... and {len(names) - offset - len(page)} more entities\n"
            return {"total_entities": len(names), "entities": entities, "has_more": info['has_more'], "text": text}

        @self._tool("Unified diff between the contents of two nodes (chunks or files).")
        async def diff_chunks(
            node_id_1: Annotated[str, Field(description="First node id.")],
            node_id_2: Annotated[str, Field(description="Second node id.")],
        ) -> dict:
            self._validate_node_exists(node_id_1)
            self._validate_node_exists(node_id_2)
            content1 = getattr(self.graph.nodes[node_id_1]['data'], 'content', None)
            content2 = getattr(self.graph.nodes[node_id_2]['data'], 'content', None)
            if content1 is None or content2 is None:
                raise InvalidInputError("One or both nodes have no content.")
            diff = list(difflib.unified_diff(content1.splitlines(), content2.splitlines(),
                                             fromfile=node_id_1, tofile=node_id_2, lineterm=""))
            return {"node_id_1": node_id_1, "node_id_2": node_id_2, "has_differences": bool(diff),
                    "diff": diff, "text": "\n".join(diff) if diff else "No differences."}

        @self._tool("Tree view of the repository (directories and files) from a node, following 'contains' edges.")
        async def print_tree(
            root_id: Annotated[Optional[str], Field(description="Start node (default: repository root).")] = 'root',
            max_depth: Annotated[int, Field(description="Maximum depth.")] = 3,
            include_chunks: Annotated[bool, Field(description="Also list the chunks of each file.")] = False,
        ) -> dict:
            root_id = root_id or 'root'
            self._validate_node_exists(root_id)
            self._validate_positive_int(max_depth, "max_depth", maximum=20)
            count = 0
            truncated = False

            def build(node_id, depth):
                nonlocal count, truncated
                node = self.graph.nodes[node_id]['data']
                count += 1
                info = {"id": node_id, "name": node.name, "type": node.node_type, "depth": depth, "children": []}
                if depth >= max_depth:
                    return info
                children = sorted(t for _, t, d in self.graph.out_edges(node_id, data=True)
                                  if d.get('relation') == CONTAINS)
                for child in children:
                    if not include_chunks and self.graph.nodes[child]['data'].node_type == 'chunk':
                        continue
                    if count >= MAX_TREE_NODES:
                        truncated = True
                        break
                    info["children"].append(build(child, depth + 1))
                return info

            tree = build(root_id, 0)

            def fmt(node):
                return "  " * node["depth"] + f"- {node['name']} ({node['type']})\n" + "".join(
                    fmt(c) for c in node["children"])

            text = fmt(tree) + (f"... truncated after {MAX_TREE_NODES} nodes\n" if truncated else "")
            return {"root_id": root_id, "max_depth": max_depth, "tree": [tree], "truncated": truncated, "text": text}

        @self._tool("Search nodes of a type whose name contains a substring (case-insensitive). For entities, "
                    "entity_type narrows the kind of entity.")
        async def search_by_type_and_name(
            node_type: Annotated[NodeType, Field(description="Node type.")],
            name_query: Annotated[str, Field(description="Substring to look for in the name.")],
            entity_type: Annotated[Optional[str], Field(description="For entities: class, function...")] = None,
            limit: Annotated[int, Field(description="Maximum results.")] = 10,
        ) -> dict:
            self._validate_positive_int(limit, "limit")
            needle = name_query.lower()
            matches = []
            for node_id in sorted(self.indexes['type'].get(node_type, [])):
                node = self.graph.nodes[node_id]['data']
                if needle not in (node.name or '').lower():
                    continue
                if entity_type and entity_type not in (kg.entities.get(node_id, {}).get('type') or []):
                    continue
                matches.append({"id": node_id, "name": node.name,
                                "content_preview": _preview(getattr(node, 'content', None))})
            page, info = self._page(matches, limit, 0)
            if not matches:
                return {"node_type": node_type, "name_query": name_query, "matches": [], "count": 0,
                        "text": f"No matches for type '{node_type}' and name containing '{name_query}'."}
            text = f"Matches for type '{node_type}' and name '{name_query}' ({len(matches)} total):\n"
            text += "".join(f"- {m['id']}: {m['name']}\n" for m in page)
            return {"node_type": node_type, "name_query": name_query, "count": len(page), "total": len(matches),
                    "matches": page, "has_more": info['has_more'], "text": text}

        @self._tool("Full content of a chunk together with the previous and next chunks of the same file.")
        async def get_chunk_context(
            node_id: Annotated[str, Field(description="Chunk id.")],
        ) -> dict:
            self._validate_node_exists(node_id)
            current = self.graph.nodes[node_id]['data']
            if not isinstance(current, ChunkNode):
                raise InvalidInputError(f"'{node_id}' is not a chunk.")
            previous_chunk = kg.get_previous_chunk(node_id)
            next_chunk = kg.get_next_chunk(node_id)
            chunks = [c for c in (previous_chunk, current, next_chunk) if c is not None]
            return {
                "node_id": node_id,
                "current_chunk": {"id": node_id, "content": current.content},
                "previous_chunk": {"id": previous_chunk.id, "content": previous_chunk.content} if previous_chunk else None,
                "next_chunk": {"id": next_chunk.id, "content": next_chunk.content} if next_chunk else None,
                "text": join_organized_chunks(organize_chunks_by_file_name(chunks)),
            }

        @self._tool("Statistics for a file or directory path: lines, chunks, declared and called entities.")
        async def get_file_stats(
            path: Annotated[str, Field(description="Repository-relative file or directory path.")],
        ) -> dict:
            node_ids = [n for n in self.indexes['path'].get(path, [])
                        if self.graph.nodes[n]['data'].node_type in ('file', 'directory')]
            if not node_ids:
                raise NodeNotFoundError(f"No file or directory found for path '{path}'."
                                        + self._suggestions(path, self.indexes['path'].keys()))
            stats = []
            text = f"Statistics for '{path}':\n"
            for node_id in node_ids:
                node = self.graph.nodes[node_id]['data']
                content = getattr(node, 'content', '') or ''
                declared = getattr(node, 'declared_entities', [])
                called = getattr(node, 'called_entities', [])
                chunks = [t for _, t in self.graph.out_edges(node_id)
                          if self.graph.nodes[t]['data'].node_type == 'chunk']
                node_stats = {
                    "node_id": node_id, "node_type": node.node_type, "lines": len(content.splitlines()),
                    "declared_entities_count": len(declared),
                    "declared_entities": self._entity_declared_types(node)[:10],
                    "called_entities_count": len(called), "called_entities": [str(e) for e in called[:10]],
                    "chunks_count": len(chunks), "has_more_declared": len(declared) > 10,
                    "has_more_called": len(called) > 10,
                }
                stats.append(node_stats)
                text += (f"- Node: {node_id} ({node.node_type})\n  Lines: {node_stats['lines']}\n"
                         f"  Declared entities: {len(declared)}\n  Called entities: {len(called)}\n"
                         f"  Chunks: {len(chunks)}\n")
                text += "".join(f"    - {e['name']} ({e['type']})\n" for e in node_stats['declared_entities'])
            return {"path": path, "nodes": stats, "text": text}

        @self._tool("Search file paths matching a regular expression (Python syntax), e.g. '\\.py$' or 'test_'.")
        async def search_file_names_by_regex(
            pattern: Annotated[str, Field(description="Regular expression matched against file paths.")],
            limit: Annotated[int, Field(description="Maximum results.")] = 20,
        ) -> dict:
            self._validate_positive_int(limit, "limit")
            try:
                regex = re.compile(pattern)
            except re.error as e:
                raise InvalidInputError(f"Invalid regex pattern: {e}") from None
            matches = [{"node_id": n, "file_name": self.graph.nodes[n]['data'].name}
                       for n in sorted(self.indexes['type'].get('file', [])) if regex.search(n)]
            if not matches:
                return {"pattern": pattern, "matches": [], "count": 0,
                        "text": f"No file names matched the pattern: '{pattern}'"}
            page, info = self._page(matches, limit, 0)
            text = f"Files matching pattern '{pattern}':\n" + "".join(
                f"- {m['file_name']} (node ID: {m['node_id']})\n" for m in page)
            if info['has_more']:
                text += f"... and {len(matches) - len(page)} more\n"
            return {"pattern": pattern, "count": len(matches), "matches": page,
                    "has_more": info['has_more'], "text": text}

        @self._tool("Shortest directed path between two nodes (e.g. from a file to an entity).")
        async def find_path(
            source_id: Annotated[str, Field(description="Source node id.")],
            target_id: Annotated[str, Field(description="Target node id.")],
            max_depth: Annotated[int, Field(description="Maximum path length.")] = 5,
        ) -> dict:
            self._validate_positive_int(max_depth, "max_depth", maximum=20)
            result = kg.find_path(self._resolve_node(source_id), self._resolve_node(target_id), max_depth)
            if "error" in result:
                raise NodeNotFoundError(result["error"])
            return result

        @self._tool("Subgraph around a node up to a depth, optionally restricted to some relations "
                    "(contains, declares, called_by, imports, inherits, modifies).")
        async def get_subgraph(
            node_id: Annotated[str, Field(description="Central node id.")],
            depth: Annotated[int, Field(description="Radius (1-4).")] = 2,
            edge_types: Annotated[Optional[List[str]], Field(description="Relations to follow.")] = None,
        ) -> dict:
            self._validate_positive_int(depth, "depth", maximum=4)
            result = kg.get_subgraph(self._resolve_node(node_id), depth, edge_types)
            if "error" in result:
                raise NodeNotFoundError(result["error"])
            return result

        # --- git history -------------------------------------------------------------------

        @self._tool("Commits that modified a file (requires a graph built with git history).")
        async def get_file_history(
            file_path: Annotated[str, Field(description="Repository-relative file path.")],
            limit: Annotated[int, Field(description="Maximum number of commits.")] = 20,
        ) -> dict:
            self._validate_node_exists(file_path)
            if not self.indexes['type'].get('commit'):
                raise InvalidInputError("The graph has no git history: build it with include_git_history=True "
                                        "(CLI: --git-history).")
            commits = kg.get_file_history(file_path)
            page, info = self._page(commits, limit, 0)
            items = [{"commit_id": c.id, "hash": c.name, "author": c.author, "date": c.date, "message": c.message}
                     for c in page]
            text = f"History of '{file_path}' ({len(commits)} commits):\n" + "".join(
                f"- {i['hash']} {i['date'][:10]} {i['author']}: {i['message']}\n" for i in items)
            return {"file_path": file_path, "total_commits": len(commits), "commits": items,
                    "has_more": info['has_more'], "text": text}

        @self._tool("Details of a commit (hash or short hash): author, date, message and files modified.")
        async def get_commit_info(
            commit: Annotated[str, Field(description="Commit hash, short hash or commit node id.")],
        ) -> dict:
            matches = [n for n in self.indexes['type'].get('commit', [])
                       if n == commit or self.graph.nodes[n]['data'].commit_hash.startswith(commit.replace('commit:', ''))]
            if not matches:
                raise NodeNotFoundError(f"Commit '{commit}' not found in the graph history.")
            if len(matches) > 1:
                raise InvalidInputError(f"Ambiguous commit prefix '{commit}' ({len(matches)} matches).")
            node = self.graph.nodes[matches[0]]['data']
            text = (f"Commit {node.commit_hash}\nAuthor: {node.author}\nDate: {node.date}\n\n{node.message}\n\n"
                    f"Files ({len(node.files_changed)}):\n" + "".join(f"- {f}\n" for f in node.files_changed))
            return {"commit_id": node.id, "hash": node.commit_hash, "author": node.author, "date": node.date,
                    "message": node.message, "files_changed": node.files_changed, "text": text}

        @self._tool("Unified diff of a file between two git revisions (commit hashes, branches, tags, HEAD~N). "
                    "Requires the repository to be available locally.")
        async def diff_file_versions(
            file_path: Annotated[str, Field(description="Repository-relative file path.")],
            rev_a: Annotated[str, Field(description="Older revision, e.g. 'HEAD~3' or a commit hash.")],
            rev_b: Annotated[str, Field(description="Newer revision.")] = "HEAD",
        ) -> dict:
            from .git_history import diff_file, is_git_repository
            if not is_git_repository(kg.repo_path):
                raise InvalidInputError("The repository is not available locally (graph loaded from a file or "
                                        "built from a temporary clone): file diffs are unavailable.")
            if file_path not in self.graph and not any(file_path in c.files_changed for c in
                                                       (self.graph.nodes[n]['data'] for n in
                                                        self.indexes['type'].get('commit', []))):
                raise NodeNotFoundError(f"File '{file_path}' is not part of the repository graph.")
            diff = diff_file(kg.repo_path, file_path, rev_a, rev_b)
            return {"file_path": file_path, "rev_a": rev_a, "rev_b": rev_b, "has_differences": bool(diff.strip()),
                    "diff": diff, "text": diff or "No differences."}

        # Java / Spring Boot knowledge added by the enrichers (endpoints, beans, persistence, configuration...)
        from .framework_tools import register_framework_tools
        register_framework_tools(self)


def main():  # pragma: no cover - convenience entry point
    from .cli import main as cli_main
    cli_main()


if __name__ == '__main__':  # pragma: no cover
    main()
