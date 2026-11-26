"""
Shared infrastructure of the framework enrichers.

An enricher runs at the end of `GraphBuilder.assemble_graph`, once the tree, the entities and the
generic relations are in place. It reads the file analyses (file contents and declared entities with
their attributes) and adds framework knowledge to the graph:

- attributes on entity nodes (the dict is shared with the entity registry record, so both stay in sync),
- resource nodes (`ResourceNode`: endpoint, config_property, table, module, dependency, channel),
- edges between entities and resources.

Enrichers must be pure functions of their inputs (no global state) so that incremental updates, which
re-assemble the graph, give the same result as a full build.
"""
import logging
import posixpath
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

import networkx as nx

from ..entity_registry import EntityRegistry
from ..node import EntityNode, ResourceNode

logger = logging.getLogger(__name__)

RESOURCE_LEVEL = 5
GENERIC_RELATIONS = ('calls', 'uses_type', 'called_by')


class EnrichmentContext:
    """What an enricher works on, with helpers to query Java declarations and to add nodes / edges."""

    def __init__(self, graph: nx.DiGraph, registry: EntityRegistry, analyses: Dict[str, Any],
                 mask_config_values: bool = True):
        self.graph = graph
        self.registry = registry
        self.analyses = analyses
        self.mask_config_values = mask_config_values
        self.shared: Dict[str, Any] = {}  # data passed from one enricher to the next (e.g. config values)
        self._java_types: Optional[Dict[str, dict]] = None
        self._constants: Optional[Dict[str, str]] = None

    # ------------------------------------------------------------------ files

    def files(self, *extensions: str) -> Iterator[Tuple[str, str]]:
        """(path, content) of the analysed files, optionally filtered by extension / file name."""
        for path in sorted(self.analyses):
            lower = path.lower()
            if not extensions or any(lower.endswith(ext) for ext in extensions):
                yield path, self.analyses[path].file_node.content or ''

    def file_declarations(self, path: str) -> List[dict]:
        return [e for e in self.analyses[path].file_node.declared_entities if isinstance(e, dict)]

    def java_declarations(self) -> Iterator[Tuple[str, dict]]:
        for path in sorted(self.analyses):
            if path.lower().endswith('.java'):
                for entity in self.file_declarations(path):
                    yield path, entity

    @property
    def java_types(self) -> Dict[str, dict]:
        """Canonical name -> declaration dict of every Java type (class, interface, enum, record, annotation)."""
        if self._java_types is None:
            self._java_types = {}
            for path, entity in self.java_declarations():
                if entity.get('type') in ('class', 'interface', 'enum', 'record', 'annotation'):
                    canonical = self.registry.lookup(entity['name']) or entity['name']
                    self._java_types.setdefault(canonical, {**entity, '_path': path})
        return self._java_types

    def members_of(self, type_name: str) -> List[dict]:
        """Declarations (methods, fields, constructors) whose owner is `type_name`."""
        declaration = self.java_types.get(type_name)
        if not declaration:
            return []
        prefix = declaration['name'] + '::'
        return [e for e in self.file_declarations(declaration['_path'])
                if e.get('name', '').startswith(prefix) and '.' not in e['name'][len(prefix):]]

    @property
    def constants(self) -> Dict[str, str]:
        """String constants of the repository: 'Type.NAME', 'pkg.Type.NAME' and 'NAME' -> value."""
        if self._constants is None:
            self._constants = {}
            for _, entity in self.java_declarations():
                value = (entity.get('attributes') or {}).get('constant_value')
                if value is None or '::' not in entity.get('name', ''):
                    continue
                owner, name = entity['name'].rsplit('::', 1)
                self._constants[f'{owner}.{name}'] = value
                self._constants[f"{owner.rsplit('.', 1)[-1]}.{name}"] = value
                self._constants.setdefault(name, value)
        return self._constants

    def resolve(self, name: Optional[str], path: Optional[str] = None) -> Optional[str]:
        """Canonical entity for a (possibly simple) name, using the imports of `path` when given."""
        if not name:
            return None
        imports: List[str] = []
        if path and path in self.analyses:
            from ..builder import GraphBuilder
            imports = GraphBuilder._import_prefixes(self.analyses[path].file_node)
        return self.registry.resolve(name, imports)

    def qualify(self, name: Optional[str], path: Optional[str] = None) -> Optional[str]:
        """Declared entity for a type name, else its fully qualified name from the file imports, else the name."""
        if not name:
            return None
        resolved = self.resolve(name, path)
        if resolved and self.declared(resolved):
            return resolved
        if '.' not in name and path in self.analyses:
            for imp in self.analyses[path].file_node.imports or []:
                if imp.endswith('.' + name):
                    return imp
        return name

    def declared(self, name: Optional[str]) -> bool:
        return bool(name) and name in self.registry.entities and bool(
            self.registry.entities[name]['declaring_chunk_ids'])

    # ------------------------------------------------------------------ graph updates

    def entity_attributes(self, name: str) -> Dict[str, Any]:
        """Attributes dict of an entity node (shared with the registry record)."""
        record = self.registry.entities.get(name)
        if record is None:
            return {}
        if not isinstance(record.get('attributes'), dict):
            record['attributes'] = {}
        node = self.graph.nodes[name]['data'] if name in self.graph else None
        if isinstance(node, EntityNode) and node.attributes is not record['attributes']:
            node.attributes = record['attributes']
        return record['attributes']

    def set_attribute(self, name: str, key: str, value: Any) -> None:
        if name in self.registry.entities:
            self.entity_attributes(name)[key] = value

    def framework_attributes(self, name: str, namespace: str) -> Dict[str, Any]:
        """attributes[namespace] dict of an entity ('spring', 'jpa', 'test'...), created on demand."""
        attributes = self.entity_attributes(name)
        if not isinstance(attributes.get(namespace), dict):
            attributes[namespace] = {}
        return attributes[namespace]

    def set_framework_attribute(self, name: str, namespace: str, key: str, value: Any) -> None:
        if name in self.registry.entities and value not in (None, '', [], {}):
            self.framework_attributes(name, namespace)[key] = value

    def add_resource(self, node_type: str, key: str, attributes: Optional[Dict[str, Any]] = None,
                     file_path: str = '', start_line: int = 0, name: Optional[str] = None) -> str:
        """Add (or merge into) a resource node; returns its id '<prefix>:<key>'."""
        prefix = {'config_property': 'config', 'dependency': 'dep'}.get(node_type, node_type)
        node_id = f'{prefix}:{key}'
        if node_id in self.graph and isinstance(self.graph.nodes[node_id].get('data'), ResourceNode):
            node = self.graph.nodes[node_id]['data']
            for k, v in (attributes or {}).items():
                if k not in node.attributes or node.attributes[k] in (None, '', [], {}):
                    node.attributes[k] = v
            if file_path and not node.file_path:
                node.file_path, node.start_line = file_path, start_line
            return node_id
        node = ResourceNode(id=node_id, name=name or key, node_type=node_type, key=key,
                            attributes=dict(attributes or {}), file_path=file_path, start_line=start_line)
        self.graph.add_node(node_id, data=node, level=RESOURCE_LEVEL)
        return node_id

    def add_edge(self, source: Optional[str], target: Optional[str], relation: str, **attributes: Any) -> bool:
        if not source or not target or source == target or source not in self.graph or target not in self.graph:
            return False
        if self.graph.has_edge(source, target):
            # One edge per pair in a DiGraph: a framework relation replaces a generic one ('calls',
            # 'uses_type', 'called_by'), which is kept in the 'also' attribute; otherwise the first one wins.
            data = self.graph.edges[source, target]
            current = data.get('relation')
            if current != relation:
                if current not in GENERIC_RELATIONS:
                    return False
                data['also'] = list(dict.fromkeys(list(data.get('also') or []) + [current]))
                data['relation'] = relation
            data.update({k: v for k, v in attributes.items() if v is not None})
            return True
        self.graph.add_edge(source, target, relation=relation,
                            **{k: v for k, v in attributes.items() if v is not None})
        return True

    def directory_of(self, path: str) -> str:
        directory = posixpath.dirname(path)
        return directory if directory in self.graph else 'root'


