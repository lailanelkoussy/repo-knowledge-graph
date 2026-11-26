"""
Security enrichment: catalog of sources, sinks and sanitizers, and the classification of every
dangerous call of tests/fixtures/vuln_shop, where each flaw sits next to its safe variant. The checks measure
missed findings (a vulnerable call not reported) and false alarms (a safe call reported) at the same time.
"""
import pytest
from fastmcp import Client

from repo_knowledge_graph import framework_queries as fq
from repo_knowledge_graph.knowledge_graph import RepoKnowledgeGraph
from repo_knowledge_graph.mcp_server import KnowledgeGraphMCPServer
from repo_knowledge_graph.security_catalog import load_catalog

from .conftest import VULN_SHOP

PKG = 'com.acme.vuln.'


@pytest.fixture(scope='module')
def vuln_kg():
    return RepoKnowledgeGraph.from_path(str(VULN_SHOP), extract_entities=True, index_nodes=False)


@pytest.fixture(scope='module')
def sinks(vuln_kg):
    return fq.list_sinks(vuln_kg, include_safe=True)


def statuses(sinks, method, category=None):
    """Statuses of the sinks found in `method` (relative to the fixture package), optionally of one category."""
    return sorted(s['status'] for s in sinks
                  if s['method'] == PKG + method and (category is None or s['category'] == category))


def test_catalog_loads_with_cwe():
    catalog = load_catalog()
    assert catalog.sinks and catalog.sanitizers
    assert all(s.cwe > 0 for s in catalog.sinks)
    assert [s.id for s in catalog.sinks_for('JdbcTemplate', 'queryForList')] == ['sql.spring_jdbc']
    assert catalog.sinks_for('org.springframework.jdbc.core.JdbcTemplate', 'queryForList')
    assert not catalog.sinks_for('JdbcTemplate', 'toString')
    assert catalog.param_annotations['RequestParam'] == 'http_request'
    assert catalog.source_kind('HttpServletRequest', 'getParameter') == 'http_request'
    assert catalog.sanitizer('Long', 'parseLong') is not None


def test_security_enricher_runs(vuln_kg):
    assert 'security' in vuln_kg.metadata['enrichers']


@pytest.mark.parametrize('method, category, expected', [
    # SQL: string concatenation of a request parameter vs bind parameter vs parsed number
    ('web.VulnController::search', 'sql_injection', ['tainted']),
    ('web.VulnController::searchSafe', 'sql_injection', ['safe_by_construction']),
    ('web.VulnController::byId', 'sql_injection', ['sanitized']),
    ('web.VulnController::byEmail', 'sql_injection', ['tainted']),  # HttpServletRequest.getParameter
    ('repository.UserRepository::findByNameUnsafe', 'sql_injection', ['tainted']),  # reached from a handler
    ('repository.UserRepository::findByStatus', 'sql_injection', ['depends_on_param']),  # constant callers only
    ('repository.UserRepository::countOlderThan', 'sql_injection', ['sanitized']),
    # MyBatis: ${} is spliced in the SQL text, #{} is bound
    ('mapper.UserMapper::listOrdered', 'sql_injection', ['tainted']),  # FlowController.ordered passes a parameter
    ('mapper.UserMapper::byId', 'sql_injection', ['safe_by_construction']),
    # command, path, SSRF, expression, redirect, log
    ('web.VulnController::ping', 'command_injection', ['tainted']),
    ('web.VulnController::pingSafe', 'command_injection', ['safe_by_construction']),
    ('web.VulnController::download', 'path_traversal', ['tainted', 'tainted']),
    ('web.VulnController::fetch', 'ssrf', ['tainted']),
    ('web.VulnController::fetchSafe', 'ssrf', ['safe_by_construction']),
    ('web.VulnController::eval', 'expression_injection', ['tainted']),
    ('web.LoginController::done', 'open_redirect', ['tainted']),
    ('web.LoginController::back', 'open_redirect', ['sanitized']),
    ('web.VulnController::audit', 'log_injection', ['sanitized', 'tainted']),
    # crypto: weak algorithm names and a key written in the source
    ('util.CryptoUtil::md5', 'weak_hash', ['weak_value']),
    ('util.CryptoUtil::sha256', 'weak_hash', ['safe_by_construction']),
    ('util.CryptoUtil::des', 'weak_crypto', ['weak_value']),
    ('util.CryptoUtil::gcm', 'weak_crypto', ['safe_by_construction']),
    ('util.CryptoUtil::key', 'hardcoded_key', ['depends_on_field']),
    # dangerous APIs, mitigated or not
    ('util.XmlUtil::parseUnsafe', 'xxe', ['dangerous_api']),
    ('util.XmlUtil::parseSafe', 'xxe', ['mitigated']),
    ('util.XmlUtil::deserialize', 'deserialization', ['dangerous_api']),
    ('security.TokenService::generateResetToken', 'insecure_randomness', ['dangerous_api']),
    ('security.TokenService::generateSessionToken', None, []),  # SecureRandom
    ('security.TrustAllManager::checkServerTrusted', 'trust_all_certificates', ['dangerous_api']),
    # inter-procedural: helper returning its argument, interface dispatch, sanitizer in the caller
    ('web.FlowController::normalized', 'sql_injection', ['tainted']),
    ('web.FlowController::normalizedSafe', 'sql_injection', ['safe_by_construction']),
    ('service.ReportServiceImpl::render', 'command_injection', ['tainted']),
    ('service.StorageService::read', 'path_traversal', ['tainted', 'tainted']),
    ('web.FlowController::fileSafe', None, []),
])
def test_sink_classification(sinks, method, category, expected):
    assert statuses(sinks, method, category) == expected


