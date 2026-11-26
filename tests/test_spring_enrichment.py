"""
End-to-end enrichment of the Spring Boot fixture (tests/fixtures/spring_shop): build modules, beans and
injection, endpoints, persistence, messaging, configuration (with secret masking) and tests.
"""
import json

import pytest

from repo_knowledge_graph import framework_queries as fq
from repo_knowledge_graph.enrichers import available_enrichers, select_enrichers
from repo_knowledge_graph.knowledge_graph import RepoKnowledgeGraph
from repo_knowledge_graph.node import ResourceNode

PKG = 'com.acme.shop'
SECRETS = ('s3cr3t-dev-pass', 'dev-api-key-123', 'prod-client-secret-xyz', 'pr0d-url-pass')


def edge(kg, source, target):
    assert kg.graph.has_edge(source, target), f'missing edge {source} -> {target}'
    return kg.graph.edges[source, target]


def edges(kg, relation):
    return {(u, v) for u, v, d in kg.graph.edges(data=True) if d.get('relation') == relation}


def attrs(kg, node_id):
    return kg.graph.nodes[node_id]['data'].attributes


# ---------------------------------------------------------------------- selection

def test_enricher_selection():
    assert select_enrichers('none') == []
    assert select_enrichers('auto') == available_enrichers()
    assert select_enrichers('spring_web,build') == ['build', 'spring_web']
    with pytest.raises(ValueError):
        select_enrichers('nope')


def test_every_spring_enricher_applies(spring_kg):
    assert set(spring_kg.metadata['enrichers']) >= {'build', 'spring_config', 'spring_beans', 'spring_web',
                                                     'spring_data_jpa', 'spring_messaging', 'spring_tests'}
    assert 'web_routes' not in spring_kg.metadata['enrichers']  # no Python / Rust routes in this repository


def test_no_enrichment_keeps_the_generic_graph():
    from tests.conftest import SPRING_SHOP
    kg = RepoKnowledgeGraph.from_path(str(SPRING_SHOP), extract_entities=True, index_nodes=False, enrichers='none')
    assert kg.metadata['enrichers'] == []
    assert not [n for n in kg if isinstance(n, ResourceNode)]
    relations = {d.get('relation') for _, _, d in kg.graph.edges(data=True)}
    assert relations <= {'contains', 'declares', 'called_by', 'imports', 'inherits', 'calls', 'uses_type',
                         'implemented_by'}


# ---------------------------------------------------------------------- build

def test_modules_and_dependencies(spring_kg):
    modules = {n.key: n.attributes for n in spring_kg.get_resources('module')}
    assert set(modules) == {'shop-parent', 'shop-core', 'shop-api'}
    assert modules['shop-api']['spring_boot_version'] == '3.3.4'   # inherited from the parent pom
    assert modules['shop-core']['java_version'] == '21'
    assert edge(spring_kg, 'module:shop-api', 'module:shop-core')['relation'] == 'depends_on'
    kafka = edge(spring_kg, 'module:shop-core', 'dep:org.springframework.kafka:spring-kafka')
    assert kafka['scope'] == 'compile'
    feign = edge(spring_kg, 'module:shop-core', 'dep:org.springframework.cloud:spring-cloud-starter-openfeign')
    assert feign['version'] == '4.1.3'
    assert edge(spring_kg, 'module:shop-core', 'shop-core')['relation'] == 'builds'


def test_gradle_build(tmp_path):
    (tmp_path / 'settings.gradle').write_text("rootProject.name = 'demo'\ninclude 'app', 'lib'\n", encoding='utf-8')
    (tmp_path / 'app').mkdir()
    (tmp_path / 'lib').mkdir()
    (tmp_path / 'app' / 'build.gradle.kts').write_text('''plugins {
    id("org.springframework.boot") version "3.2.5"
}
java { toolchain { languageVersion = JavaLanguageVersion.of(17) } }
dependencies {
    implementation("org.springframework.boot:spring-boot-starter-web")
    implementation(project(":lib"))
    testImplementation("org.junit.jupiter:junit-jupiter:5.10.2")
}
''', encoding='utf-8')
    (tmp_path / 'lib' / 'build.gradle').write_text("dependencies { api 'com.google.guava:guava:33.0-jre' }\n",
                                                    encoding='utf-8')
    kg = RepoKnowledgeGraph.from_path(str(tmp_path), extract_entities=True, index_nodes=False)
    app = kg.graph.nodes['module:app']['data'].attributes
    assert app['spring_boot_version'] == '3.2.5' and app['java_version'] == '17'
    assert edge(kg, 'module:app', 'module:lib')['relation'] == 'depends_on'
    assert edge(kg, 'module:app', 'dep:org.junit.jupiter:junit-jupiter')['scope'] == 'testImplementation'
    assert edge(kg, 'module:lib', 'dep:com.google.guava:guava')['version'] == '33.0-jre'