def annotation(attributes: Optional[Dict[str, Any]], *names: str) -> Optional[Dict[str, Any]]:
    """First annotation of a declaration whose simple name is one of `names`."""
    for item in (attributes or {}).get('annotations') or []:
        if isinstance(item, dict) and item.get('name') in names:
            return item
    return None


def annotation_names(attributes: Optional[Dict[str, Any]]) -> List[str]:
    return [a.get('name', '') for a in (attributes or {}).get('annotations') or [] if isinstance(a, dict)]


def annotation_value(item: Optional[Dict[str, Any]], *keys: str) -> Any:
    """Value of the first present argument among `keys` ('value' for a single unnamed argument)."""
    if not item:
        return None
    args = item.get('args') or {}
    for key in keys or ('value',):
        if key in args:
            return args[key]
    return None


def as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def enum_name(value) -> str:
    """'CascadeType.ALL', '{{CascadeType.ALL}}' or 'ALL' -> 'ALL'."""
    return str(value).strip('{}').rsplit('.', 1)[-1]


def simple_name(name: str) -> str:
    return name.replace('::', '.').rsplit('.', 1)[-1]


def strip_class_literal(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    return value[:-len('.class')] if value.endswith('.class') else value


def iter_strings(values: Iterable[Any]) -> Iterator[str]:
    for value in values:
        if isinstance(value, str):
            yield value
