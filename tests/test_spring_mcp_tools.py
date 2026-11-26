"""
MCP tools over the Spring Boot enrichment, called through an in-memory FastMCP client (nominal and error cases).
"""
import pytest
from fastmcp import Client

from repo_knowledge_graph.mcp_server import KnowledgeGraphMCPServer

PKG = 'com.acme.shop'
SECRETS = ('s3cr3t-dev-pass', 'dev-api-key-123', 'prod-client-secret-xyz', 'pr0d-url-pass')


@pytest.fixture(scope='module')
def server(spring_kg):
    return KnowledgeGraphMCPServer(knowledge_graph=spring_kg)


async def call(server, tool, **args):
    async with Client(server.app) as client:
        result = await client.call_tool(tool, args, raise_on_error=False)
    assert result.structured_content is not None, f'{tool} returned no structured content'
    return result.structured_content


async def test_project_overview(server):
    data = await call(server, 'get_project_overview')
    assert data['spring_boot_version'] == '3.3.4'
    assert 'Endpoints: 6 served' in data['text']


async def test_list_endpoints(server):
    data = await call(server, 'list_endpoints', http_method='GET', path_prefix='/api/orders')
    assert [e['id'] for e in data['endpoints']] == ['endpoint:GET /api/orders', 'endpoint:GET /api/orders/{id}']
    everything = await call(server, 'list_endpoints', include_external=True, limit=2, offset=0)
    assert everything['total'] == 8 and everything['has_more'] is True
    assert 'POST /api/orders -> com.acme.shop.web.OrderController::create' in (
        await call(server, 'list_endpoints'))['text']


async def test_get_endpoint(server):
    data = await call(server, 'get_endpoint', endpoint='GET /api/orders/42')
    assert data['id'] == 'endpoint:GET /api/orders/{id}'
    assert data['handlers'][0]['location'].endswith('OrderController.java:28')
    assert data['callers'][0]['via'] in ('rest_template', 'mockmvc')
    assert 'Handler: com.acme.shop.web.OrderController::get' in data['text']
    error = await call(server, 'get_endpoint', endpoint='GET /missing')
    assert error['error_type'] == 'node_not_found'


async def test_trace_flow(server):
    data = await call(server, 'trace_flow', start='POST /api/orders')
    assert 'table:orders' in data['text'] and 'channel:kafka:order-created' in data['text']
    small = await call(server, 'trace_flow', start='POST /api/orders', limit=3)
    assert small['truncated'] is True and len(small['steps']) == 3
    trivial = await call(server, 'trace_flow', start=f'{PKG}.web.OrderController::create', include_trivial=True)
    assert any(s['kind'] == 'variable' for s in trivial['steps'])
    bad = await call(server, 'trace_flow', start='POST /api/orders', max_depth=20)
    assert bad['error_type'] == 'invalid_input'


async def test_beans_and_dependencies(server):
    beans = await call(server, 'list_beans', stereotype='service')
    assert {b['id'] for b in beans['beans']} == {f'{PKG}.service.DefaultOrderService', f'{PKG}.service.LegacyOrderService'}
    deps = await call(server, 'get_bean_dependencies', bean='OrderController')
    assert deps['injects'][0]['bean'] == f'{PKG}.service.DefaultOrderService'
    assert deps['injects'][0]['confidence'] == 'primary'
    into = await call(server, 'get_bean_dependencies', bean='OrderRepository', direction='injected_into', depth=2)
    assert {i['bean'] for i in into['injected_into']} == {f'{PKG}.service.DefaultOrderService', f'{PKG}.web.OrderController'}
    unresolved = await call(server, 'get_bean_dependencies', bean='DefaultOrderService', direction='injects')
    assert {u['name'] for u in unresolved['unresolved_injections']} == {'events', 'kafkaTemplate'}
    missing = await call(server, 'get_bean_dependencies', bean='NoSuchBean')
    assert missing['error_type'] == 'node_not_found'


async def test_implementations_and_annotations(server):
    impl = await call(server, 'find_implementations', type_name='OrderService')
    assert len(impl['implementations']) == 2
    found = await call(server, 'find_by_annotation', annotation='PreAuthorize')
    assert found['results'][0]['id'] == f'{PKG}.web.OrderController::create'
    assert found['results'][0]['args'] == {'value': "hasRole('USER')"}


async def test_data_model(server):
    data = await call(server, 'get_data_model')
    assert {e['id'] for e in data['entities']} >= {f'{PKG}.domain.Order', f'{PKG}.domain.OrderLine'}
    assert 'drift' in data['text']
    focused = await call(server, 'get_data_model', entity='OrderLine')
    assert {e['id'] for e in focused['entities']} == {f'{PKG}.domain.Order', f'{PKG}.domain.OrderLine'}


async def test_config(server):
    data = await call(server, 'get_config', prefix='app.payment')
    keys = [p['key'] for p in data['properties']]
    assert keys == ['app.payment.base-url', 'app.payment.client-secret', 'app.payment.retries',
                    'app.payment.timeout-ms']
    prod = await call(server, 'get_config', key='app.payment.timeout-ms', profile='prod')
    assert prod['properties'][0]['values'] == {'prod': '5000'}
    undefined = await call(server, 'get_config', undefined_only=True)
    assert {p['key'] for p in undefined['properties']} == {'app.payment.retries', 'app.inventory.url'}


async def test_messaging_dependencies_and_tests(server):
    messaging = await call(server, 'list_messaging')
    assert 'channel:kafka:order-created' in messaging['text']
    assert messaging['scheduled_jobs'][0]['schedule'] == {'cron': '0 0 3 * * *'}
    deps = await call(server, 'list_dependencies', module='shop-core', name_query='kafka')
    assert [d['dependency'] for d in deps['dependencies']] == ['dep:org.springframework.kafka:spring-kafka']
    tests = await call(server, 'find_tests_for', entity='OrderController')
    assert tests['tests'][0]['kind'] == 'web_slice'


async def test_existing_tools_expose_the_enrichment(server):
    info = await call(server, 'get_node_info', node_id=f'{PKG}.web.OrderController::create')
    assert info['location'].endswith('OrderController.java:40')
    assert info['attributes']['spring']['endpoints'] == ['endpoint:POST /api/orders']
    assert '@PreAuthorize' in info['text']
    definition = await call(server, 'go_to_definition', entity_name='OrderController')
    assert definition['location'].endswith('OrderController.java:16')
    usages = await call(server, 'find_usages', entity_name='OrderService::find')
    assert f'{PKG}.web.OrderController::get' in usages['typed_usages']['calls']
    stats = await call(server, 'get_graph_stats')
    assert stats['node_types']['endpoint'] == 8 and stats['edge_relations']['injects'] >= 4
    listed = await call(server, 'list_nodes_by_type', node_type='table')
    assert {r['id'] for r in listed['results']} == {'table:orders', 'table:order_line'}
    edges = await call(server, 'get_node_edges', node_id=f'{PKG}.web.OrderController', relation='injects')
    assert edges['outgoing'][0]['confidence'] == 'primary'


async def test_no_secret_in_tool_responses(server):
    responses = [
        await call(server, 'get_config'),
        await call(server, 'get_node_info', node_id='shop-api/src/main/resources/application.yml', include_content=True),
        await call(server, 'get_node_info', node_id='shop-api/src/main/resources/application-prod.properties',
                   include_content=True),
    ]
    text = repr(responses)
    for secret in SECRETS:
        assert secret not in text
