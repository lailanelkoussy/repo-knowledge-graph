"""
Every MCP tool, called through an in-memory FastMCP client (nominal and error cases).
"""
import shutil
import subprocess

import pytest
from fastmcp import Client

from repo_knowledge_graph.knowledge_graph import RepoKnowledgeGraph
from repo_knowledge_graph.mcp_server import KnowledgeGraphMCPServer

EXPECTED_TOOLS = {
    'get_node_info', 'get_node_edges', 'search_nodes', 'get_graph_stats', 'list_nodes_by_type', 'get_neighbors',
    'go_to_definition', 'find_usages', 'get_file_structure', 'get_related_chunks', 'list_all_entities',
    'diff_chunks', 'print_tree', 'search_by_type_and_name', 'get_chunk_context', 'get_file_stats',
    'search_file_names_by_regex', 'find_path', 'get_subgraph', 'get_file_history', 'get_commit_info',
    'diff_file_versions',
    # framework enrichment (framework_tools.py)
    'get_project_overview', 'list_endpoints', 'get_endpoint', 'trace_flow', 'list_beans', 'get_bean_dependencies',
    'find_implementations', 'find_by_annotation', 'get_data_model', 'get_config', 'list_messaging',
    'list_dependencies', 'find_sinks', 'find_sources', 'trace_taint', 'find_callers', 'get_security_posture',
    'list_attack_surface', 'list_findings', 'export_sarif', 'get_code', 'grep_code', 'get_dependency_usage',
    'check_dependencies', 'find_tests_for',
}


@pytest.fixture
def server(mini_kg):
    return KnowledgeGraphMCPServer(knowledge_graph=mini_kg)


async def call(server, tool, **args):
    async with Client(server.app) as client:
        result = await client.call_tool(tool, args, raise_on_error=False)
    assert result.structured_content is not None, f'{tool} returned no structured content'
    return result.structured_content


async def test_tool_list(server):
    async with Client(server.app) as client:
        tools = {t.name for t in await client.list_tools()}
    assert tools == EXPECTED_TOOLS


async def test_get_node_info_accepts_entity_aliases(server):
    data = await call(server, 'get_node_info', node_id='shop.cart.Cart')
    assert data['node_id'] == 'Cart' and data['entity_type'] == 'class'
    full = await call(server, 'get_node_info', node_id='shop/cart.py', include_content=True)
    assert 'def compute_total' in full['content']


async def test_unknown_node_suggests_close_matches(server):
    data = await call(server, 'get_node_info', node_id='shop/crat.py')
    assert data['error_type'] == 'node_not_found'
    assert 'shop/cart.py' in data['error']


async def test_get_node_edges_filter_and_pagination(server):
    data = await call(server, 'get_node_edges', node_id='shop/checkout.py', relation='imports')
    assert {e['target'] for e in data['outgoing']} == {'shop/cart.py', 'shop/models.py'}
    page = await call(server, 'get_node_edges', node_id='root', limit=1)
    assert len(page['outgoing']) == 1 and page['has_more']


async def test_search_nodes(server):
    data = await call(server, 'search_nodes', query='compute_total', limit=3)
    assert data['count'] >= 1
    assert {'id', 'path', 'content', 'score', 'declared_entities'} <= data['results'][0].keys()


async def test_search_nodes_without_index(mini_repo):
    kg = RepoKnowledgeGraph.from_path(str(mini_repo), index_nodes=False)
    data = await call(KnowledgeGraphMCPServer(knowledge_graph=kg), 'search_nodes', query='x')
    assert data['error_type'] == 'invalid_input'


async def test_get_graph_stats(server, mini_kg):
    data = await call(server, 'get_graph_stats')
    assert data['total_nodes'] == mini_kg.graph.number_of_nodes()
    assert data['edge_relations']['imports'] >= 4
    assert data['has_code_index'] is True


async def test_list_nodes_by_type(server):
    files = await call(server, 'list_nodes_by_type', node_type='file', limit=50)
    assert files['total'] == 10
    classes = await call(server, 'list_nodes_by_type', node_type='entity', entity_type='class', limit=50)
    assert {'Cart', 'Item', 'DiscountedItem'} <= {r['id'] for r in classes['results']}
    bad = await call(server, 'list_nodes_by_type', node_type='file', limit=0)
    assert bad['error_type'] == 'invalid_input'


async def test_get_neighbors(server):
    data = await call(server, 'get_neighbors', node_id='Cart')
    relations = {(n['id'], n['relation'], n['direction']) for n in data['neighbors']}
    assert ('shop/cart.py_0', 'declares', 'incoming') in relations
    assert ('shop/checkout.py_0', 'called_by', 'outgoing') in relations


async def test_go_to_definition_and_find_usages(server):
    definition = await call(server, 'go_to_definition', entity_name='compute_total')
    assert definition['declarations'][0]['file_path'] == 'shop/cart.py'
    qualified = await call(server, 'go_to_definition', entity_name='shop.cart.compute_total')
    assert qualified['entity_name'] == 'compute_total'
    usages = await call(server, 'find_usages', entity_name='compute_total')
    assert 'shop/checkout.py' in {u['file_path'] for u in usages['usages']}
    missing = await call(server, 'go_to_definition', entity_name='NoSuchThing')
    assert missing['error_type'] == 'entity_not_found'


async def test_get_file_structure(server):
    data = await call(server, 'get_file_structure', file_path='shop/checkout.py')
    assert data['language'] == 'python'
    assert set(data['imports']) == {'shop/cart.py', 'shop/models.py'}
    not_a_file = await call(server, 'get_file_structure', file_path='shop')
    assert not_a_file['error_type'] == 'invalid_input'


