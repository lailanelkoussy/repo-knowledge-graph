"""
Data-flow foundations: argument shapes and origins, local def-use, chained-call resolution and
call sites (argument -> parameter bindings) on 'calls' edges.
"""
import pytest

from repo_knowledge_graph import framework_queries as fq
from repo_knowledge_graph.call_resolution import library_result
from repo_knowledge_graph.java_extractor import TreeSitterJavaEntityExtractor
from repo_knowledge_graph.knowledge_graph import RepoKnowledgeGraph

SAMPLE = '''package a;
import org.springframework.jdbc.core.JdbcTemplate;
class Repo {
  private final JdbcTemplate jdbc;
  private String table = "users";
  List<User> byName(String name, int limit) {
    String sql = "SELECT * FROM users WHERE name = '" + name + "'";
    StringBuilder sb = new StringBuilder("SELECT * FROM t WHERE a = ");
    sb.append(limit);
    jdbc.query(sql, mapper);
    jdbc.query(sb.toString(), mapper);
    jdbc.query("SELECT * FROM users WHERE name = ?", mapper, name);
    String fixed = "SELECT 1";
    jdbc.query(fixed, mapper);
    String q = String.format("SELECT %s", name);
    names.forEach(n -> jdbc.update("DELETE FROM x WHERE n = " + n));
    this.table = name;
    return jdbc.query(q, mapper);
  }
}'''


@pytest.fixture(scope='module')
def method():
    declared, _ = TreeSitterJavaEntityExtractor().extract_entities(SAMPLE)
    return next(e for e in declared if e['name'] == 'a.Repo::byName')['attributes']


def first_arg(method, line, member='query'):
    inv = next(i for i in method['invocations'] if i['line'] == line and i['member'] == member)
    return inv['arg_shapes'][0]


def test_concatenation_is_tracked_through_a_local(method):
    sql = first_arg(method, 10)
    assert sql['shape'] == 'local' and sql['built_by'] == 'concat' and sql['origins'] == ['param:name']
    assert sql['constant'] is False


def test_string_builder(method):
    built = first_arg(method, 11)
    assert built['shape'] == 'builder' and 'param:limit' in built['origins']


def test_parameterized_and_constant_queries(method):
    assert first_arg(method, 12) == {'shape': 'literal', 'constant': True,
                                     'value': 'SELECT * FROM users WHERE name = ?'}
    fixed = first_arg(method, 14)
    assert fixed['constant'] is True and fixed['shape'] == 'literal'


def test_format_lambda_fields_and_returns(method):
    fmt = next(i for i in method['invocations'] if i['member'] == 'format')
    assert fmt['receiver_type'] == 'java.lang.String'
    q = first_arg(method, 18)
    assert q['built_by'] == 'format' and 'param:name' in q['origins']
    lambda_update = first_arg(method, 16, 'update')
    assert lambda_update['shape'] == 'concat' and lambda_update['origins'] == ['lambda:n']
    assert method['dataflow']['field_writes'] == {'table': ['param:name']}
    assert 'param:name' in method['dataflow']['returns']


def test_invocations_have_sites(method):
    sites = [i['site'] for i in method['invocations']]
    assert len(sites) == len(set(sites))
    assert all(i['line'] and i['column'] for i in method['invocations'])


def test_library_result_rules():
    assert library_result(('java.util.Optional', ['a.Order']), 'orElseThrow') == ('a.Order', [])
    assert library_result(('a.Order.OrderBuilder', []), 'customer') == ('a.Order.OrderBuilder', [])
    assert library_result(('a.Order.OrderBuilder', []), 'build') == ('a.Order', [])
    assert library_result(('jakarta.persistence.EntityManager', []), 'createQuery')[0] == 'jakarta.persistence.TypedQuery'
    assert library_result(('java.lang.Runtime', []), 'exec')[0] == 'java.lang.Process'
    assert library_result(('a.Unknown', []), 'foo') == (None, [])


