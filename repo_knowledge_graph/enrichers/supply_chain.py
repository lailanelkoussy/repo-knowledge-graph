"""
Supply chain: resolved dependency versions and the link between a library and the code using it.

- **Versions**: each `dependency` node gets `resolved_version` and `version_source`:
  `declared` (in the build file, properties substituted), `dependency_management` (Maven
  `<dependencyManagement>` of the repository), `version_catalog` (Gradle `libs.versions.toml`, whose `libs.x.y`
  references are also turned into dependencies), `spring_boot` (artifacts of the `org.springframework.boot`
  group share the Spring Boot version) or `managed` (version chosen by the Spring Boot BOM, not resolved
  offline: `managed_by` names it).
- **Library -> code**: `packages` (Java package prefixes of the library: table of well-known artifacts and
  starters, otherwise the group id) and `uses_dependency` edges (file -> dependency, with the matching imports).

Known vulnerabilities are looked up on demand only (`check_dependencies`, `vulnerability_lookup.py`), never
during the build.
"""
import logging
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

from .base import EnrichmentContext

logger = logging.getLogger(__name__)

# artifact (or group) -> Java packages it provides; starters list the libraries they bring
PACKAGES: Dict[str, Tuple[str, ...]] = {
    'spring-boot-starter-web': ('org.springframework.web', 'org.springframework.http', 'com.fasterxml.jackson'),
    'spring-boot-starter-webflux': ('org.springframework.web.reactive', 'org.springframework.http', 'reactor'),
    'spring-boot-starter-data-jpa': ('org.springframework.data.jpa', 'org.springframework.data.repository',
                                     'org.springframework.data.domain', 'jakarta.persistence', 'javax.persistence',
                                     'org.hibernate'),
    'spring-boot-starter-data-mongodb': ('org.springframework.data.mongodb',),
    'spring-boot-starter-jdbc': ('org.springframework.jdbc',),
    'spring-boot-starter-security': ('org.springframework.security',),
    'spring-boot-starter-oauth2-resource-server': ('org.springframework.security.oauth2',),
    'spring-boot-starter-validation': ('jakarta.validation', 'javax.validation', 'org.hibernate.validator'),
    'spring-boot-starter-actuator': ('org.springframework.boot.actuate',),
    'spring-boot-starter-thymeleaf': ('org.thymeleaf',),
    'spring-boot-starter-amqp': ('org.springframework.amqp',),
    'spring-boot-starter-mail': ('jakarta.mail', 'javax.mail', 'org.springframework.mail'),
    'spring-boot-starter-cache': ('org.springframework.cache',),
    'spring-boot-starter-hateoas': ('org.springframework.hateoas',),
    'spring-boot-starter-graphql': ('org.springframework.graphql',),
    'spring-boot-starter-test': ('org.junit', 'org.mockito', 'org.assertj', 'org.hamcrest',
                                 'org.springframework.test', 'org.springframework.boot.test'),
    'spring-security-test': ('org.springframework.security.test',),
    'spring-kafka': ('org.springframework.kafka',),
    'mybatis-spring-boot-starter': ('org.apache.ibatis', 'org.mybatis'),
    'mybatis': ('org.apache.ibatis',),
    'graphql-dgs-spring-boot-starter': ('com.netflix.graphql.dgs',),
    'h2': ('org.h2',), 'postgresql': ('org.postgresql',), 'mysql-connector-java': ('com.mysql',),
    'mysql-connector-j': ('com.mysql',), 'sqlite-jdbc': ('org.sqlite',),
    'lombok': ('lombok',), 'mapstruct': ('org.mapstruct',), 'guava': ('com.google.common',),
    'commons-io': ('org.apache.commons.io',), 'commons-lang3': ('org.apache.commons.lang3',),
    'commons-text': ('org.apache.commons.text',), 'commons-collections4': ('org.apache.commons.collections4',),
    'commons-collections': ('org.apache.commons.collections',), 'commons-codec': ('org.apache.commons.codec',),
    'snakeyaml': ('org.yaml.snakeyaml',), 'joda-time': ('org.joda.time',),
    'log4j-core': ('org.apache.logging.log4j',), 'log4j-api': ('org.apache.logging.log4j',),
    'jjwt': ('io.jsonwebtoken',), 'jjwt-api': ('io.jsonwebtoken',), 'jjwt-impl': ('io.jsonwebtoken',),
    'java-jwt': ('com.auth0.jwt',),
    'com.fasterxml.jackson.core:jackson-databind': ('com.fasterxml.jackson.databind',),
    'com.fasterxml.jackson.core:jackson-core': ('com.fasterxml.jackson.core',),
    'tools.jackson.core:jackson-databind': ('tools.jackson.databind',),
    'tools.jackson.core:jackson-core': ('tools.jackson.core',),
    'spring-boot-starter-webmvc': ('org.springframework.web', 'org.springframework.http'),
    'spring-boot-starter-aspectj': ('org.aspectj',), 'spring-boot-starter-aop': ('org.aspectj',),
    'spring-boot-starter-security-test': ('org.springframework.security.test',),
    'springdoc-openapi-starter-webmvc-ui': ('org.springdoc', 'io.swagger.v3'),
    'springdoc-openapi-ui': ('org.springdoc', 'io.swagger.v3'), 'springfox-swagger2': ('springfox',),
    'flyway-core': ('org.flywaydb',), 'liquibase-core': ('liquibase',), 'xstream': ('com.thoughtworks.xstream',),
    'rest-assured': ('io.restassured',), 'spring-mock-mvc': ('io.restassured.module.mockmvc',),
    'io.rest-assured:json-path': ('io.restassured.path.json',), 'com.jayway.jsonpath:json-path': ('com.jayway.jsonpath',),
    'xml-path': ('io.restassured.path.xml',),
}
CATALOG_REF = re.compile(r'\blibs\.([\w.\-]+)')