# ---------------------------------------------------------------------- beans

def test_bean_stereotypes(spring_kg):
    beans = {b['id']: b for b in fq.list_beans(spring_kg)}
    assert beans[f'{PKG}.service.DefaultOrderService']['stereotype'] == 'service'
    assert beans[f'{PKG}.service.DefaultOrderService']['primary'] is True
    assert beans[f'{PKG}.service.LegacyOrderService']['bean_name'] == 'legacyOrders'
    assert beans[f'{PKG}.web.OrderController']['stereotype'] == 'rest_controller'
    assert beans[f'{PKG}.repository.OrderRepository']['stereotype'] == 'repository'  # Spring Data, no annotation
    assert beans[f'{PKG}.config.SecurityConfig::restTemplate']['stereotype'] == 'bean_method'
    assert beans[f'{PKG}.config.PaymentProperties']['stereotype'] == 'configuration_properties'
    assert beans[f'{PKG}.client.PaymentClient']['stereotype'] == 'feign_client'


def test_injection_resolution(spring_kg):
    controller = edge(spring_kg, f'{PKG}.web.OrderController', f'{PKG}.service.DefaultOrderService')
    assert controller['relation'] == 'injects' and controller['confidence'] == 'primary'
    assert controller['via'] == 'constructor'
    qualified = edge(spring_kg, f'{PKG}.client.InventoryGateway', f'{PKG}.service.LegacyOrderService')
    assert qualified['confidence'] == 'qualifier' and qualified['via'] == 'field'
    bean_method = edge(spring_kg, f'{PKG}.client.InventoryGateway', f'{PKG}.config.SecurityConfig::restTemplate')
    assert bean_method['confidence'] == 'exact'
    lombok = edge(spring_kg, f'{PKG}.service.DefaultOrderService', f'{PKG}.repository.OrderRepository')
    assert lombok['via'] == 'constructor'  # @RequiredArgsConstructor + final field
    injections = attrs(spring_kg, f'{PKG}.service.DefaultOrderService')['spring']['injections']
    unresolved = {i['name'] for i in injections if i.get('unresolved')}
    assert unresolved == {'events', 'kafkaTemplate'}  # provided by Spring Boot, not by the repository


def test_plain_interface_annotated_repository_is_not_a_bean(tmp_path):
    """A domain interface annotated @Repository is implemented by another bean (e.g. a MyBatis repository)."""
    src = tmp_path / 'a'
    src.mkdir()
    (src / 'UserRepository.java').write_text(
        'package a;\nimport org.springframework.stereotype.Repository;\n'
        '@Repository\npublic interface UserRepository { void save(String u); }\n', encoding='utf-8')
    (src / 'MyBatisUserRepository.java').write_text(
        'package a;\nimport org.springframework.stereotype.Repository;\n'
        '@Repository\npublic class MyBatisUserRepository implements UserRepository { public void save(String u) {} }\n',
        encoding='utf-8')
    (src / 'UserService.java').write_text(
        'package a;\nimport org.springframework.stereotype.Service;\n'
        '@Service\npublic class UserService {\n  private final UserRepository userRepository;\n'
        '  public UserService(UserRepository userRepository) { this.userRepository = userRepository; }\n}\n',
        encoding='utf-8')
    kg = RepoKnowledgeGraph.from_path(str(tmp_path), extract_entities=True, index_nodes=False)
    assert 'a.UserRepository' not in {b['id'] for b in fq.list_beans(kg)}
    assert edge(kg, 'a.UserService', 'a.MyBatisUserRepository')['confidence'] == 'inferred'


def test_implementations(spring_kg):
    result = fq.find_implementations(spring_kg, 'OrderService')
    assert {i['id'] for i in result['implementations']} == {f'{PKG}.service.DefaultOrderService',
                                                            f'{PKG}.service.LegacyOrderService'}


# ---------------------------------------------------------------------- web

