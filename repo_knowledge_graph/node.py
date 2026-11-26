from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, List, Optional


@dataclass
class Node:
    id: str = ''
    name: str = ''
    node_type: str = ''
    description: Optional[str] = None
    declared_entities: List[dict] = field(default_factory=list)  # Classes, functions, variables
    called_entities: List[str] = field(default_factory=list)  # Classes, functions, variables, but also external libraries

    def dict(self):
        return {k: str(v) for k, v in asdict(self).items()}

    @classmethod
    def field_names(cls) -> set:
        return {f.name for f in fields(cls)}

    @classmethod
    def from_kwargs(cls, kwargs: Dict[str, Any]) -> 'Node':
        """Build a node ignoring unknown keys (forward/backward compatible deserialization)."""
        known = cls.field_names()
        return cls(**{k: v for k, v in kwargs.items() if k in known})


@dataclass
class DirectoryNode(Node):
    path: str = ''
    node_type: str = 'directory'


@dataclass
class FileNode(Node):
    path: str = ''
    content: str = ''
    node_type: str = 'file'
    language: str = ''
    content_hash: str = ''  # sha256 of the content, used for incremental updates
    imports: List[str] = field(default_factory=list)  # modules / paths imported by the file
    attributes: Dict[str, Any] = field(default_factory=dict)  # e.g. hardcoded_secrets (CWE-798 candidates)


@dataclass
class ChunkNode(FileNode):
    node_type: str = 'chunk'
    order_in_file: int = field(default_factory=int)
    embedding: Optional[list] = None
    start_line: int = 0  # 1-based, 0 = unknown (graphs saved before schema_version 3)
    end_line: int = 0

    def get_field_to_embed(self) -> str:
        """
        Text used to compute the chunk embedding: file path, optional LLM description, then code.
        The code content is always included so embeddings stay meaningful when no description
        has been generated (describe_nodes=False).
        """
        parts = [f"File: {self.path}"] if self.path else []
        annotations = sorted({a for e in self.declared_entities if isinstance(e, dict)
                              for a in _annotation_names(e)})
        if annotations:
            parts.append('Annotations: ' + ' '.join('@' + a for a in annotations))
        endpoints = [e for e in self.declared_entities if isinstance(e, dict) and e.get('endpoint')]
        for e in endpoints:
            parts.append(f"Endpoint: {' '.join(e.get('methods') or [])} {e['endpoint']}".strip())
        if self.description:
            parts.append(self.description)
        parts.append(self.content or "")
        return "\n".join(parts)


@dataclass
class EntityNode(Node):
    entity_type: str = ''
    declaring_chunk_ids: List[str] = field(default_factory=list)
    calling_chunk_ids: List[str] = field(default_factory=list)
    aliases: List[str] = field(default_factory=list)  # All possible aliases for this entity
    node_type: str = 'entity'
    # Extra knowledge: annotations, modifiers, signature, framework data (spring.*, jpa.*...)
    attributes: Dict[str, Any] = field(default_factory=dict)
    file_path: str = ''
    start_line: int = 0
    end_line: int = 0

    def __post_init__(self):
        # Use entity_name (stored in name field) as the id if id is not set
        if not self.id and self.name:
            self.id = self.name

    def get_field_to_embed(self) -> Optional[str]:
        return self.name


@dataclass
class CommitNode(Node):
    """A git commit (see git_history.py). `name` holds the short hash."""
    node_type: str = 'commit'
    commit_hash: str = ''
    author: str = ''
    date: str = ''  # ISO 8601
    message: str = ''
    files_changed: List[str] = field(default_factory=list)


@dataclass
class ResourceNode(Node):
    """
    A framework-level object added by an enricher (see enrichers/): REST endpoint, configuration
    property, database table, build module, library dependency or messaging channel.
    `node_type` is one of RESOURCE_TYPES, `id` is '<prefix>:<key>' (e.g. 'endpoint:GET /api/orders').
    """
    key: str = ''
    attributes: Dict[str, Any] = field(default_factory=dict)
    file_path: str = ''
    start_line: int = 0


RESOURCE_TYPES = ('endpoint', 'config_property', 'table', 'module', 'dependency', 'channel')

NODE_CLASSES = {
    cls.__name__: cls
    for cls in (Node, DirectoryNode, FileNode, ChunkNode, EntityNode, CommitNode, ResourceNode)
}

NODE_LEVELS = {'root': 0, 'directory': 1, 'file': 1, 'chunk': 2, 'entity': 3, 'commit': 4,
               **{t: 5 for t in RESOURCE_TYPES}}


def _annotation_names(entity: dict) -> List[str]:
    attributes = entity.get('attributes') or {}
    return [a.get('name', '') for a in attributes.get('annotations') or [] if isinstance(a, dict) and a.get('name')]


def node_level(node: Node) -> int:
    return NODE_LEVELS.get(node.node_type, 1)