def applies(context: EnrichmentContext) -> bool:
    return any(n for n in context.graph if str(n).startswith('dep:')) or \
        any(p.endswith('libs.versions.toml') for p in context.analyses)


def packages_of(group_id: Optional[str], artifact_id: str) -> List[str]:
    for key in (f'{group_id}:{artifact_id}', artifact_id):
        if key in PACKAGES:
            return list(PACKAGES[key])
    if group_id and not group_id.startswith('org.springframework.boot'):
        return [group_id]
    return []


# ---------------------------------------------------------------------- Gradle version catalogs

def parse_version_catalog(content: str) -> Dict[str, Dict[str, Optional[str]]]:
    """`libs.versions.toml` -> alias (dotted, as used in `libs.x.y`) -> {group, artifact, version}."""
    try:
        import tomllib  # Python 3.11+
        data = tomllib.loads(content)
    except ImportError:  # pragma: no cover - Python 3.10: a minimal reader of the two tables used here
        data = _minimal_toml(content)
    except Exception as e:  # noqa: BLE001
        logger.warning(f'Cannot parse version catalog: {e}')
        return {}
    versions = {k: str(v) for k, v in (data.get('versions') or {}).items() if isinstance(v, (str, int, float))}
    libraries = {}
    for alias, spec in (data.get('libraries') or {}).items():
        if isinstance(spec, str):
            parts = spec.split(':')
            group, artifact, version = (parts + [None, None])[:3]
        elif isinstance(spec, dict):
            module = spec.get('module') or f"{spec.get('group')}:{spec.get('name')}"
            group, _, artifact = str(module).partition(':')
            raw = spec.get('version')
            version = versions.get(raw.get('ref')) if isinstance(raw, dict) else raw
            if version is None and spec.get('version.ref'):
                version = versions.get(spec['version.ref'])
        else:
            continue
        libraries[alias.replace('-', '.').replace('_', '.')] = {'group': group, 'artifact': artifact,
                                                                 'version': str(version) if version else None}
    return libraries


def _minimal_toml(content: str) -> Dict[str, Dict[str, Any]]:
    data: Dict[str, Dict[str, Any]] = {}
    table = None
    for line in content.splitlines():
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        header = re.fullmatch(r'\[([\w.\-]+)\]', line)
        if header:
            table = data.setdefault(header.group(1), {})
            continue
        if table is None or '=' not in line:
            continue
        key, value = (s.strip() for s in line.split('=', 1))
        if value.startswith('{'):
            item: Dict[str, Any] = {}
            for k, v in re.findall(r'([\w.]+)\s*=\s*"([^"]*)"', value):
                if k == 'version.ref':
                    item['version'] = {'ref': v}
                else:
                    item[k] = v
            ref = re.search(r'version\s*=\s*\{\s*ref\s*=\s*"([^"]+)"', value)
            if ref:
                item['version'] = {'ref': ref.group(1)}
            table[key] = item
        else:
            table[key] = value.strip('"')
    return data


# ---------------------------------------------------------------------- Maven dependencyManagement

def managed_versions(content: str) -> Dict[str, str]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return {}
    for element in root.iter():
        if isinstance(element.tag, str) and '}' in element.tag:
            element.tag = element.tag.split('}', 1)[1]
    block = root.find('properties')
    properties = {c.tag: (c.text or '').strip() for c in (list(block) if block is not None else [])
                  if isinstance(c.tag, str)}
    found = {}
    for dep in root.findall('dependencyManagement/dependencies/dependency'):
        group, artifact, version = (dep.findtext(t) for t in ('groupId', 'artifactId', 'version'))
        if group and artifact and version:
            version = re.sub(r'\$\{([^}]+)\}', lambda m: properties.get(m.group(1), m.group(0)), version.strip())
            if '${' not in version and (dep.findtext('scope') or '') != 'import':
                found[f'{group.strip()}:{artifact.strip()}'] = version
    return found


# ---------------------------------------------------------------------- enrichment