@pytest.fixture(scope='module')
def chain_kg(tmp_path_factory):
    root = tmp_path_factory.mktemp('chains')
    src = root / 'a'
    src.mkdir()
    (src / 'Order.java').write_text('''package a;
import lombok.Builder;
import lombok.Getter;
@Getter
@Builder
public class Order {
    private Long id;
    private String customer;
    public Order discount(int percent) { return this; }
}''', encoding='utf-8')
    (src / 'OrderRepository.java').write_text('''package a;
import org.springframework.data.jpa.repository.JpaRepository;
public interface OrderRepository extends JpaRepository<Order, Long> { }''', encoding='utf-8')
    (src / 'OrderService.java').write_text('''package a;
import jakarta.persistence.EntityManager;
import java.util.List;
public class OrderService {
    private final OrderRepository repository;
    private final EntityManager em;

    public String customerOf(Long id) {
        return repository.findById(id).orElseThrow().discount(10).getCustomer();
    }

    public List<Order> search(String name) {
        var query = em.createQuery("select o from Order o where o.customer = :c", Order.class);
        return query.setParameter("c", name).getResultList();
    }

    public Long build(String customer) {
        return Order.builder().customer(customer).build().getId();
    }

    public String delegate(String input) {
        return customerOf(Long.valueOf(input));
    }
}''', encoding='utf-8')
    return RepoKnowledgeGraph.from_path(str(root), extract_entities=True, index_nodes=False)


def invocation(kg, method, member):
    attrs = kg.graph.nodes[method]['data'].attributes
    return next(i for i in attrs['invocations'] if i['member'] == member)


def test_spring_data_and_optional_chain(chain_kg):
    assert invocation(chain_kg, 'a.OrderService::customerOf', 'orElseThrow')['receiver_type'] == 'java.util.Optional'
    discount = invocation(chain_kg, 'a.OrderService::customerOf', 'discount')
    assert discount['receiver_type'] == 'a.Order' and discount['resolved_by'] == 'chain'
    assert chain_kg.graph.has_edge('a.OrderService::customerOf', 'a.Order::discount')
    assert chain_kg.graph.has_edge('a.OrderService::customerOf', 'a.Order::getCustomer')  # Lombok getter


def test_var_initialized_by_a_call(chain_kg):
    set_parameter = invocation(chain_kg, 'a.OrderService::search', 'setParameter')
    assert set_parameter['receiver_type'] == 'jakarta.persistence.TypedQuery'
    assert invocation(chain_kg, 'a.OrderService::search', 'getResultList')['receiver_type'] == \
        'jakarta.persistence.TypedQuery'


def test_lombok_builder_chain(chain_kg):
    assert invocation(chain_kg, 'a.OrderService::build', 'build')['receiver_type'] == 'a.Order.OrderBuilder'
    assert chain_kg.graph.has_edge('a.OrderService::build', 'a.Order::getId')


def test_call_sites_bind_arguments_to_parameters(chain_kg):
    edge = chain_kg.graph.edges['a.OrderService::delegate', 'a.OrderService::customerOf']
    site = edge['sites'][0]
    assert site['file'] == 'a/OrderService.java' and site['line'] == 22
    assert site['args'] == [{'index': 0, 'shape': 'call', 'constant': False, 'param': 'id',
                             'origins': ['call:22:32', 'via:valueOf:param:input']}]  # Long.valueOf sanitizes
    trace = fq.trace_flow(chain_kg, 'a.OrderService::delegate')
    step = next(s for s in trace['steps'] if s['node'] == 'a.OrderService::customerOf')
    assert step['call_site'] == 'a/OrderService.java:22' and step['args'][0]['param'] == 'id'
    assert 'called at a/OrderService.java:22' in trace['text']


def test_round_trip_keeps_sites(chain_kg, tmp_path):
    path = tmp_path / 'g.json'
    chain_kg.save_graph_to_file(str(path))
    loaded = RepoKnowledgeGraph.load_graph_from_file(str(path), index_nodes=False)
    assert loaded.graph.edges['a.OrderService::delegate', 'a.OrderService::customerOf']['sites'] == \
        chain_kg.graph.edges['a.OrderService::delegate', 'a.OrderService::customerOf']['sites']