def test_findings_carry_origins_and_cwe(sinks):
    search = next(s for s in sinks if s['method'] == PKG + 'web.VulnController::search')
    assert search['cwe'] == 89 and search['origins'] == ['param:name']
    assert search['file'].endswith('VulnController.java') and search['line'] == 37
    key = next(s for s in sinks if s['method'] == PKG + 'util.CryptoUtil::key')
    assert key['origins'] == ['field:ENCRYPTION_KEY']
    assert '0123456789abcdef' not in str(key)  # the key itself never leaves the graph


def test_safe_sinks_are_hidden_by_default(vuln_kg, sinks):
    reported = fq.list_sinks(vuln_kg)
    assert reported and len(reported) < len(sinks)
    assert not {s['status'] for s in reported} & {'safe_by_construction', 'sanitized', 'mitigated'}
    assert reported[0]['status'] == 'tainted'  # most worrying first
    assert {s['cwe'] for s in fq.list_sinks(vuln_kg, cwe=78)} == {78}
    assert len(fq.list_sinks(vuln_kg, status='safe_by_construction')) == 7


def test_sources(vuln_kg):
    sources = fq.list_sources(vuln_kg)
    names = {(s['method'].removeprefix(PKG), s.get('param')) for s in sources}
    assert ('web.VulnController::search', 'name') in names
    assert ('web.LoginController::done', 'next') in names
    assert {s['kind'] for s in sources} == {'http_request', 'message'}
    [message] = fq.list_sources(vuln_kg, kind='message')
    assert message['method'] == PKG + 'messaging.OrderEvents::onNote' and message['implicit']


def test_overview_summarises_security(vuln_kg):
    summary = fq.security_summary(vuln_kg)
    assert summary['sinks_by_status']['tainted'] == 16
    assert summary['sinks_by_category']['sql_injection'] == 12
    assert summary['taint_flows']['inter_procedural'] == 5
    text = fq.format_overview(fq.project_overview(vuln_kg))
    assert 'Dangerous API calls by status' in text and 'Taint flows' in text


def flows_to(sinks, method):
    return [f for s in sinks if s['method'] == PKG + method for f in s.get('flows') or []]


def test_flow_through_service_and_repository(sinks):
    sink = next(s for s in sinks if s['method'] == PKG + 'repository.UserRepository::findByNameUnsafe')
    assert sink['local_status'] == 'depends_on_param' and sink['confidence'] == 'high'
    [flow] = sink['flows']
    assert flow['source'] == {'kind': 'http_request', 'method': PKG + 'web.VulnController::find', 'param': 'name',
                              'file': 'src/main/java/com/acme/vuln/web/VulnController.java',
                              'entry': 'GET /api/users/find'}
    assert [(s['to'].removeprefix(PKG), s['param'], s['line']) for s in flow['steps']] == [
        ('service.UserService::findByName', 'name', 59), ('repository.UserRepository::findByNameUnsafe', 'name', 18)]


def test_interface_dispatch_and_mybatis(sinks):
    [flow] = flows_to(sinks, 'service.ReportServiceImpl::render')
    assert flow['source']['entry'] == 'POST /flow/reports'
    assert flow['steps'][-1]['to'] == PKG + 'service.ReportServiceImpl::render'
    [flow] = flows_to(sinks, 'mapper.UserMapper::listOrdered')
    assert flow['source']['param'] == 'order' and flow['steps'][0]['param'] == 'order'


def test_sanitizer_in_the_caller_stops_the_flow(sinks):
    entries = {f['source']['entry'] for f in flows_to(sinks, 'service.StorageService::read')}
    assert entries == {'GET /flow/files'}  # not GET /flow/files/safe (FilenameUtils.getName)


def test_helper_returning_its_argument(sinks):
    [flow] = flows_to(sinks, 'web.FlowController::normalized')
    assert flow['source']['param'] == 'name' and flow['steps'] == []


def test_message_listener_flow(vuln_kg):
    [flow] = fq.trace_taint(vuln_kg, 'kafka:order-notes')
    assert fq.trace_taint(vuln_kg, 'OrderEvents') == [flow]
    assert flow['category'] == 'sql_injection' and flow['source']['kind'] == 'message'
    assert flow['source']['channels'] == [{'channel': 'kafka:order-notes',
                                           'publishers': [PKG + 'messaging.OrderController::addNote']}]
    assert flow['source']['entry'] == 'listener of kafka:order-notes'