def test_endpoints(spring_kg):
    served = {n.id for n in spring_kg.get_endpoints()}
    assert served == {
        'endpoint:GET /api/orders', 'endpoint:POST /api/orders', 'endpoint:GET /api/orders/{id}',
        'endpoint:DELETE /api/orders/{id}', 'endpoint:GET /api/admin/stats', 'endpoint:GET /api/admin/statistics',
    }
    create = attrs(spring_kg, 'endpoint:POST /api/orders')
    assert create['handler'] == f'{PKG}.web.OrderController::create'
    assert create['status'] == 'CREATED' and create['consumes'] == ['application/json']
    assert create['request_body']['type'] == 'CreateOrderRequest' and create['validated'] is True
    assert create['security'][0] == {'annotation': 'PreAuthorize', 'value': "hasRole('USER')", 'level': 'method'}
    assert create['response_type'] == 'OrderDto'
    get = attrs(spring_kg, 'endpoint:GET /api/orders/{id}')
    assert get['path_params'] == [{'name': 'id', 'type': 'Long'}]
    assert get['security_filter_chain']['rule'] == 'permitAll'
    assert attrs(spring_kg, 'endpoint:GET /api/admin/stats')['security_filter_chain']['args'] == ['ADMIN']
    page = attrs(spring_kg, 'endpoint:GET /api/orders')['query_params'][0]
    assert page == {'name': 'page', 'type': 'int', 'default': '0'}
    assert edge(spring_kg, 'endpoint:GET /api/orders/{id}', f'{PKG}.web.OrderController::get')['relation'] == 'handled_by'


def test_exception_handlers(spring_kg):
    handler = f'{PKG}.web.ApiExceptionHandler::notFound'
    assert edge(spring_kg, handler, f'{PKG}.service.OrderNotFoundException')['relation'] == 'handles'
    details = fq.endpoint_details(spring_kg, 'endpoint:GET /api/orders/{id}')
    assert details['exception_handlers'][0]['status'] == 'NOT_FOUND'


def test_outgoing_http_calls(spring_kg):
    feign = edge(spring_kg, f'{PKG}.client.PaymentClient::pay', 'endpoint:POST /api/payments/{orderId}')
    assert feign['via'] == 'feign'
    assert attrs(spring_kg, 'endpoint:POST /api/payments/{orderId}')['external'] is True
    internal = edge(spring_kg, f'{PKG}.client.InventoryGateway::echoOrder', 'endpoint:GET /api/orders/{id}')
    assert internal['via'] == 'rest_template'  # matched to the endpoint of the repository
    assert spring_kg.graph.has_edge(f'{PKG}.client.InventoryGateway::reserve',
                                    'endpoint:POST http://inventory/api/stock/reserve')
    mock_mvc = edge(spring_kg, f'{PKG}.web.OrderControllerTest::returnsNotFoundForUnknownOrder',
                    'endpoint:GET /api/orders/{id}')
    assert mock_mvc['via'] == 'mockmvc'


# ---------------------------------------------------------------------- persistence

def test_jpa_model(spring_kg):
    order = attrs(spring_kg, f'{PKG}.domain.Order')['jpa']
    assert order['table'] == 'orders' and order['id'] == {'field': 'id', 'type': 'Long'}
    columns = {c['column'] for c in order['columns']}
    assert {'id', 'customer_id', 'status', 'created_at', 'version'} <= columns  # @MappedSuperclass included
    assert 'dirty' not in columns  # @Transient
    line = attrs(spring_kg, f'{PKG}.domain.OrderLine')['jpa']
    assert line['table'] == 'order_line'
    assert {'field': 'order', 'column': 'order_id', 'type': 'Order', 'foreign_key': True} in line['columns']
    many_to_one = edge(spring_kg, f'{PKG}.domain.OrderLine', f'{PKG}.domain.Order')
    assert many_to_one['cardinality'] == 'many_to_one' and many_to_one['fetch'] == 'LAZY'
    one_to_many = edge(spring_kg, f'{PKG}.domain.Order', f'{PKG}.domain.OrderLine')
    assert one_to_many['mapped_by'] == 'order' and one_to_many['cascade'] == ['ALL']
    assert edge(spring_kg, f'{PKG}.domain.Order', 'table:orders')['relation'] == 'maps_to'


