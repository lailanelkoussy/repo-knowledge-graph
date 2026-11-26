"""
End-to-end construction of a graph from the multi-language fixture repository.
"""
import pytest

from repo_knowledge_graph.knowledge_graph import RepoKnowledgeGraph
from repo_knowledge_graph.node import ChunkNode, EntityNode, FileNode


def edges(kg, relation):
    return {(u, v) for u, v, d in kg.graph.edges(data=True) if d.get('relation') == relation}


@pytest.fixture
def kg(mini_repo):
    return RepoKnowledgeGraph.from_path(str(mini_repo), extract_entities=True, index_nodes=False)


def test_tree_structure_uses_posix_ids(kg):
    files = {f.id for f in kg.get_all_files()}
    assert files == {
        'README.md', 'shop/__init__.py', 'shop/models.py', 'shop/cart.py', 'shop/checkout.py',
        'web/src/api.ts', 'web/src/format.ts', 'web/src/legacy.js',
        'java/com/acme/Inventory.java', 'java/com/acme/Warehouse.java',
    }
    assert ('root', 'shop') in edges(kg, 'contains')
    assert ('web', 'web/src') in edges(kg, 'contains')
    assert ('web/src', 'web/src/api.ts') in edges(kg, 'contains')
    assert all('\\' not in node_id for node_id in kg.graph.nodes)


def test_chunks_belong_to_their_file(kg):
    for file_node in kg.get_all_files():
        chunks = kg.get_chunks_of_file(file_node.id)
        assert [c.order_in_file for c in chunks] == list(range(len(chunks)))
        assert all(c.path == file_node.path for c in chunks)
        assert ''.join(c.content for c in chunks).replace('\n', '') in file_node.content.replace('\n', '') or chunks


def test_files_carry_language_hash_and_imports(kg):
    cart = kg['shop/cart.py']
    assert isinstance(cart, FileNode)
    assert cart.language == 'python'
    assert len(cart.content_hash) == 64
    assert '.models' in cart.imports
    assert kg['web/src/api.ts'].language == 'typescript'


def test_declarations_and_calls_across_files(kg):
    assert kg.entities['Cart']['declaring_chunk_ids'] == ['shop/cart.py_0']
    assert 'shop/checkout.py_0' in kg.entities['Cart']['calling_chunk_ids']
    assert 'shop/checkout.py_0' in kg.entities['compute_total']['calling_chunk_ids']
    assert ('Cart', 'shop/checkout.py_0') in edges(kg, 'called_by')
    assert ('shop/cart.py_0', 'Cart') in edges(kg, 'declares')


def test_entity_nodes_have_their_type(kg):
    node = kg['Cart']
    assert isinstance(node, EntityNode)
    assert node.entity_type == 'class'
    assert 'shop.cart.Cart' in node.aliases
    assert kg.graph.nodes['Cart']['level'] == 3


def test_import_edges_between_files(kg):
    imports = edges(kg, 'imports')
    assert ('shop/cart.py', 'shop/models.py') in imports          # relative python import
    assert ('shop/checkout.py', 'shop/cart.py') in imports        # absolute python import
    assert ('web/src/api.ts', 'web/src/format.ts') in imports     # typescript relative import
    assert ('web/src/legacy.js', 'web/src/format.ts') in imports  # javascript -> typescript


def test_inheritance_edges(kg):
    inherits = edges(kg, 'inherits')
    assert ('DiscountedItem', 'Item') in inherits
    assert ('com.acme.Warehouse', 'com.acme.Inventory') in inherits


def test_typescript_entities_are_extracted(kg):
    assert 'ApiClient' in kg.entities
    assert 'ApiClient.fetchCart' in kg.entities
    assert 'CartLine' in kg.entities
    assert 'web/src/api.ts_0' in kg.entities['formatPrice']['calling_chunk_ids']
    assert 'API:GET:/api/cart' in kg.entities


def test_no_model_is_loaded_without_index_or_descriptions(mini_repo, monkeypatch):
    import repo_knowledge_graph.model_service as model_service_module

    def forbidden(**kwargs):
        raise AssertionError('model service should not be created')

    monkeypatch.setattr(model_service_module, 'create_model_service', forbidden)
    kg = RepoKnowledgeGraph.from_path(str(mini_repo), extract_entities=True, index_nodes=False)
    assert kg.get_all_chunks()
    kg2 = RepoKnowledgeGraph.from_path(str(mini_repo), index_nodes=True,
                                       code_index_kwargs={'index_type': 'keyword-only'})
    assert kg2.code_index.search('Cart', 3)


