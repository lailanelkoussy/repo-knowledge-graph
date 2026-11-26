"""
Saving and reloading a graph must not lose information.
"""
import json

import numpy as np

from repo_knowledge_graph.knowledge_graph import RepoKnowledgeGraph
from repo_knowledge_graph.node import ChunkNode, EntityNode, FileNode, Node


def _snapshot(kg):
    nodes = {n: (type(d['data']).__name__, d['level'], d['data']) for n, d in kg.graph.nodes(data=True)}
    edges = {(u, v, d.get('relation')) for u, v, d in kg.graph.edges(data=True)}
    return nodes, edges


def test_roundtrip_preserves_nodes_edges_entities(mini_kg, tmp_path):
    path = tmp_path / 'graph.json'
    mini_kg.save_graph_to_file(str(path))
    loaded = RepoKnowledgeGraph.load_graph_from_file(str(path), index_nodes=False)

    nodes_a, edges_a = _snapshot(mini_kg)
    nodes_b, edges_b = _snapshot(loaded)
    assert edges_a == edges_b
    assert nodes_a.keys() == nodes_b.keys()
    for node_id, (cls_a, level_a, data_a) in nodes_a.items():
        cls_b, level_b, data_b = nodes_b[node_id]
        assert (cls_a, level_a) == (cls_b, level_b), node_id
        assert data_a == data_b, node_id
    assert loaded.entities == mini_kg.entities
    assert loaded.entities['Cart']['aliases']  # aliases survive


def test_file_is_utf8_json_with_schema_version(mini_kg, tmp_path):
    mini_kg['shop/cart.py'].description = 'Panier — prix en €'
    path = tmp_path / 'graph.json'
    mini_kg.save_graph_to_file(str(path))
    data = json.loads(path.read_text(encoding='utf-8'))
    assert data['schema_version'] == 3
    assert 'entities' in data and 'metadata' in data
    loaded = RepoKnowledgeGraph.load_graph_from_file(str(path), index_nodes=False)
    assert loaded['shop/cart.py'].description == 'Panier — prix en €'


def test_numpy_embeddings_are_serializable(tmp_path):
    kg = RepoKnowledgeGraph()
    kg.graph.add_node('root', data=Node(id='root', name='root', node_type='root'), level=0)
    chunk = ChunkNode(id='a.py_0', name='a.py_0', path='a.py', content='x = 1',
                      embedding=np.array([0.1, 0.2], dtype=np.float32))
    kg.graph.add_node(chunk.id, data=chunk, level=2)
    kg.save_graph_to_file(str(tmp_path / 'g.json'))
    loaded = RepoKnowledgeGraph.load_graph_from_file(str(tmp_path / 'g.json'), index_nodes=False)
    assert loaded['a.py_0'].embedding == [np.float32(0.1).item(), np.float32(0.2).item()]


def test_missing_embeddings_are_none_and_get_recomputed(mini_kg, fake_model_service):
    data = mini_kg.to_dict()
    for node in data['nodes']:
        if node['class'] == 'ChunkNode':
            node['data']['embedding'] = []
    loaded = RepoKnowledgeGraph.from_dict(data, index_nodes=True, model_service=fake_model_service,
                                          code_index_kwargs={'index_type': 'hybrid', 'reuse': False})
    assert all(c.embedding for c in loaded.get_all_chunks())
    assert loaded.code_index.search('Cart', 2)


def test_legacy_format_is_still_readable():
    """Files produced before schema_version 2: no 'entities', entity nodes carry the information."""
    legacy = {
        'nodes': [
            {'id': 'root', 'class': 'Node', 'data': {'id': 'root', 'name': 'root', 'node_type': 'root'}},
            {'id': 'a.py', 'class': 'FileNode', 'data': {'id': 'a.py', 'name': 'a.py', 'node_type': 'file',
                                                         'path': 'a.py', 'content': 'def f(): pass\nf()',
                                                         'declared_entities': [], 'called_entities': []}},
            {'id': 'a.py_0', 'class': 'ChunkNode', 'data': {
                'id': 'a.py_0', 'name': 'a.py_0', 'node_type': 'chunk', 'path': 'a.py',
                'content': 'def f(): pass\nf()', 'order_in_file': 0, 'embedding': [],
                'declared_entities': [{'name': 'f', 'type': 'function', 'aliases': ['f', 'a.f']}],
                'called_entities': ['f'], 'unknown_future_field': 1}},
            {'id': 'f', 'class': 'EntityNode', 'data': {
                'id': 'f', 'name': 'f', 'node_type': 'entity', 'entity_type': '',
                'declaring_chunk_ids': ['a.py_0'], 'calling_chunk_ids': ['a.py_0'], 'aliases': ['f', 'a.f']}},
        ],
        'edges': [
            {'source': 'root', 'target': 'a.py', 'relation': 'contains'},
            {'source': 'a.py', 'target': 'a.py_0', 'relation': 'contains'},
            {'source': 'a.py_0', 'target': 'f', 'relation': 'declares'},
        ],
    }
    kg = RepoKnowledgeGraph.from_dict(legacy, index_nodes=False)
    assert isinstance(kg['a.py'], FileNode)
    assert kg['a.py_0'].embedding is None
    assert isinstance(kg['f'], EntityNode) and kg.graph.nodes['f']['level'] == 3
    assert kg.entities['f']['aliases'] == ['f', 'a.f']
    assert kg.entities['f']['type'] == ['function']
    assert kg.get_entity_by_alias('a.f') == 'f'
