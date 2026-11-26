"""
Contract-first APIs: OpenAPI operations matched to the controllers implementing the generated interfaces
(tests/fixtures/openapi_shop), Swagger 2 parsing, and the related spring_web fixes.
"""
import pytest
from fastmcp import Client

from repo_knowledge_graph import framework_queries as fq
from repo_knowledge_graph.enrichers.openapi import camelize, parse_operations
from repo_knowledge_graph.enrichers.spring_web import unwrap_response
from repo_knowledge_graph.knowledge_graph import RepoKnowledgeGraph
from repo_knowledge_graph.mcp_server import KnowledgeGraphMCPServer
from tests.conftest import FIXTURES

WEB = 'com.acme.catalog.web'


@pytest.fixture(scope='module')
def kg():
    return RepoKnowledgeGraph.from_path(str(FIXTURES / 'openapi_shop'), extract_entities=True, index_nodes=False)


def attrs(kg, node_id):
    return kg.graph.nodes[node_id]['data'].attributes


def test_camelize_follows_openapi_generator():
    assert camelize('owner-v2') == 'OwnerV2'
    assert camelize('pettypes') == 'Pettypes'
    assert camelize('catalog v2') == 'CatalogV2'
    assert camelize('') == 'Default'


def test_operations_are_matched_to_controller_methods(kg):
    assert 'openapi' in kg.metadata['enrichers']
    served = {e.id: e.attributes for e in kg.get_endpoints()}
    assert served['endpoint:GET /v2/products']['handler'] == f'{WEB}.ProductController::listProducts'
    assert served['endpoint:POST /v2/products']['handler'] == f'{WEB}.ProductController::createProduct'
    assert served['endpoint:GET /v2/products/{productId}']['handler'] == f'{WEB}.ProductController::getProduct'
    # no tag: interface named after the first path segment (HealthApi), no class-level @RequestMapping
    assert served['endpoint:GET /health']['handler'] == f'{WEB}.HealthController::health'
    assert served['endpoint:GET /health']['openapi']['match'] == 'tag'
    assert kg.graph.edges['endpoint:GET /v2/products', f'{WEB}.ProductController::listProducts']['relation'] == \
        'handled_by'


def test_specification_details(kg):
    create = attrs(kg, 'endpoint:POST /v2/products')
    assert create['source'] == 'openapi' and create['summary'] == 'Creates a product'
    assert create['request_body']['schema'] == 'Product'
    assert create['response_schema'] == 'Product'
    assert create['security'][0]['value'] == "hasRole('CATALOG_ADMIN')"  # from the implementation
    assert create['openapi']['operation_id'] == 'createProduct'
    assert create['openapi']['spec_file'] == 'src/main/resources/api/openapi.yaml'
    listing = attrs(kg, 'endpoint:GET /v2/products')
    assert listing['query_params'] == [{'name': 'category', 'required': False, 'type': 'string'}]
    assert listing['response_schema'] == 'array of Product'
    one = attrs(kg, 'endpoint:GET /v2/products/{productId}')
    assert one['path_params'] == [{'name': 'productId', 'required': True, 'type': 'integer'}]  # path-level $ref
    assert one['response_type'] == 'ResponseEntity<?>'


def test_unimplemented_operations(kg):
    deleted = attrs(kg, 'endpoint:DELETE /v2/products/{productId}')
    assert deleted['implemented'] is False and deleted['deprecated'] is True
    assert not list(kg.graph.successors('endpoint:DELETE /v2/products/{productId}'))


def test_mock_mvc_requests_are_never_external(kg):
    test_method = f'{WEB}.ProductControllerTest::getsOneProduct'
    assert kg.graph.edges[test_method, 'endpoint:GET /v2/products/{productId}']['via'] == 'mockmvc'
    assert attrs(kg, test_method)['test']['unmatched_requests'] == ['GET /v2/unknown']
    assert not [e for e in kg.get_endpoints(include_external=True) if e.attributes.get('external')]


def test_trace_from_a_spec_endpoint(kg):
    trace = fq.trace_flow(kg, 'GET /v2/products/7')
    assert [s['node'] for s in trace['steps']][:2] == [f'{WEB}.ProductController::getProduct',
                                                      'com.acme.catalog.service.ProductService::find']


async def test_mcp_get_endpoint_shows_the_contract(kg):
    server = KnowledgeGraphMCPServer(knowledge_graph=kg)
    async with Client(server.app) as client:
        result = await client.call_tool('get_endpoint', {'endpoint': 'DELETE /v2/products/{productId}'},
                                        raise_on_error=False)
    text = result.structured_content['text']
    assert 'Not implemented' in text and 'deleteProduct' in text


def test_swagger_2_and_non_spec_files(tmp_path):
    operations = parse_operations('swagger.json', {
        'swagger': '2.0', 'basePath': '/api',
        'paths': {'/pets': {'post': {'operationId': 'addPet', 'tags': ['pet'], 'parameters': [
            {'in': 'body', 'name': 'body', 'required': True, 'schema': {'$ref': '#/definitions/Pet'}}],
            'responses': {'200': {'description': 'ok', 'schema': {'$ref': '#/definitions/Pet'}}}}}},
    })
    assert operations[0]['path'] == '/api/pets'
    assert operations[0]['request_body'] == {'schema': 'Pet', 'required': True}
    assert operations[0]['responses'] == {'200': 'Pet'}

    (tmp_path / 'docker-compose.yml').write_text('services:\n  db:\n    image: postgres\n', encoding='utf-8')
    (tmp_path / 'broken.yaml').write_text('openapi: 3.0.0\npaths: [unclosed\n', encoding='utf-8')
    graph = RepoKnowledgeGraph.from_path(str(tmp_path), extract_entities=True, index_nodes=False)
    assert 'openapi' not in graph.metadata['enrichers']  # invalid spec skipped, the build does not fail


def test_unwrap_response():
    assert unwrap_response('ResponseEntity<List<OrderDto>>') == 'List<OrderDto>'
    assert unwrap_response('ResponseEntity<?>') == 'ResponseEntity<?>'
    assert unwrap_response('Mono<Void>') == 'Mono<Void>'