def test_repositories_and_queries(spring_kg):
    repo = f'{PKG}.repository.OrderRepository'
    assert edge(spring_kg, repo, f'{PKG}.domain.Order')['relation'] == 'persists'
    assert attrs(spring_kg, repo)['jpa']['id_type'] == 'java.lang.Long'
    derived = attrs(spring_kg, f'{repo}::findByStatusAndCustomerIdOrderByCreatedAtDesc')['jpa']['query']
    assert derived['fields'] == ['status', 'customerId'] and derived['order_by'] == 'CreatedAtDesc'
    native = attrs(spring_kg, f'{repo}::findNewWithLines')['jpa']['query']
    assert native['kind'] == 'native' and native['tables'] == ['orders', 'order_line']
    jpql = attrs(spring_kg, f'{repo}::findForCustomer')['jpa']['query']
    assert jpql['kind'] == 'jpql' and jpql['tables'] == ['orders']
    assert edges(spring_kg, 'queries') >= {(f'{repo}::findNewWithLines', 'table:order_line')}


def test_migrations_and_drift(spring_kg):
    migration = 'shop-core/src/main/resources/db/migration/V1__init.sql'
    assert edge(spring_kg, migration, 'table:order_line')['version'] == '1'
    table = attrs(spring_kg, 'table:order_line')
    assert set(table['sources']) == {'jpa', 'migration'}
    assert table['drift'] == {'only_in_entity': [], 'only_in_migrations': ['unit_price']}
    assert 'drift' not in attrs(spring_kg, 'table:orders')


# ---------------------------------------------------------------------- messaging & tasks

def test_messaging(spring_kg):
    create = f'{PKG}.service.DefaultOrderService::create'
    assert edge(spring_kg, create, 'channel:kafka:order-created')['via'] == 'KafkaTemplate.send'  # constant resolved
    event = f'channel:event:{PKG}.service.OrderCreatedEvent'
    assert edge(spring_kg, create, event)['relation'] == 'publishes'
    assert edge(spring_kg, f'{PKG}.messaging.OrderEventsListener::onOrderCreated', event)['relation'] == 'listens_to'
    payments = edge(spring_kg, f'{PKG}.messaging.OrderEventsListener::onPayment', 'channel:kafka:payments')
    assert payments['group'] == 'shop'  # topic from '${app.kafka.payments-topic}'


def test_tasks_and_transactions(spring_kg):
    listener = f'{PKG}.messaging.OrderEventsListener'
    assert attrs(spring_kg, f'{listener}::cleanup')['spring']['schedule'] == {'cron': '0 0 3 * * *'}
    assert attrs(spring_kg, f'{listener}::onOrderCreated')['spring']['async'] == {'executor': None}
    service = f'{PKG}.service.DefaultOrderService'
    assert attrs(spring_kg, f'{service}::find')['spring']['transactional'] == {'level': 'method', 'read_only': True}
    assert attrs(spring_kg, f'{service}::create')['spring']['transactional'] == {'level': 'class'}


# ---------------------------------------------------------------------- configuration

def test_configuration_properties(spring_kg):
    timeout = attrs(spring_kg, 'config:app.payment.timeout-ms')
    assert timeout['values'] == {'default': '2000', 'test': '10', 'prod': '5000'}
    readers = edge(spring_kg, f'{PKG}.config.PaymentProperties', 'config:app.payment.timeout-ms')
    assert readers['via'] == '@ConfigurationProperties' and readers['field'] == 'timeoutMs'  # relaxed binding
    retries = attrs(spring_kg, 'config:app.payment.retries')
    assert retries['defined'] is False
    url = attrs(spring_kg, 'config:app.inventory.url')
    assert url['defined'] is False and url['default_in_code'] == 'http://inventory'
    assert edge(spring_kg, f'{PKG}.client.InventoryGateway::inventoryUrl', 'config:app.inventory.url')['via'] == '@Value'
    assert fq.profiles(spring_kg) == ['default', 'prod', 'test']


def test_secrets_are_masked_everywhere(spring_kg):
    assert attrs(spring_kg, 'config:spring.datasource.password')['values'] == {'default': '***',
                                                                               'prod': '${DB_PASSWORD}'}
    assert attrs(spring_kg, 'config:app.admin.api-key')['values'] == {'default': '***'}
    assert attrs(spring_kg, 'config:spring.datasource.url')['values']['prod'] == \
        'jdbc:postgresql://shop:***@db.prod:5432/shop'
    dump = json.dumps(spring_kg.to_dict(), ensure_ascii=False)
    for secret in SECRETS:
        assert secret not in dump
    yml = spring_kg['shop-api/src/main/resources/application.yml']
    assert 'password: ***' in yml.content and 'username: shop' in yml.content