def test_unreachable_sinks_are_marked(sinks):
    status = next(s for s in sinks if s['method'] == PKG + 'repository.UserRepository::findByStatus')
    assert status['reachable_from_source'] is False and 'flows' not in status


def test_trace_taint_query(vuln_kg):
    flows = fq.trace_taint(vuln_kg)
    assert len(flows) == 16
    by_endpoint = fq.trace_taint(vuln_kg, 'GET /api/users/find')
    assert [f['sink']['method'] for f in by_endpoint] == [PKG + 'repository.UserRepository::findByNameUnsafe']
    assert by_endpoint[0]['sink']['local_status'] == 'depends_on_param'
    assert {f['category'] for f in fq.trace_taint(vuln_kg, 'StorageService')} == {'path_traversal'}
    assert not fq.trace_taint(vuln_kg, category='xxe')


def test_find_callers_query(vuln_kg):
    result = fq.find_callers(vuln_kg, 'UserRepository::findByNameUnsafe')
    assert [(c['caller'].removeprefix(PKG), c['depth']) for c in result['callers']] == [
        ('service.UserService::findByName', 1), ('web.VulnController::find', 2)]
    assert result['entry_points'] == [{'entry': 'GET /api/users/find', 'depth': 2}]
    impl = fq.find_callers(vuln_kg, 'ReportServiceImpl::render')  # called through the interface
    assert [e['entry'] for e in impl['entry_points']] == ['POST /flow/reports']


async def call(kg, tool, **args):
    async with Client(KnowledgeGraphMCPServer(knowledge_graph=kg).app) as client:
        result = await client.call_tool(tool, args, raise_on_error=False)
    assert result.structured_content is not None, f'{tool} returned no structured content'
    return result.structured_content


async def test_find_sinks_tool(vuln_kg):
    data = await call(vuln_kg, 'find_sinks', category='sql_injection')
    assert {s['status'] for s in data['sinks']} == {'tainted', 'depends_on_param'}
    assert not any('flows' in s for s in data['sinks'])  # flows come from trace_taint
    assert 'reached from GET /api/users/find' in data['text']
    page = await call(vuln_kg, 'find_sinks', limit=2)
    assert len(page['sinks']) == 2 and page['has_more']
    safe = await call(vuln_kg, 'find_sinks', category='sql_injection', include_safe=True)
    assert len(safe['sinks']) == 12


async def test_trace_taint_tool(vuln_kg):
    data = await call(vuln_kg, 'trace_taint', target='POST /flow/reports')
    assert data['count'] == 1 and 'ReportServiceImpl::render(format) called at' in data['text']
    assert 'Runtime.exec' in data['text']
    missing = await call(vuln_kg, 'trace_taint', target='NoSuchThing')
    assert missing.get('error_type') == 'node_not_found'


async def test_find_callers_tool(vuln_kg):
    data = await call(vuln_kg, 'find_callers', entity='StorageService::read')
    assert {e['entry'] for e in data['entry_points']} == {'GET /flow/files', 'GET /flow/files/safe'}


async def test_find_sources_tool(vuln_kg):
    data = await call(vuln_kg, 'find_sources', kind='http_request')
    assert len(data['sources']) >= 10


def test_security_survives_serialization(vuln_kg, tmp_path):
    path = tmp_path / 'kg.json'
    vuln_kg.save_graph_to_file(str(path))
    loaded = RepoKnowledgeGraph.load_graph_from_file(str(path), index_nodes=False)
    assert fq.security_summary(loaded) == fq.security_summary(vuln_kg)


SERVLET = '''package bench;

public class Probe extends javax.servlet.http.HttpServlet {
    @Override
    public void doPost(javax.servlet.http.HttpServletRequest request, javax.servlet.http.HttpServletResponse response)
            throws java.io.IOException {
        String param = request.getParameter("q");
        response.getWriter().println("Hello " + param);
        try {
            javax.crypto.Cipher c = javax.crypto.Cipher.getInstance("DES/CBC/PKCS5Padding");
            java.security.MessageDigest md = java.security.MessageDigest.getInstance("MD5");
        } catch (Exception e) {
            throw new java.io.IOException(e);
        }
    }
}
'''


def test_fully_qualified_receivers_and_servlet_xss(tmp_path):
    (tmp_path / 'Probe.java').write_text(SERVLET, encoding='utf-8')
    kg = RepoKnowledgeGraph.from_path(str(tmp_path), extract_entities=True, index_nodes=False)
    found = {(s['category'], s['status']) for s in fq.list_sinks(kg, include_safe=True)}
    assert ('weak_crypto', 'weak_value') in found  # DES in any mode, receiver written with its package
    assert ('weak_hash', 'weak_value') in found
    assert ('xss', 'tainted') in found  # response.getWriter() returns a PrintWriter
