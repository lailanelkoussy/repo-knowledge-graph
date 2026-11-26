"""
(De)serialization of a RepoKnowledgeGraph to a JSON-compatible dict.

Format (schema_version 3):
    {
      "schema_version": 3,
      "metadata": {...},
      "nodes": [{"id": ..., "class": "ChunkNode", "data": {<dataclass fields>}}, ...],
      "edges": [{"source": ..., "target": ..., "relation": ..., <other edge attributes>}, ...],
      "entities": {canonical_name: {"declaring_chunk_ids": [...], "calling_chunk_ids": [...],
                                     "type": [...], "dtype": ..., "aliases": [...],
                                     "attributes": {...}, "file_path": ..., "start_line": ..., "end_line": ...}}
    }

Version 3 added entity attributes and positions, chunk line ranges, resource nodes (endpoints,
configuration properties, tables, modules, dependencies, channels) and edge attributes.
Fields missing from a version 2 file take their default value.

Files written by older versions (no schema_version / no "entities") are still readable:
entities are then rebuilt from the entity and chunk nodes.
"""
import logging
from dataclasses import asdict
from typing import Any, Dict, Optional, Tuple

import networkx as nx

from .entity_registry import new_entity_record, normalize_entity_record
from .node import NODE_CLASSES, ChunkNode, EntityNode, Node, node_level

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 3


def _jsonable(value: Any) -> Any:
    if hasattr(value, 'tolist'):  # numpy arrays / scalars
        return value.tolist()
    return value


def node_to_dict(node_id: str, node: Node) -> dict:
    data = {k: _jsonable(v) for k, v in asdict(node).items()}
    data['id'] = node.id or node_id
    return {'id': node.id or node_id, 'class': node.__class__.__name__, 'data': data}


def graph_to_dict(graph: nx.DiGraph, entities: Dict[str, dict], metadata: Optional[dict] = None) -> dict:
    nodes = []
    for node_id, attrs in graph.nodes(data=True):
        if 'data' not in attrs:
            logger.warning(f"Node {node_id} has no 'data' attribute, skipping in serialization")
            continue
        nodes.append(node_to_dict(node_id, attrs['data']))

    edges = []
    for u, v, attrs in graph.edges(data=True):
        edge = {'source': u, 'target': v, 'relation': attrs.get('relation', '')}
        for key, value in attrs.items():
            if key not in ('relation', 'source', 'target'):
                edge[key] = list(value) if isinstance(value, (set, tuple)) else _jsonable(value)
        edges.append(edge)

    return {
        'schema_version': SCHEMA_VERSION,
        'metadata': dict(metadata or {}),
        'nodes': nodes,
        'edges': edges,
        'entities': {name: normalize_entity_record(info) for name, info in entities.items()},
    }


def node_from_dict(node_data: dict) -> Node:
    node_cls = NODE_CLASSES.get(str(node_data.get('class')), Node)
    kwargs = dict(node_data.get('data') or {})
    if not kwargs.get('id'):
        kwargs['id'] = node_data['id']
    kwargs['declared_entities'] = list(kwargs.get('declared_entities') or [])
    kwargs['called_entities'] = list(kwargs.get('called_entities') or [])
    if node_cls is ChunkNode and not kwargs.get('embedding'):
        kwargs['embedding'] = None  # "no embedding" is always None, never []
    return node_cls.from_kwargs(kwargs)


def _rebuild_legacy_entities(graph: nx.DiGraph) -> Dict[str, dict]:
    """Entities of a graph saved before schema_version 2 (they were not serialized)."""
    entities: Dict[str, dict] = {}

    for node_id, attrs in graph.nodes(data=True):
        node = attrs['data']
        if isinstance(node, EntityNode):
            record = entities.setdefault(node.name or node_id, new_entity_record())
            record['declaring_chunk_ids'] = list(node.declaring_chunk_ids)
            record['calling_chunk_ids'] = list(node.calling_chunk_ids)
            record['aliases'] = list(node.aliases)
            if node.entity_type:
                record['type'] = [node.entity_type]

    has_entity_nodes = bool(entities)
    for node_id, attrs in graph.nodes(data=True):
        node = attrs['data']
        if not isinstance(node, ChunkNode):
            continue
        for entity in node.declared_entities:
            name = entity.get('name') if isinstance(entity, dict) else entity
            if not name:
                continue
            record = entities.setdefault(name, new_entity_record())
            if not has_entity_nodes and node_id not in record['declaring_chunk_ids']:
                record['declaring_chunk_ids'].append(node_id)
            if isinstance(entity, dict):
                if entity.get('type') and entity['type'] not in record['type']:
                    record['type'].append(entity['type'])
                if entity.get('dtype'):
                    record['dtype'] = entity['dtype']
                for alias in [name] + list(entity.get('aliases') or []):
                    if alias and alias not in record['aliases']:
                        record['aliases'].append(alias)
        if not has_entity_nodes:
            for called_name in node.called_entities:
                if not called_name:
                    continue
                record = entities.setdefault(called_name, new_entity_record())
                if node_id not in record['calling_chunk_ids']:
                    record['calling_chunk_ids'].append(node_id)
    return entities


def graph_from_dict(data: dict) -> Tuple[nx.DiGraph, Dict[str, dict], dict]:
    """Return (graph, entities, metadata)."""
    graph = nx.DiGraph()
    if not any(n.get('id') == 'root' for n in data.get('nodes', [])):
        logger.warning('Root node not found in the data, creating one')
        graph.add_node('root', data=Node(id='root', name='root', node_type='root'), level=0)

    for node_data in data.get('nodes', []):
        node = node_from_dict(node_data)
        graph.add_node(node_data['id'], data=node, level=node_level(node))

    for edge in data.get('edges', []):
        source, target = edge['source'], edge['target']
        if source in graph and target in graph:
            attrs = {k: v for k, v in edge.items() if k not in ('source', 'target')}
            attrs.setdefault('relation', '')
            graph.add_edge(source, target, **attrs)
        else:
            logger.warning(f"Cannot add edge {source} -> {target}, nodes don't exist")

    if 'entities' in data:
        entities = {name: normalize_entity_record(info) for name, info in data['entities'].items()}
    else:
        entities = _rebuild_legacy_entities(graph)
    return graph, entities, dict(data.get('metadata') or {})
