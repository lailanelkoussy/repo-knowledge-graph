"""
Supply chain and findings: resolved dependency versions, library -> code links and reachability, opt-in OSV lookup (with a
fake HTTP layer: no network), unified findings, SARIF export, and the code access tools.
"""
import json
from pathlib import Path

import pytest
from fastmcp import Client

from repo_knowledge_graph import findings as fd
from repo_knowledge_graph import framework_queries as fq
from repo_knowledge_graph.enrichers.supply_chain import managed_versions, packages_of, parse_version_catalog
from repo_knowledge_graph.knowledge_graph import RepoKnowledgeGraph
from repo_knowledge_graph.mcp_server import KnowledgeGraphMCPServer
from repo_knowledge_graph.vulnerability_lookup import OsvClient, summarize

from .conftest import FIXTURES, VULN_SHOP

POM = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-parent</artifactId><version>3.2.5</version>
  </parent>
  <groupId>com.acme</groupId><artifactId>yaml-api</artifactId><version>1.0</version>
  <properties><text.version>1.9</text.version></properties>
  <dependencyManagement><dependencies>
    <dependency><groupId>org.apache.commons</groupId><artifactId>commons-text</artifactId><version>${text.version}</version></dependency>
  </dependencies></dependencyManagement>
  <dependencies>
    <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-web</artifactId></dependency>
    <dependency><groupId>org.yaml</groupId><artifactId>snakeyaml</artifactId><version>1.30</version></dependency>
    <dependency><groupId>org.apache.commons</groupId><artifactId>commons-text</artifactId></dependency>
    <dependency><groupId>com.h2database</groupId><artifactId>h2</artifactId><scope>runtime</scope></dependency>
    <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-test</artifactId><scope>test</scope></dependency>
  </dependencies>
</project>
"""
CONTROLLER = """package com.acme.yaml;

import org.springframework.web.bind.annotation.*;

@RestController
public class ConfigController {
    private final ConfigParser parser;
    public ConfigController(ConfigParser parser) { this.parser = parser; }

    @PostMapping("/config")
    public Object upload(@RequestBody String body) {
        return parser.parse(body);
    }
}
"""
PARSER = """package com.acme.yaml;

import org.springframework.stereotype.Service;
import org.yaml.snakeyaml.Yaml;

@Service
public class ConfigParser {
    public Object parse(String text) {
        return new Yaml().load(text);
    }
}
"""
TEXT_UTIL = """package com.acme.yaml;

import org.apache.commons.text.StringEscapeUtils;