def test_secrets_kept_when_masking_is_disabled(spring_repo):
    from repo_knowledge_graph.builder import GraphBuilder
    from repo_knowledge_graph.utils.parsing_utils import read_directory_files_recursively
    files = read_directory_files_recursively(str(spring_repo))
    builder = GraphBuilder(mask_config_values=False)
    graph, _ = builder.assemble_graph(builder.analyze_files(files, root_path=str(spring_repo)))
    values = graph.nodes['config:spring.datasource.password']['data'].attributes['values']
    assert values['default'] == 's3cr3t-dev-pass'


# ---------------------------------------------------------------------- tests

def test_tests_are_linked(spring_kg):
    web = edge(spring_kg, f'{PKG}.web.OrderControllerTest', f'{PKG}.web.OrderController')
    assert web['via'] == 'annotation'
    assert attrs(spring_kg, f'{PKG}.web.OrderControllerTest')['test']['kind'] == 'web_slice'
    assert edge(spring_kg, f'{PKG}.service.DefaultOrderServiceTest', f'{PKG}.service.DefaultOrderService')['via'] == 'subject'
    assert attrs(spring_kg, f'{PKG}.repository.OrderRepositoryTest')['test']['kind'] == 'data_slice'
    tests = fq.tests_for(spring_kg, 'OrderRepository')['tests']
    assert [t['test'] for t in tests] == [f'{PKG}.repository.OrderRepositoryTest']


# ---------------------------------------------------------------------- call graph quality

EXPECTED_CALLS = {
    f'{PKG}.web.OrderController::get': {
        f'{PKG}.service.OrderService::find', f'{PKG}.service.OrderNotFoundException', f'{PKG}.web.OrderDto::of'},
    f'{PKG}.web.OrderController::create': {
        f'{PKG}.service.OrderService::create', f'{PKG}.service.CreateOrderCommand',
        f'{PKG}.web.CreateOrderRequest::customerId', f'{PKG}.web.CreateOrderRequest::sku',
        f'{PKG}.web.CreateOrderRequest::quantity', f'{PKG}.web.OrderDto::of'},
    f'{PKG}.service.DefaultOrderService::find': set(),  # findById is inherited from JpaRepository
    f'{PKG}.web.OrderDto::of': {
        f'{PKG}.web.OrderDto', f'{PKG}.domain.Order::getId', f'{PKG}.domain.Order::getCustomerId',
        f'{PKG}.domain.Order::getStatus'},
    f'{PKG}.messaging.OrderEventsListener::onOrderCreated': {
        f'{PKG}.messaging.OrderEventsListener::markPaid', f'{PKG}.service.OrderCreatedEvent::orderId'},
}


def test_call_edges_precision_and_recall(spring_kg):
    """Annotated call graph of the fixture: every expected 'calls' edge is found, none is spurious."""
    calls = edges(spring_kg, 'calls')
    expected = {(s, t) for s, targets in EXPECTED_CALLS.items() for t in targets}
    found = {(s, t) for s, t in calls if s in EXPECTED_CALLS}
    true_positives = expected & found
    precision = len(true_positives) / len(found) if found else 1.0
    recall = len(true_positives) / len(expected)
    assert (precision, recall) == (1.0, 1.0), (found - expected, expected - found)


# ---------------------------------------------------------------------- queries

def test_trace_flow_from_endpoint_to_table(spring_kg):
    trace = fq.trace_flow(spring_kg, 'POST /api/orders')
    reached = {s['node'] for s in trace['steps']}
    assert {f'{PKG}.web.OrderController::create', f'{PKG}.service.OrderService::create',
            f'{PKG}.service.DefaultOrderService::create', f'{PKG}.repository.OrderRepository',
            f'{PKG}.domain.Order', 'table:orders', 'channel:kafka:order-created',
            f'{PKG}.messaging.OrderEventsListener::onOrderCreated'} <= reached
    assert not any(s['kind'] == 'variable' for s in trace['steps'])  # trivial steps hidden by default
    assert 'POST /api/orders' in trace['text'].splitlines()[0]