async def test_get_related_chunks_both_directions(server):
    calls = await call(server, 'get_related_chunks', chunk_id='shop/checkout.py_0', relation_type='calls')
    assert 'shop/cart.py_0' in {c['id'] for c in calls['related_chunks']}
    called_by = await call(server, 'get_related_chunks', chunk_id='shop/cart.py_0', relation_type='called_by')
    assert 'shop/checkout.py_0' in {c['id'] for c in called_by['related_chunks']}


async def test_list_all_entities(server):
    data = await call(server, 'list_all_entities', entity_type='class', declared_only=True, limit=100)
    names = {e['name'] for e in data['entities']}
    assert {'Cart', 'Item', 'ApiClient', 'com.acme.Inventory'} <= names
    assert all(e['declaration_count'] > 0 for e in data['entities'])


async def test_diff_chunks(server):
    data = await call(server, 'diff_chunks', node_id_1='shop/cart.py_0', node_id_2='shop/checkout.py_0')
    assert data['has_differences']
    same = await call(server, 'diff_chunks', node_id_1='shop/cart.py_0', node_id_2='shop/cart.py_0')
    assert same['text'] == 'No differences.'


async def test_print_tree_follows_contains_only(server):
    data = await call(server, 'print_tree', max_depth=5)
    assert '- cart.py (file)' in data['text']
    assert '(entity)' not in data['text'] and '(chunk)' not in data['text']
    with_chunks = await call(server, 'print_tree', root_id='shop', include_chunks=True)
    assert '(chunk)' in with_chunks['text']


async def test_search_by_type_and_name(server):
    data = await call(server, 'search_by_type_and_name', node_type='entity', name_query='cart',
                      entity_type='class')
    assert {m['id'] for m in data['matches']} == {'Cart'}


async def test_get_chunk_context(server):
    data = await call(server, 'get_chunk_context', node_id='shop/cart.py_0')
    assert data['previous_chunk'] is None
    assert 'File: shop/cart.py' in data['text']
    wrong = await call(server, 'get_chunk_context', node_id='shop/cart.py')
    assert wrong['error_type'] == 'invalid_input'


async def test_get_file_stats(server):
    data = await call(server, 'get_file_stats', path='shop/cart.py')
    assert data['nodes'][0]['chunks_count'] == 1
    directory = await call(server, 'get_file_stats', path='shop')
    assert directory['nodes'][0]['node_type'] == 'directory'


async def test_search_file_names_by_regex(server):
    data = await call(server, 'search_file_names_by_regex', pattern=r'\.ts$')
    assert {m['node_id'] for m in data['matches']} == {'web/src/api.ts', 'web/src/format.ts'}
    invalid = await call(server, 'search_file_names_by_regex', pattern='(')
    assert invalid['error_type'] == 'invalid_input'


async def test_find_path_and_subgraph(server):
    path = await call(server, 'find_path', source_id='shop/checkout.py', target_id='shop/models.py')
    assert path['length'] == 1
    missing = await call(server, 'find_path', source_id='nope', target_id='root')
    assert missing['error_type'] == 'node_not_found'
    sub = await call(server, 'get_subgraph', node_id='DiscountedItem', depth=1, edge_types=['inherits'])
    assert {e['relation'] for e in sub['edges']} == {'inherits'}
    too_deep = await call(server, 'get_subgraph', node_id='root', depth=10)
    assert too_deep['error_type'] == 'invalid_input'


async def test_history_tools_without_history(server):
    data = await call(server, 'get_file_history', file_path='shop/cart.py')
    assert data['error_type'] == 'invalid_input'
    diff = await call(server, 'diff_file_versions', file_path='shop/cart.py', rev_a='HEAD~1')
    assert diff['error_type'] == 'invalid_input'


@pytest.mark.skipif(shutil.which('git') is None, reason='git not installed')
async def test_history_tools(mini_repo):
    def git(*args):
        subprocess.run(['git', '-c', 'user.name=T', '-c', 'user.email=t@example.com', '-C', str(mini_repo), *args],
                       check=True, capture_output=True)

    git('init', '-q')
    git('add', '.')
    git('commit', '-q', '-m', 'first')
    (mini_repo / 'shop' / 'cart.py').write_text('def compute_total(items):\n    return 0\n')
    git('commit', '-q', '-am', 'simplify cart')

    kg = RepoKnowledgeGraph.from_path(str(mini_repo), index_nodes=False, include_git_history=True)
    server = KnowledgeGraphMCPServer(knowledge_graph=kg)
    history = await call(server, 'get_file_history', file_path='shop/cart.py')
    assert [c['message'] for c in history['commits']] == ['simplify cart', 'first']
    commit = await call(server, 'get_commit_info', commit=history['commits'][0]['hash'])
    assert commit['files_changed'] == ['shop/cart.py']
    diff = await call(server, 'diff_file_versions', file_path='shop/cart.py', rev_a='HEAD~1', rev_b='HEAD')
    assert diff['has_differences'] and '-class Cart:' in diff['diff']
    injection = await call(server, 'diff_file_versions', file_path='shop/cart.py', rev_a='--output=x')
    assert injection['error_type'] == 'invalid_input'


def test_server_from_file(mini_kg, tmp_path):
    path = tmp_path / 'g.json'
    mini_kg.save_graph_to_file(str(path))
    server = KnowledgeGraphMCPServer.from_file(str(path), index_nodes=False)
    assert server.knowledge_graph.entities == mini_kg.entities