public final class TextUtil {
    private TextUtil() {}
    public static String escape(String s) { return StringEscapeUtils.escapeHtml4(s); }
}
"""
SNAKEYAML_VULN = {
    'id': 'GHSA-mjmj-j48q-9wg2', 'aliases': ['CVE-2022-1471'], 'summary': 'SnakeYaml Constructor Deserialization RCE',
    'database_specific': {'severity': 'HIGH'},
    'affected': [{'package': {'name': 'org.yaml:snakeyaml', 'ecosystem': 'Maven'},
                  'ranges': [{'type': 'ECOSYSTEM', 'events': [{'introduced': '0'}, {'fixed': '2.0'}]}]}]}


@pytest.fixture(scope='module')
def yaml_repo(tmp_path_factory):
    root = tmp_path_factory.mktemp('yaml_api')
    (root / 'pom.xml').write_text(POM, encoding='utf-8')
    package = root / 'src/main/java/com/acme/yaml'
    package.mkdir(parents=True)
    (package / 'ConfigController.java').write_text(CONTROLLER, encoding='utf-8')
    (package / 'ConfigParser.java').write_text(PARSER, encoding='utf-8')
    (package / 'TextUtil.java').write_text(TEXT_UTIL, encoding='utf-8')
    return root


@pytest.fixture(scope='module')
def yaml_kg(yaml_repo):
    return RepoKnowledgeGraph.from_path(str(yaml_repo), extract_entities=True, index_nodes=False)


class FakeOsv:
    """HTTP layer of OSV: records the calls, answers snakeyaml 1.30 with one vulnerability."""

    def __init__(self):
        self.calls = []

    def __call__(self, url, payload=None):
        self.calls.append((url, payload))
        if url.endswith('/querybatch'):
            return {'results': [{'vulns': [{'id': SNAKEYAML_VULN['id']}]} if q['package']['name'] == 'org.yaml:snakeyaml'
                                else {} for q in payload['queries']]}
        assert url.endswith(SNAKEYAML_VULN['id'])
        return SNAKEYAML_VULN


# ---------------------------------------------------------------------- versions and library -> code

def dep(kg, key):
    return kg.graph.nodes[f'dep:{key}']['data'].attributes


def test_resolved_versions(yaml_kg):
    assert dep(yaml_kg, 'org.yaml:snakeyaml')['resolved_version'] == '1.30'
    assert dep(yaml_kg, 'org.yaml:snakeyaml')['version_source'] == 'declared'
    assert dep(yaml_kg, 'org.apache.commons:commons-text')['resolved_version'] == '1.9'
    assert dep(yaml_kg, 'org.apache.commons:commons-text')['version_source'] == 'dependency_management'
    assert dep(yaml_kg, 'org.springframework.boot:spring-boot-starter-web')['resolved_version'] == '3.2.5'
    h2 = dep(yaml_kg, 'com.h2database:h2')
    assert 'resolved_version' not in h2 and h2['version_source'] == 'managed'
    assert h2['managed_by'] == 'spring-boot-dependencies 3.2.5' and h2['declared_in'] == 'pom.xml'


def test_uses_dependency_edges(yaml_kg):
    usage = fq.dependency_usage(yaml_kg, 'org.yaml:snakeyaml')
    assert [f['file'] for f in usage['files']] == ['src/main/java/com/acme/yaml/ConfigParser.java']
    assert usage['files'][0]['imports'] == ['org.yaml.snakeyaml.Yaml']
    assert usage['entry_points'] == [{'entry': 'POST /config', 'depth': 1,
                                      'through': 'com.acme.yaml.ConfigParser::parse'}]
    text = fq.dependency_usage(yaml_kg, 'commons-text')  # unique artifact id
    assert text['files'] and text['entry_points'] == []  # TextUtil is never called
    with pytest.raises(fq.LookupError_):
        fq.dependency_usage(yaml_kg, 'no-such-lib')


def test_dependency_query_shows_resolution(yaml_kg):
    rows = {r['dependency']: r for r in fq.dependencies(yaml_kg)}
    assert rows['dep:org.apache.commons:commons-text']['resolved_version'] == '1.9'
    assert rows['dep:org.yaml:snakeyaml']['used_by_files'] == 1


def test_packages_and_catalogs():
    assert packages_of('com.fasterxml.jackson.core', 'jackson-databind') == ['com.fasterxml.jackson.databind']
    assert packages_of('tools.jackson.core', 'jackson-databind') == ['tools.jackson.databind']
    assert packages_of('org.springframework.boot', 'spring-boot-starter-unknown') == []
    assert packages_of('io.github.foo', 'bar') == ['io.github.foo']
    catalog = parse_version_catalog('[versions]\nyaml = "2.2"\n\n[libraries]\n'
                                    'snakeyaml = { module = "org.yaml:snakeyaml", version.ref = "yaml" }\n'
                                    'commons-text = "org.apache.commons:commons-text:1.10.0"\n')
    assert catalog['snakeyaml'] == {'group': 'org.yaml', 'artifact': 'snakeyaml', 'version': '2.2'}
    assert catalog['commons.text']['version'] == '1.10.0'
    assert managed_versions(POM) == {'org.apache.commons:commons-text': '1.9'}


def test_gradle_version_catalog(tmp_path):
    (tmp_path / 'gradle').mkdir()
    (tmp_path / 'gradle/libs.versions.toml').write_text(
        '[versions]\nyaml = "1.33"\n[libraries]\nsnakeyaml = { module = "org.yaml:snakeyaml", version.ref = "yaml" }\n',
        encoding='utf-8')
    (tmp_path / 'build.gradle').write_text("plugins { id 'java' }\ndependencies {\n    implementation libs.snakeyaml\n}\n",
                                           encoding='utf-8')
    kg = RepoKnowledgeGraph.from_path(str(tmp_path), extract_entities=True, index_nodes=False)
    attributes = dep(kg, 'org.yaml:snakeyaml')
    assert attributes['resolved_version'] == '1.33' and attributes['version_source'] == 'version_catalog'


# ---------------------------------------------------------------------- OSV (opt-in, cached)

def test_osv_lookup_and_cache(tmp_path):
    fake = FakeOsv()
    cache = tmp_path / 'osv.json'
    client = OsvClient(fetch=fake, cache_file=cache, enabled=True)
    answer = client.lookup([('org.yaml:snakeyaml', '1.30'), ('org.apache.commons:commons-text', '1.9')])
    assert answer['org.yaml:snakeyaml@1.30']['vulns'][0]['aliases'] == ['CVE-2022-1471']
    assert answer['org.yaml:snakeyaml@1.30']['vulns'][0]['fixed_in'] == ['2.0']
    assert answer['org.apache.commons:commons-text@1.9'] == {'vulns': [], 'source': 'osv'}
    batch = fake.calls[0][1]
    assert batch == {'queries': [{'package': {'name': 'org.yaml:snakeyaml', 'ecosystem': 'Maven'}, 'version': '1.30'},
                                 {'package': {'name': 'org.apache.commons:commons-text', 'ecosystem': 'Maven'},
                                  'version': '1.9'}]}  # coordinates only
    offline = OsvClient(fetch=lambda *a: pytest.fail('network used'), cache_file=cache, enabled=False)
    again = offline.lookup([('org.yaml:snakeyaml', '1.30')])
    assert again['org.yaml:snakeyaml@1.30']['source'] == 'cache'
    unknown = offline.lookup([('org.yaml:snakeyaml', '2.2')])
    assert unknown['org.yaml:snakeyaml@2.2'] == {'vulns': [], 'source': 'not_checked'}


def test_osv_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv('OSV_LOOKUP', raising=False)
    client = OsvClient(fetch=lambda *a: pytest.fail('network used'), cache_file=tmp_path / 'none.json')
    assert client.enabled is False
    assert client.lookup([('a:b', '1')])['a:b@1']['source'] == 'not_checked'


def test_summarize_severity():
    assert summarize(SNAKEYAML_VULN, 'org.yaml:snakeyaml')['severity'] == 'high'
    assert summarize({'id': 'X', 'severity': [{'type': 'CVSS_V3', 'score': '5.3'}]}, 'p')['severity'] == 'medium'


def test_check_dependencies_with_reachability(yaml_repo, tmp_path):
    kg = RepoKnowledgeGraph.from_path(str(yaml_repo), extract_entities=True, index_nodes=False)
    client = OsvClient(fetch=FakeOsv(), cache_file=tmp_path / 'osv.json', enabled=True)
    result = fq.check_dependencies(kg, client)
    assert result['enabled'] and result['checked'] == 3  # web starter, snakeyaml, commons-text (h2 unresolved, test skipped)
    [vulnerable] = result['vulnerable']
    assert vulnerable['dependency'] == 'dep:org.yaml:snakeyaml'
    assert [e['entry'] for e in vulnerable['entry_points']] == ['POST /config']
    assert {d['dependency'] for d in result['not_checked']} == {'dep:com.h2database:h2'}
    findings = [f for f in fd.collect_findings(kg) if f['kind'] == 'dependency']
    assert len(findings) == 1 and findings[0]['confidence'] == 'high' and findings[0]['severity'] == 'high'
    assert 'CVE-2022-1471' in findings[0]['title'] and 'POST /config' in findings[0]['evidence']
    assert 'Upgrade to 2.0' in findings[0]['hint']


# ---------------------------------------------------------------------- findings and SARIF

@pytest.fixture(scope='module')
def vuln_kg():
    return RepoKnowledgeGraph.from_path(str(VULN_SHOP), extract_entities=True, index_nodes=False)


@pytest.fixture(scope='module')
def secure_kg():
    return RepoKnowledgeGraph.from_path(str(FIXTURES / 'secure_shop'), extract_entities=True, index_nodes=False)


def test_findings_on_vuln_shop(vuln_kg):
    items = fd.collect_findings(vuln_kg)
    kinds = {f['kind'] for f in items}
    assert {'taint', 'sink', 'posture'} <= kinds
    taint = [f for f in items if f['kind'] == 'taint']
    assert len(taint) == 16
    find = next(f for f in taint if (f.get('method') or '').endswith('UserRepository::findByNameUnsafe'))
    assert find['entry'] == 'GET /api/users/find' and find['cwe'] == 89
    assert 'UserService::findByName(name)' in find['evidence'] and "OR '1'='1" in find['hint']
    assert 'parameter name of GET /api/users/find' in find['hint']
    assert not any((f.get('method') or '').endswith('UserRepository::findByStatus') for f in items)  # unreachable lead
    assert any((f.get('method') or '').endswith('UserRepository::findByStatus')
               for f in fd.collect_findings(vuln_kg, include_unconfirmed=True)) is False  # reachable_from_source: false
    assert not any((f.get('method') or '').endswith('TokenService::shuffle') for f in items)  # Random for shuffling


def test_finding_ids_are_stable(vuln_kg):
    again = RepoKnowledgeGraph.from_path(str(VULN_SHOP), extract_entities=True, index_nodes=False)
    assert [f['id'] for f in fd.collect_findings(vuln_kg)] == [f['id'] for f in fd.collect_findings(again)]
    ids = [f['id'] for f in fd.collect_findings(vuln_kg)]
    assert len(ids) == len(set(ids))


def test_filters(secure_kg):
    items = fd.collect_findings(secure_kg)
    high = fd.filter_findings(items, severity='high')
    assert high and all(f['severity'] == 'high' for f in high)
    assert {f['cwe'] for f in fd.filter_findings(items, cwe=639)} == {639}
    assert all(f['confidence'] in ('high', 'medium') for f in fd.filter_findings(items, min_confidence='medium'))
    assert {f['kind'] for f in fd.filter_findings(items, kind='posture')} == {'posture'}


def test_sarif(vuln_kg):
    sarif = fd.to_sarif(fd.collect_findings(vuln_kg))
    assert sarif['version'] == '2.1.0' and len(sarif['runs']) == 1
    run = sarif['runs'][0]
    rules = {r['id']: r for r in run['tool']['driver']['rules']}
    assert 'external/cwe/cwe-89' in rules['sink.sql_injection']['properties']['tags']
    result = next(r for r in run['results'] if 'UserRepository.java' in json.dumps(r['locations']) and r['codeFlows'])
    steps = result['codeFlows'][0]['threadFlows'][0]['locations']
    assert len(steps) == 4  # source, two calls, sink
    assert steps[1]['location']['physicalLocation']['region']['startLine'] == 59
    assert {r['level'] for r in run['results']} <= {'error', 'warning', 'note'}
    json.dumps(sarif)  # serialisable


# ---------------------------------------------------------------------- code access

def test_get_code(vuln_kg):
    code = fq.get_code(vuln_kg, 'VulnController.java', 37, 37, context=1)
    assert code['start_line'] == 36 and code['end_line'] == 38 and 'queryForList(sql)' in code['code']
    with pytest.raises(fq.LookupError_):
        fq.get_code(vuln_kg, 'Missing.java')
    with pytest.raises(ValueError):
        fq.get_code(vuln_kg, 'VulnController.java', 10_000)


def test_grep_code(vuln_kg):
    result = fq.grep_code(vuln_kg, r'Runtime\.getRuntime\(\)\.exec')
    assert {m['file'].rsplit('/', 1)[-1] for m in result['matches']} == {'VulnController.java', 'ReportServiceImpl.java'}
    assert fq.grep_code(vuln_kg, 'runtime.getruntime', case_insensitive=True)['files_with_matches'] == 2
    assert fq.grep_code(vuln_kg, 'queryForList', path_prefix='src/main/java/com/acme/vuln/repository')['files_with_matches'] == 1
    with pytest.raises(ValueError):
        fq.grep_code(vuln_kg, '([')


def test_grep_keeps_secrets_masked(vuln_kg):
    result = fq.grep_code(vuln_kg, 'ENCRYPTION_KEY =')
    assert result['matches'] and '0123456789abcdef' not in json.dumps(result)


# ---------------------------------------------------------------------- MCP tools

async def call(kg, tool, osv=None, **args):
    server = KnowledgeGraphMCPServer(knowledge_graph=kg)
    if osv is not None:
        server.osv_client = osv
    async with Client(server.app) as client:
        result = await client.call_tool(tool, args, raise_on_error=False)
    assert result.structured_content is not None, f'{tool} returned no structured content'
    return result.structured_content


async def test_list_findings_tool(vuln_kg):
    data = await call(vuln_kg, 'list_findings', cwe=89, limit=3)
    assert data['count'] == 3 and data['has_more']
    assert all('flows' not in f for f in data['findings'])
    assert 'verify:' in data['text']


async def test_export_sarif_tool(secure_kg):
    data = await call(secure_kg, 'export_sarif', severity='high')
    assert data['sarif']['version'] == '2.1.0' and data['results'] == len(data['sarif']['runs'][0]['results']) > 0


async def test_code_tools_flag_untrusted_content(vuln_kg):
    code = await call(vuln_kg, 'get_code', file_path='VulnController.java', start_line=35, end_line=38)
    assert code['untrusted_content'] is True and 'search(' in code['text']
    grep = await call(vuln_kg, 'grep_code', pattern='parseExpression')
    assert grep['untrusted_content'] is True and grep['files_with_matches'] == 1


async def test_dependency_tools(yaml_kg, tmp_path):
    usage = await call(yaml_kg, 'get_dependency_usage', dependency='snakeyaml')
    assert 'POST /config (depth 1)' in usage['text']
    disabled = await call(yaml_kg, 'check_dependencies',
                          osv=OsvClient(fetch=lambda *a: pytest.fail('network used'), cache_file=tmp_path / 'x.json',
                                        enabled=False))
    assert disabled['enabled'] is False and 'OSV_LOOKUP=1' in disabled['text'] and not disabled['vulnerable']
    enabled = await call(yaml_kg, 'check_dependencies',
                         osv=OsvClient(fetch=FakeOsv(), cache_file=tmp_path / 'y.json', enabled=True))
    assert 'GHSA-mjmj-j48q-9wg2[high]' in enabled['text'] and 'reached from POST /config' in enabled['text']


def test_supply_chain_survives_serialization(yaml_kg, tmp_path):
    path = Path(tmp_path) / 'kg.json'
    yaml_kg.save_graph_to_file(str(path))
    loaded = RepoKnowledgeGraph.load_graph_from_file(str(path), index_nodes=False)
    assert fq.dependency_usage(loaded, 'snakeyaml') == fq.dependency_usage(yaml_kg, 'snakeyaml')


def test_cli_findings_sarif(tmp_path, capsys):
    from repo_knowledge_graph import cli
    output = tmp_path / 'findings.sarif'
    code = cli.main(['--log-level', 'WARNING', 'findings', '--repo-path', str(VULN_SHOP), '--format', 'sarif',
                     '-o', str(output), '--severity', 'medium', '--fail-on-high'])
    sarif = json.loads(output.read_text(encoding='utf-8'))
    assert code == 1 and sarif['version'] == '2.1.0' and sarif['runs'][0]['results']  # high findings remain
    assert cli.main(['--log-level', 'WARNING', 'findings', '--repo-path', str(VULN_SHOP), '--severity', 'high']) == 0
    assert 'CWE-89' in capsys.readouterr().out