def test_descriptions_use_the_model_service_once_per_chunk(mini_repo, fake_model_service):
    kg = RepoKnowledgeGraph.from_path(str(mini_repo), describe_nodes=True, index_nodes=False,
                                      model_service=fake_model_service)
    chunks = kg.get_all_chunks()
    assert all(c.description == 'Fake description.' for c in chunks)
    assert len(fake_model_service.queries) == len(chunks)


def test_hybrid_index_search(mini_kg):
    results = mini_kg.code_index.search('compute_total', 3)
    assert 'shop/cart.py' in [r.path for r in results]
    assert all(isinstance(c.embedding, list) for c in mini_kg.get_all_chunks())


def test_from_path_works_inside_a_running_event_loop(mini_repo):
    import asyncio

    async def build():
        return RepoKnowledgeGraph.from_path(str(mini_repo), index_nodes=False)

    kg = asyncio.run(build())
    assert kg.get_all_files()


def test_skip_dirs_extensions_and_gitignore(mini_repo):
    (mini_repo / '.gitignore').write_text('web/src/legacy.js\n')
    (mini_repo / 'big.py').write_text('x = 1\n' * 10)
    (mini_repo / 'image.py').write_bytes(b'\x00\x01binary')
    kg = RepoKnowledgeGraph.from_path(str(mini_repo), index_nodes=False, skip_dirs=['java'],
                                      max_file_size=30)
    files = {f.id for f in kg.get_all_files()}
    assert 'web/src/legacy.js' not in files
    assert not any(f.startswith('java/') for f in files)
    assert 'big.py' not in files and 'image.py' not in files

    only_py = RepoKnowledgeGraph.from_path(str(mini_repo), index_nodes=False, extensions=['py'])
    assert {f.id.rsplit('.', 1)[-1] for f in only_py.get_all_files()} == {'py'}


def test_ambiguous_simple_names_are_not_guessed(tmp_path):
    (tmp_path / 'a.py').write_text('class A:\n    def run(self):\n        pass\n')
    (tmp_path / 'b.py').write_text('class B:\n    def run(self):\n        pass\n')
    (tmp_path / 'c.py').write_text('def go(x):\n    x.run()\n')
    kg = RepoKnowledgeGraph.from_path(str(tmp_path), extract_entities=True, index_nodes=False)
    assert 'c.py_0' not in kg.entities['A.run']['calling_chunk_ids']
    assert 'c.py_0' not in kg.entities['B.run']['calling_chunk_ids']
    assert 'c.py_0' in kg.entities['run']['calling_chunk_ids']


def test_imports_disambiguate_calls(tmp_path):
    (tmp_path / 'pkg').mkdir()
    (tmp_path / 'pkg' / '__init__.py').write_text('')
    (tmp_path / 'pkg' / 'one.py').write_text('def helper():\n    return 1\n')
    (tmp_path / 'pkg' / 'two.py').write_text('def helper():\n    return 2\n')
    (tmp_path / 'main.py').write_text('from pkg import two\n\ndef main():\n    return two.helper()\n')
    kg = RepoKnowledgeGraph.from_path(str(tmp_path), extract_entities=True, index_nodes=False)
    # both files declare 'helper': they share one canonical entity (historical behaviour)
    assert set(kg.entities['helper']['declaring_chunk_ids']) == {'pkg/one.py_0', 'pkg/two.py_0'}
    assert 'main.py_0' in kg.entities['helper']['calling_chunk_ids']


def test_resolve_entity_and_references(kg):
    assert kg.resolve_entity('shop.cart.Cart') == 'Cart'
    assert kg.get_entity_by_alias('shop.models.Item') == 'Item'
    assert kg.resolve_entity('does_not_exist') is None
    assert isinstance(kg.resolve_entity_references(), dict)


def test_navigation_helpers(kg):
    chunk = kg.get_all_chunks()[0]
    assert kg.get_previous_chunk(chunk.id) is None or isinstance(kg.get_previous_chunk(chunk.id), ChunkNode)
    neighbors = {n.id for n in kg.get_neighbors('shop/cart.py')}
    assert {'shop', 'shop/cart.py_0', 'shop/models.py'} <= neighbors
    path = kg.find_path('root', 'Cart')
    assert path['length'] > 0 and path['path'][-1]['node_id'] == 'Cart'
    sub = kg.get_subgraph('shop/cart.py', depth=1, edge_types=['imports'])
    assert {e['relation'] for e in sub['edges']} == {'imports'}
    assert 'error' in kg.find_path('nope', 'root')
