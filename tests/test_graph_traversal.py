"""
Graph traversal helpers of RepoKnowledgeGraph: find_path and get_subgraph (on the mini_repo fixture).
"""
import pytest

from repo_knowledge_graph.knowledge_graph import RepoKnowledgeGraph


@pytest.fixture
def kg(mini_repo, fake_model_service):
    return RepoKnowledgeGraph.from_path(str(mini_repo), extract_entities=True, index_nodes=False,
                                        model_service=fake_model_service)


def test_find_path_follows_contains_edges(kg):
    result = kg.find_path('root', 'shop/cart.py_0', max_depth=10)

    assert result['length'] == 3
    assert [step['node_id'] for step in result['path']] == ['root', 'shop', 'shop/cart.py', 'shop/cart.py_0']
    assert [step['type'] for step in result['path']] == ['root', 'directory', 'file', 'chunk']
    assert [step.get('edge_to_next') for step in result['path']] == ['contains', 'contains', 'contains', None]
    assert [step['step'] for step in result['path']] == [0, 1, 2, 3]
    assert "Path from 'root' to 'shop/cart.py_0' (length: 3)" in result['text']
    assert '--[contains]-->' in result['text']


def test_find_path_follows_imports(kg):
    result = kg.find_path('shop/checkout.py', 'shop/cart.py')
    assert result['length'] == 1
    assert result['path'][0]['edge_to_next'] == 'imports'


def test_find_path_is_directed(kg):
    result = kg.find_path('shop/cart.py_0', 'root')
    assert result['path'] == []
    assert result['length'] == -1
    assert result['text'].startswith('No path found')


def test_find_path_respects_max_depth(kg):
    result = kg.find_path('root', 'shop/cart.py_0', max_depth=1)
    assert result['path'] == []
    assert result['length'] == 3
    assert 'exceeds max_depth of 1' in result['text']


def test_find_path_unknown_nodes(kg):
    assert kg.find_path('missing', 'root')['error_type'] == 'node_not_found'
    assert kg.find_path('root', 'missing')['error_type'] == 'node_not_found'


def test_get_subgraph(kg):
    result = kg.get_subgraph('shop/cart.py', depth=1)

    node_ids = {n['node_id'] for n in result['nodes']}
    # parent directory, own chunk, imported module and importing module
    assert node_ids == {'shop', 'shop/cart.py', 'shop/cart.py_0', 'shop/models.py', 'shop/checkout.py'}
    assert result['center_node_id'] == 'shop/cart.py'
    assert result['node_count'] == len(result['nodes'])
    assert result['edge_count'] == len(result['edges'])
    assert set(result['edge_by_relation']) == {'contains', 'imports'}
    assert {'source': 'shop/cart.py', 'target': 'shop/models.py', 'relation': 'imports'} in result['edges']
    assert "Subgraph around 'shop/cart.py' (depth: 1)" in result['text']

    deeper = kg.get_subgraph('shop/cart.py', depth=2)
    assert node_ids < {n['node_id'] for n in deeper['nodes']}
    assert {'declares', 'called_by'} <= set(deeper['edge_by_relation'])


def test_get_subgraph_with_edge_type_filter(kg):
    result = kg.get_subgraph('shop/cart.py', depth=2, edge_types=['contains'])

    assert {n['node_id'] for n in result['nodes']} >= {'root', 'shop', 'shop/cart.py', 'shop/cart.py_0'}
    assert result['edges'] and all(e['relation'] == 'contains' for e in result['edges'])
    assert set(result['edge_by_relation']) == {'contains'}
    assert result['edge_types_filter'] == ['contains']
    assert 'Edge types filter: contains' in result['text']


def test_get_subgraph_unknown_node(kg):
    assert kg.get_subgraph('missing')['error_type'] == 'node_not_found'


def test_traversal_after_save_and_load(kg, tmp_path, fake_model_service):
    path = tmp_path / 'kg.json'
    kg.save_graph_to_file(str(path))
    loaded = RepoKnowledgeGraph.load_graph_from_file(str(path), index_nodes=False,
                                                     model_service=fake_model_service)

    assert loaded.find_path('root', 'shop/cart.py_0')['path'] == kg.find_path('root', 'shop/cart.py_0')['path']
    assert loaded.get_subgraph('shop/cart.py', depth=1)['node_count'] == \
        kg.get_subgraph('shop/cart.py', depth=1)['node_count']
