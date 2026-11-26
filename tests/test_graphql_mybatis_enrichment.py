"""
GraphQL resolvers (Netflix DGS, Spring for GraphQL) and MyBatis statements
(tests/fixtures/graphql_mybatis).
"""
import pytest
from fastmcp import Client

from repo_knowledge_graph import framework_queries as fq
from repo_knowledge_graph.enrichers.graphql import parse_schema
from repo_knowledge_graph.enrichers.mybatis import parse_mapper_xml, sql_tables
from repo_knowledge_graph.knowledge_graph import RepoKnowledgeGraph
from repo_knowledge_graph.mcp_server import KnowledgeGraphMCPServer
from tests.conftest import FIXTURES

PKG = 'com.acme.library'


@pytest.fixture(scope='module')
def kg():
    return RepoKnowledgeGraph.from_path(str(FIXTURES / 'graphql_mybatis'), extract_entities=True, index_nodes=False)


def attrs(kg, node_id):
    return kg.graph.nodes[node_id]['data'].attributes


# ---------------------------------------------------------------------- GraphQL

def test_parse_schema():
    schema = parse_schema((FIXTURES / 'graphql_mybatis/src/main/resources/schema/library.graphqls')
                          .read_text(encoding='utf-8'))
    assert set(schema['Query']) == {'book', 'books'}
    assert schema['Query']['books']['args'] == [{'name': 'first', 'type': 'Int'}, {'name': 'after', 'type': 'String'}]
    assert schema['Query']['book']['description'] == 'Returns one book.'
    assert schema['Mutation']['addBook']['description'] == 'Adds a book to the catalog'
    assert schema['Mutation']['addBook']['returns'] == 'Book'
    assert set(schema['Book']) == {'id', 'title', 'author'}


def test_graphql_operations(kg):
    assert {'graphql', 'mybatis'} <= set(kg.metadata['enrichers'])
    endpoints = {e.id: e.attributes for e in kg.get_endpoints() if e.attributes.get('protocol') == 'graphql'}
    # DGS with codegen constants (DgsConstants.QUERY_TYPE, QUERY.Book, MUTATION.AddBook) and Spring for GraphQL
    assert endpoints['endpoint:GRAPHQL Query.book']['handler'] == f'{PKG}.graphql.BookDatafetcher::book'
    assert endpoints['endpoint:GRAPHQL Mutation.addBook']['handler'] == f'{PKG}.graphql.BookDatafetcher::addBook'
    assert endpoints['endpoint:GRAPHQL Query.books']['handler'] == f'{PKG}.graphql.BookGraphController::books'
    add = endpoints['endpoint:GRAPHQL Mutation.addBook']
    assert add['arguments'] == [{'name': 'title', 'type': 'String!'}, {'name': 'authorId', 'type': 'ID!'}]
    assert add['response_type'] == 'Book' and add['summary'] == 'Adds a book to the catalog'
    assert add['security'][0]['value'] == "hasRole('LIBRARIAN')"
    assert add['http_path'] == '/api/graphql'  # spring.graphql.path
    assert add['security_filter_chain']['rule'] == 'authenticated'
    assert endpoints['endpoint:GRAPHQL Mutation.removeBook']['implemented'] is False


def test_field_resolvers_are_attributes_not_endpoints(kg):
    author = attrs(kg, f'{PKG}.graphql.BookDatafetcher::author')['graphql']
    assert author == {'type': 'Book', 'field': 'author', 'root': False}
    assert 'endpoint:GRAPHQL Book.author' not in kg.graph


def test_graphql_beans(kg):
    beans = {b['id']: b['stereotype'] for b in fq.list_beans(kg)}
    assert beans[f'{PKG}.graphql.BookDatafetcher'] == 'graphql_component'
    assert beans[f'{PKG}.mapper.BookMapper'] == 'mybatis_mapper'
    assert f'{PKG}.repository.BookRepository' not in beans  # plain interface, implemented by a bean
    injected = kg.graph.edges[f'{PKG}.repository.MyBatisBookRepository', f'{PKG}.mapper.BookMapper']
    assert injected['relation'] == 'injects' and injected['confidence'] == 'exact'


# ---------------------------------------------------------------------- MyBatis

def test_parse_mapper_xml_and_sql_tables():
    namespace, statements = parse_mapper_xml((FIXTURES / 'graphql_mybatis/src/main/resources/mapper/BookMapper.xml')
                                             .read_text(encoding='utf-8'))
    assert namespace == f'{PKG}.mapper.BookMapper'
    by_id = {s['id']: s for s in statements}
    assert 'left join authors' in by_id['findById']['sql']  # <include refid> expanded
    assert by_id['insert']['statement'] == 'insert'
    assert sql_tables('select * from a.books b join (select 1 from dual) x on 1=1 where exists (select 1 from loans)') \
        == ['books', 'loans']


def test_mybatis_statements(kg):
    find = attrs(kg, f'{PKG}.mapper.BookMapper::findById')['mybatis']
    assert find['statement'] == 'select' and find['tables'] == ['books', 'authors']
    assert find['source'] == 'src/main/resources/mapper/BookMapper.xml'
    insert = kg.graph.edges[f'{PKG}.mapper.BookMapper::insert', 'table:books']
    assert insert['relation'] == 'queries' and insert['operation'] == 'insert'
    annotated = attrs(kg, f'{PKG}.mapper.BookMapper::countLoaned')['mybatis']
    assert annotated['tables'] == ['books', 'loans'] and annotated['source'].endswith('BookMapper.java')
    assert attrs(kg, 'table:loans')['sources'] == ['mybatis']
    assert attrs(kg, f'{PKG}.mapper.BookMapper')['mybatis']['xml'].endswith('BookMapper.xml')


def test_trace_from_graphql_mutation_to_table(kg):
    trace = fq.trace_flow(kg, 'GRAPHQL Mutation.addBook', max_depth=7)
    reached = [s['node'] for s in trace['steps']]
    assert f'{PKG}.repository.MyBatisBookRepository::save' in reached
    assert f'{PKG}.mapper.BookMapper::insert' in reached and 'table:books' in reached


def test_overview_and_lookup(kg):
    overview = fq.project_overview(kg)
    assert overview['graphql_operations'] == 3 and overview['mybatis_statements'] == 3
    assert fq.find_endpoint(kg, 'Mutation.addBook') == 'endpoint:GRAPHQL Mutation.addBook'


async def test_mcp_tools_list_graphql_operations(kg):
    server = KnowledgeGraphMCPServer(knowledge_graph=kg)
    async with Client(server.app) as client:
        listing = (await client.call_tool('list_endpoints', {'http_method': 'GRAPHQL'})).structured_content
        details = (await client.call_tool('get_endpoint', {'endpoint': 'GRAPHQL Query.book'})).structured_content
    assert listing['total'] == 4  # 3 implemented + removeBook
    assert 'served on /api/graphql' in details['text'] and 'GraphQL arguments' in details['text']