def enrich(context: EnrichmentContext) -> None:
    graph = context.graph
    managed: Dict[str, str] = {}
    for _path, content in context.files('pom.xml'):
        managed.update(managed_versions(content))
    catalog: Dict[str, Dict[str, Optional[str]]] = {}
    for _path, content in context.files('libs.versions.toml'):
        catalog.update(parse_version_catalog(content))

    modules = [graph.nodes[n]['data'] for n in graph if str(n).startswith('module:')]
    boot_versions = {m.id: m.attributes.get('spring_boot_version') for m in modules}

    # libs.x.y references of the Gradle files
    if catalog:
        for path, content in context.files('build.gradle', 'build.gradle.kts'):
            module_id = next((m.id for m in modules if m.attributes.get('file') == path), None)
            for match in CATALOG_REF.finditer(content):
                alias = match.group(1).replace('-', '.').replace('_', '.')
                spec = catalog.get(alias)
                if not spec or not spec.get('artifact') or module_id is None:
                    continue
                key = f"{spec['group']}:{spec['artifact']}"
                dep_id = context.add_resource('dependency', key, {
                    'group_id': spec['group'], 'artifact_id': spec['artifact'], 'versions': {},
                    'starter': str(spec['artifact']).startswith('spring-boot-starter')})
                if spec.get('version'):
                    graph.nodes[dep_id]['data'].attributes['versions'][module_id.removeprefix('module:')] = spec['version']
                    graph.nodes[dep_id]['data'].attributes['catalog_version'] = spec['version']
                line = content.count('\n', 0, match.start()) + 1
                scope = re.search(r'(\w+)\s*\(?\s*$', content[max(0, match.start() - 40):match.start()])
                context.add_edge(module_id, dep_id, 'depends_on', scope=scope.group(1) if scope else None,
                                 version=spec.get('version'), via='version_catalog', line=line)

    deps = {n: graph.nodes[n]['data'] for n in graph if str(n).startswith('dep:')}
    for dep_id, node in deps.items():
        attributes = node.attributes
        group, artifact = attributes.get('group_id'), attributes.get('artifact_id')
        key = f'{group}:{artifact}'
        declared = sorted({v for v in (attributes.get('versions') or {}).values() if v and '${' not in str(v)})
        module_ids = [s for s, _, d in graph.in_edges(dep_id, data=True) if d.get('relation') == 'depends_on']
        boot = next((boot_versions.get(m) for m in module_ids if boot_versions.get(m)), None)
        if attributes.get('catalog_version'):
            attributes.update(resolved_version=attributes['catalog_version'], version_source='version_catalog')
        elif declared:
            attributes.update(resolved_version=declared[-1], version_source='declared')
            if len(declared) > 1:
                attributes['version_conflict'] = declared
        elif key in managed:
            attributes.update(resolved_version=managed[key], version_source='dependency_management')
        elif group and group.startswith('org.springframework.boot') and boot:
            attributes.update(resolved_version=boot, version_source='spring_boot')
        elif boot:
            attributes.update(version_source='managed', managed_by=f'spring-boot-dependencies {boot}')
        else:
            attributes['version_source'] = 'unknown'
        attributes['scopes'] = sorted({str(d.get('scope')) for _, _, d in graph.in_edges(dep_id, data=True)
                                       if d.get('relation') == 'depends_on' and d.get('scope')})
        attributes['packages'] = packages_of(group, str(artifact))
        files = [graph.nodes[m]['data'].attributes.get('file') for m in module_ids if m in graph]
        if files and files[0]:
            attributes['declared_in'] = files[0]

    # file -> dependency, from the imports
    prefixes = sorted(((p, dep_id) for dep_id, node in deps.items() for p in node.attributes.get('packages') or []),
                      key=lambda x: -len(x[0]))
    usage: Dict[str, int] = {}
    for path, analysis in context.analyses.items():
        if not path.endswith('.java'):
            continue
        matched: Dict[str, List[str]] = {}
        for imported in getattr(analysis.file_node, 'imports', []) or []:
            name = str(imported).removeprefix('static ')
            for prefix, dep_id in prefixes:
                if name == prefix or name.startswith(prefix + '.'):
                    matched.setdefault(dep_id, []).append(name)
                    break
        for dep_id, imports in matched.items():
            context.add_edge(path, dep_id, 'uses_dependency', imports=sorted(set(imports))[:10],
                             test='/test/' in f'/{path}')
            usage[dep_id] = usage.get(dep_id, 0) + 1
    for dep_id, node in deps.items():
        node.attributes['used_by_files'] = usage.get(dep_id, 0)
    context.shared['supply_chain'] = {
        'dependencies': len(deps),
        'resolved': sum(1 for n in deps.values() if n.attributes.get('resolved_version')),
        'used': sum(1 for d in deps if usage.get(d))}


__all__ = ['applies', 'enrich', 'packages_of', 'parse_version_catalog', 'managed_versions']
