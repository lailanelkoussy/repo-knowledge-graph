"""
Security posture and effective authorization on tests/fixtures/secure_shop: two ordered filter
chains and a conditional one, a shadowed rule, CSRF / CORS / headers, @PreAuthorize without method security,
IDOR, mass assignment, sensitive response fields and risky properties, each next to a safe counterpart.
"""
import pytest
from fastmcp import Client

from repo_knowledge_graph import framework_queries as fq
from repo_knowledge_graph.enrichers.spring_security import ant_covers, expression_level, strip_comments
from repo_knowledge_graph.knowledge_graph import RepoKnowledgeGraph
from repo_knowledge_graph.mcp_server import KnowledgeGraphMCPServer

from .conftest import FIXTURES

SECURE_SHOP = FIXTURES / 'secure_shop'


@pytest.fixture(scope='module')
def kg():
    return RepoKnowledgeGraph.from_path(str(SECURE_SHOP), extract_entities=True, index_nodes=False)


@pytest.fixture(scope='module')
def posture(kg):
    return fq.security_posture(kg)


def access(kg, endpoint):
    return kg.graph.nodes[f'endpoint:{endpoint}']['data'].attributes['access']


def findings(posture, finding_id):
    return [f for f in posture['findings'] if f['id'] == finding_id]


# ---------------------------------------------------------------------- helpers

def test_expression_levels():
    assert expression_level("hasRole('ADMIN')") == ('role', ['ADMIN'])
    assert expression_level("hasAnyRole('ROLE_A', 'B')") == ('role', ['A', 'B'])
    assert expression_level('isAuthenticated()') == ('authenticated', [])
    assert expression_level('permitAll') == ('public', [])
    assert expression_level('#id == authentication.principal.id') == ('expression', [])


def test_comments_are_stripped_but_lines_kept():
    source = 'a(); // .permitAll()\n/* x\n y */ b("// not a comment");'
    stripped = strip_comments(source)
    assert 'permitAll' not in stripped and stripped.count('\n') == source.count('\n')
    assert '"// not a comment"' in stripped


def test_ant_covers():
    assert ant_covers('/api/**', '/api/admin/**') and ant_covers('/**', '/x')
    assert not ant_covers('/api/**', '/apix/**') and not ant_covers('/api/*', '/api/a/b')


# ---------------------------------------------------------------------- configuration

def test_filter_chains(posture):
    chains = {c['name'].rsplit('::', 1)[-1]: c for c in posture['filter_chains']}
    api, web, dev = chains['apiChain'], chains['webChain'], chains['devChain']
    assert api['scope'] == ['/api/**'] and api['order'] == 1
    assert [r['rule'] for r in api['rules']] == ['permitAll', 'authenticated', 'hasRole', 'denyAll']
    assert not any('/api/debug/**' in r['patterns'] for r in api['rules'])  # commented-out rule
    assert api['csrf']['enabled'] is False and api['authentication'] == ['httpBasic']
    assert api['headers']['frame_options'] == 'disabled'
    assert web['csrf']['enabled'] and web['authentication'] == ['formLogin']
    assert dev['condition'] == {'status': 'conditional', 'reasons': ['@Profile(dev)']}
    assert api['condition']['status'] == web['condition']['status'] == 'active'


def test_ignored_paths_and_cors(posture):
    assert [i['pattern'] for i in posture['ignored_paths']] == ['/static/**', '/h2-console/**']
    assert posture['cors'][0]['origins'] == ['*'] and posture['cors'][0]['credentials'] is True
    assert posture['method_security'] == []


def test_posture_findings(posture):
    assert findings(posture, 'config.csrf_disabled')[0]['chain'].endswith('::apiChain')  # not webChain
    assert findings(posture, 'config.frame_options_disabled')
    [shadowed] = findings(posture, 'config.shadowed_rule')
    assert shadowed['line'] == 31 and '/api/admin/**' in shadowed['title']
    assert findings(posture, 'config.cors_wildcard_credentials')[0]['severity'] == 'high'


def test_risky_properties(posture):
    ids = {f['property'] for f in posture['findings'] if f['id'].startswith('config.') and f.get('property')}
    assert ids == {'management.endpoints.web.exposure.include', 'spring.h2.console.enabled',
                   'server.error.include-stacktrace', 'server.servlet.session.cookie.secure', 'app.jwt.secret'}
    exposure = next(f for f in posture['findings'] if f.get('property') == 'management.endpoints.web.exposure.include')
    assert exposure['value'] == 'health,env'  # the test resources ('*') do not override the main configuration
    assert 'fixture-literal-signing-key' not in str(posture)  # secret value never copied


# ---------------------------------------------------------------------- access matrix

