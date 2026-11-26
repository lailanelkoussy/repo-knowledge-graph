"""
Robustness of the Java / Spring enrichment on a real open-source Spring Boot application.

Needs network access (git clone): set KG_NETWORK_TESTS=1 to run it.
"""
import os
import subprocess
import time

import pytest

from repo_knowledge_graph import framework_queries as fq
from repo_knowledge_graph.java_extractor import TreeSitterJavaEntityExtractor
from repo_knowledge_graph.knowledge_graph import RepoKnowledgeGraph

REPO_URL = 'https://github.com/spring-projects/spring-petclinic.git'
BUILD_BUDGET_SECONDS = 120


@pytest.mark.network
@pytest.mark.slow
@pytest.mark.skipif(os.getenv('KG_NETWORK_TESTS') != '1', reason='set KG_NETWORK_TESTS=1 to run network tests')
def test_spring_petclinic(tmp_path):
    clone = tmp_path / 'petclinic'
    subprocess.run(['git', 'clone', '--depth', '1', REPO_URL, str(clone)], check=True, capture_output=True)

    # Every Java file parses without syntax error
    extractor = TreeSitterJavaEntityExtractor()
    failures = []
    for path in clone.rglob('*.java'):
        extractor.extract_entities(path.read_text(encoding='utf-8'), str(path))
        if extractor.syntax_errors:
            failures.append(path.name)
    assert not failures

    start = time.monotonic()
    kg = RepoKnowledgeGraph.from_path(str(clone), extract_entities=True, index_nodes=False)
    assert time.monotonic() - start < BUILD_BUDGET_SECONDS

    overview = fq.project_overview(kg)
    assert overview['spring_boot_version']
    assert overview['endpoints']['served'] >= 10
    assert overview['jpa_entities'] >= 5 and overview['repositories'] >= 2
    assert overview['beans_by_stereotype'].get('controller', 0) >= 4
    owners = [e for e in fq.list_endpoints(kg) if e.attributes['path'].startswith('/owners')]
    assert owners and all(e.attributes.get('handler') for e in owners)
    trace = fq.trace_flow(kg, owners[0].id)
    assert trace['steps']


@pytest.mark.network
@pytest.mark.slow
@pytest.mark.skipif(os.getenv('KG_NETWORK_TESTS') != '1', reason='set KG_NETWORK_TESTS=1 to run network tests')
def test_realworld_graphql_and_mybatis(tmp_path):
    """REST + Netflix DGS GraphQL + MyBatis XML mappers (no JPA)."""
    clone = tmp_path / 'realworld'
    subprocess.run(['git', 'clone', '--depth', '1',
                    'https://github.com/gothinkster/spring-boot-realworld-example-app.git', str(clone)],
                   check=True, capture_output=True)
    kg = RepoKnowledgeGraph.from_path(str(clone), extract_entities=True, index_nodes=False)
    overview = fq.project_overview(kg)
    assert overview['endpoints']['by_method'].get('GRAPHQL', 0) >= 15
    assert overview['mybatis_statements'] >= 30
    graphql = [e for e in kg.get_endpoints() if e.attributes.get('protocol') == 'graphql']
    assert all(e.attributes.get('handler') for e in graphql if e.attributes.get('implemented') is not False)
    trace = fq.trace_flow(kg, 'GRAPHQL Mutation.createArticle', max_depth=7)
    assert 'table:articles' in {s['node'] for s in trace['steps']}


@pytest.mark.network
@pytest.mark.slow
@pytest.mark.skipif(os.getenv('KG_NETWORK_TESTS') != '1', reason='set KG_NETWORK_TESTS=1 to run network tests')
def test_spring_petclinic_rest_contract_first(tmp_path):
    """Controllers implement interfaces generated from openapi.yml: endpoints come from the specification."""
    clone = tmp_path / 'petclinic-rest'
    subprocess.run(['git', 'clone', '--depth', '1', 'https://github.com/spring-petclinic/spring-petclinic-rest.git',
                    str(clone)], check=True, capture_output=True)
    kg = RepoKnowledgeGraph.from_path(str(clone), extract_entities=True, index_nodes=False)
    endpoints = kg.get_endpoints(include_external=True)
    from_spec = [e for e in endpoints if e.attributes.get('source') == 'openapi']
    assert len(from_spec) >= 30
    implemented = [e for e in from_spec if e.attributes.get('implemented') is not False]
    assert len(implemented) >= 0.9 * len(from_spec) and all(e.attributes.get('handler') for e in implemented)
    assert not [e for e in endpoints if e.attributes.get('external')]  # MockMvc tests are callers, not clients
    owners = fq.find_endpoint(kg, 'GET /api/owners/1')
    assert fq.endpoint_details(kg, owners)['callers']
    # Every JDBC call in the JDBC repositories is parameterised or built from typed values: none is reported.
    sql = fq.list_sinks(kg, category='sql_injection', include_safe=True)
    assert len(sql) >= 50
    assert {s['status'] for s in sql} <= {'safe_by_construction', 'sanitized'}
    # Taint propagation: the request URL reaches URI.create through ExceptionControllerAdvice::detailBuild
    flows = fq.trace_taint(kg, category='ssrf')
    assert flows and all(f['steps'] and f['steps'][-1]['to'].endswith('::detailBuild') for f in flows)
    assert not fq.trace_taint(kg, category='sql_injection')
    # Security posture: petclinic.security.enable=false in the main configuration (true only in the test
    # resources), so the permit-all chain is active and the @PreAuthorize annotations are not enforced.
    posture = fq.security_posture(kg)
    status = {c['name'].rsplit('.', 1)[-1]: c['condition']['status'] for c in posture['filter_chains']}
    assert status == {'BasicAuthenticationConfig::filterChain': 'inactive', 'DisableSecurityConfig::filterChain': 'active'}
    assert set(posture['access_summary']) == {'public'}
    not_enforced = [f for f in posture['findings'] if f['id'] == 'access.annotation_not_enforced']
    assert len(not_enforced) >= 30 and all(f['severity'] == 'medium' for f in not_enforced)  # enforced if enabled