def test_find_endpoint_variants(spring_kg):
    assert fq.find_endpoint(spring_kg, 'GET /api/orders/42') == 'endpoint:GET /api/orders/{id}'
    assert fq.find_endpoint(spring_kg, 'endpoint:POST /api/orders') == 'endpoint:POST /api/orders'
    assert fq.find_endpoint(spring_kg, '/api/admin/stats') == 'endpoint:GET /api/admin/stats'
    with pytest.raises(fq.LookupError_):
        fq.find_endpoint(spring_kg, 'GET /nope')
    with pytest.raises(fq.LookupError_):
        fq.find_endpoint(spring_kg, '/api/orders')  # GET and POST


def test_overview(spring_kg):
    overview = fq.project_overview(spring_kg)
    assert overview['spring_boot_version'] == '3.3.4' and overview['java_version'] == '21'
    assert overview['endpoints']['served'] == 6 and overview['repositories'] == 1
    assert overview['beans_by_stereotype']['rest_controller'] == 2
    assert 'Spring Boot: 3.3.4' in fq.format_overview(overview)


def test_find_by_annotation(spring_kg):
    found = {f['id'] for f in fq.find_by_annotation(spring_kg, '@Transactional')}
    assert found == {f'{PKG}.service.DefaultOrderService', f'{PKG}.service.DefaultOrderService::find'}
    qualified = fq.find_by_annotation(spring_kg, 'org.springframework.scheduling.annotation.Scheduled')
    assert [f['id'] for f in qualified] == [f'{PKG}.messaging.OrderEventsListener::cleanup']


# ---------------------------------------------------------------------- persistence of the graph

def test_serialization_round_trip(spring_kg, tmp_path):
    path = tmp_path / 'spring.json'
    spring_kg.save_graph_to_file(str(path))
    loaded = RepoKnowledgeGraph.load_graph_from_file(str(path), index_nodes=False)
    assert set(loaded.graph.nodes) == set(spring_kg.graph.nodes)
    assert edges(loaded, 'injects') == edges(spring_kg, 'injects')
    assert loaded.graph.edges[f'{PKG}.web.OrderController', f'{PKG}.service.DefaultOrderService']['confidence'] == 'primary'
    assert attrs(loaded, f'{PKG}.domain.Order')['jpa']['table'] == 'orders'
    assert attrs(loaded, 'endpoint:POST /api/orders')['handler'] == f'{PKG}.web.OrderController::create'
    entity = loaded.graph.nodes[f'{PKG}.web.OrderController::create']['data']
    assert entity.file_path.endswith('OrderController.java') and entity.start_line == 40
    assert fq.trace_flow(loaded, 'POST /api/orders')['steps'] == fq.trace_flow(spring_kg, 'POST /api/orders')['steps']


def test_incremental_update_refreshes_enrichment(spring_repo):
    kg = RepoKnowledgeGraph.from_path(str(spring_repo), extract_entities=True, index_nodes=False)
    controller = spring_repo / 'shop-api/src/main/java/com/acme/shop/web/AdminController.java'
    text = controller.read_text(encoding='utf-8')
    controller.write_text(text.replace('@GetMapping({"/stats", "/statistics"})', '@GetMapping("/metrics")'),
                          encoding='utf-8')
    summary = kg.update_from_path(str(spring_repo), rebuild_index=False)
    assert summary['modified'] == ['shop-api/src/main/java/com/acme/shop/web/AdminController.java']
    served = {n.id for n in kg.get_endpoints()}
    assert 'endpoint:GET /api/admin/metrics' in served and 'endpoint:GET /api/admin/stats' not in served
    assert 'endpoint:POST /api/orders' in served  # unchanged files keep their enrichment
    assert edges(kg, 'injects')  # beans re-enriched from the reused analyses


def test_python_routes_become_endpoints(tmp_path):
    (tmp_path / 'app.py').write_text('''from fastapi import FastAPI
app = FastAPI()

@app.get("/items/{item_id}")
def read_item(item_id: int):
    return {}
''', encoding='utf-8')
    kg = RepoKnowledgeGraph.from_path(str(tmp_path), extract_entities=True, index_nodes=False)
    assert kg.metadata['enrichers'] == ['web_routes']
    assert [n.id for n in kg.get_endpoints()] == ['endpoint:GET /items/{item_id}']
    assert edge(kg, 'endpoint:GET /items/{item_id}', 'read_item')['relation'] == 'handled_by'