@pytest.mark.parametrize('endpoint, level', [
    ('GET /api/accounts/{id}', 'authenticated'),
    ('POST /api/public/register', 'public'),
    ('DELETE /api/admin/accounts/{id}', 'authenticated'),  # /api/** matches before /api/admin/**
    ('GET /', 'public'),
    ('POST /contact', 'public'),
    ('GET /dashboard', 'authenticated'),  # second chain (the API chain does not apply outside /api/**)
])
def test_access_levels(kg, endpoint, level):
    info = access(kg, endpoint)
    assert info['level'] == level
    assert info['alternatives'] == [{'chain': 'com.acme.secure.config.DevSecurityConfig::devChain',
                                     'status': 'conditional', 'when': ['@Profile(dev)'], 'level': 'public',
                                     'roles': []}]


def test_decision_trace(kg):
    via = access(kg, 'DELETE /api/admin/accounts/{id}')['via']
    assert via[0]['source'] == 'filter_chain' and via[0]['line'] == 30 and via[0]['chain'].endswith('::apiChain')
    assert via[1]['source'] == 'annotation' and via[1]['effective'] is False and via[1]['roles'] == ['ADMIN']
    assert access(kg, 'GET /dashboard')['via'][0]['chain'].endswith('::webChain')


@pytest.mark.parametrize('endpoint, expected', [
    ('GET /api/accounts/{id}', {'access.idor_candidate', 'output.sensitive_fields'}),
    ('GET /api/accounts/mine/{id}', set()),  # @AuthenticationPrincipal, @JsonIgnore on the password
    ('PUT /api/accounts/{id}', {'input.mass_assignment', 'input.missing_validation', 'output.sensitive_fields'}),
    ('POST /api/public/register', {'access.public_state_changing', 'input.mass_assignment'}),
    ('POST /contact', {'access.public_state_changing'}),
    ('GET /dashboard', set()),
])
def test_endpoint_findings(kg, endpoint, expected):
    assert set(access(kg, endpoint)['findings']) >= expected
    if not expected:
        assert access(kg, endpoint)['findings'] == []


def test_annotation_not_enforced(kg, posture):
    assert 'access.annotation_not_enforced' in access(kg, 'DELETE /api/admin/accounts/{id}')['findings']
    [item] = findings(posture, 'access.annotation_not_enforced')
    assert item['severity'] == 'high' and item['endpoint'] == 'DELETE /api/admin/accounts/{id}'


def test_register_is_an_authentication_endpoint(posture):
    item = next(f for f in findings(posture, 'access.public_state_changing') if f['endpoint'] == 'POST /api/public/register')
    assert item['severity'] == 'low'
    contact = next(f for f in findings(posture, 'access.public_state_changing') if f['endpoint'] == 'POST /contact')
    assert contact['severity'] == 'medium'


# ---------------------------------------------------------------------- queries and tools

def test_attack_surface_query(kg):
    rows = fq.attack_surface(kg)
    assert [r['access'] for r in rows[:3]] == ['public'] * 3
    public_changes = fq.attack_surface(kg, access='public', http_method='POST')
    assert {r['entry'] for r in public_changes} == {'POST /api/public/register', 'POST /contact'}
    delete = next(r for r in rows if r['entry'] == 'DELETE /api/admin/accounts/{id}')
    assert delete['annotation'] == {'name': 'PreAuthorize', 'value': "hasRole('ADMIN')", 'effective': False}
    assert delete['decided_by']['line'] == 30


def test_overview_mentions_access(kg):
    text = fq.format_overview(fq.project_overview(kg))
    assert 'Endpoint access: authenticated=5, public=3' in text


async def call(kg, tool, **args):
    async with Client(KnowledgeGraphMCPServer(knowledge_graph=kg).app) as client:
        result = await client.call_tool(tool, args, raise_on_error=False)
    assert result.structured_content is not None, f'{tool} returned no structured content'
    return result.structured_content


async def test_get_security_posture_tool(kg):
    data = await call(kg, 'get_security_posture', severity='high')
    assert {f['severity'] for f in data['findings']} == {'high'}
    assert 'csrf=OFF' in data['text'] and 'line 31: * /api/admin/** -> hasRole(ADMIN)' in data['text']
    assert 'fixture-literal-signing-key' not in str(data)


async def test_list_attack_surface_tool(kg):
    data = await call(kg, 'list_attack_surface', with_findings=True)
    assert 'NOT enforced' in data['text']
    assert all(e['findings'] or e['taint_flows'] for e in data['entries'])


def test_posture_survives_serialization(kg, tmp_path):
    path = tmp_path / 'kg.json'
    kg.save_graph_to_file(str(path))
    loaded = RepoKnowledgeGraph.load_graph_from_file(str(path), index_nodes=False)
    assert fq.security_posture(loaded)['access_summary'] == fq.security_posture(kg)['access_summary']
    assert fq.attack_surface(loaded) == fq.attack_surface(kg)
