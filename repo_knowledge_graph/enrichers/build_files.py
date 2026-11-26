"""
Build files: Maven (`pom.xml`) and Gradle (`build.gradle`, `build.gradle.kts`, `settings.gradle(.kts)`).

Adds `module` nodes (group, version, packaging, Java and Spring Boot versions), `dependency` nodes
(`dep:<group>:<artifact>`, with version and scope) and the relations
    module -depends_on-> dependency | module      module -builds-> directory      module -defined_in-> file
Gradle files are read with regular expressions (they are never executed).
"""
import logging
import posixpath
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, Optional

from .base import EnrichmentContext

logger = logging.getLogger(__name__)

GRADLE_FILES = ('build.gradle', 'build.gradle.kts')
GRADLE_CONFIGURATIONS = ('implementation', 'api', 'compileOnly', 'runtimeOnly', 'testImplementation',
                         'testRuntimeOnly', 'testCompileOnly', 'annotationProcessor', 'developmentOnly',
                         'kapt', 'compile', 'testCompile', 'runtime')
_GRADLE_DEP = re.compile(
    r'\b(' + '|'.join(GRADLE_CONFIGURATIONS) + r')\s*\(?\s*(?:platform\s*\(\s*)?["\']([^"\':\s]+):([^"\':\s]+)(?::([^"\'\s]+))?["\']')
_GRADLE_PROJECT_DEP = re.compile(r'\b(' + '|'.join(GRADLE_CONFIGURATIONS) + r')\s*\(?\s*project\s*\(\s*(?:path\s*[:=]\s*)?["\']:?([^"\']+)["\']')
_GRADLE_BOOT_PLUGIN = re.compile(r'''id\s*\(?\s*["']org\.springframework\.boot["']\s*\)?\s*version\s*\(?\s*["']([^"']+)["']''')
_GRADLE_BOOT_PLUGIN_LEGACY = re.compile(r'''spring-boot-gradle-plugin:([\w.\-]+)''')
_GRADLE_JAVA = [
    re.compile(r'JavaLanguageVersion\.of\(\s*["\']?(\d+)'),
    re.compile(r'(?:sourceCompatibility|targetCompatibility)\s*=\s*(?:JavaVersion\.VERSION_)?["\']?([\d._]+)'),
]
_GRADLE_GROUP = re.compile(r'^\s*group\s*=\s*["\']([^"\']+)["\']', re.M)
_GRADLE_VERSION = re.compile(r'^\s*version\s*=\s*["\']([^"\']+)["\']', re.M)
_GRADLE_INCLUDE = re.compile(r'''include\s*\(?([^\n)]*)''')
_GRADLE_ROOT_NAME = re.compile(r'''rootProject\.name\s*=\s*["']([^"']+)["']''')


def applies(context: EnrichmentContext) -> bool:
    return any(posixpath.basename(p) in ('pom.xml',) + GRADLE_FILES for p in context.analyses)


def _strip_ns(tree: ET.Element) -> None:
    for element in tree.iter():
        if isinstance(element.tag, str) and '}' in element.tag:
            element.tag = element.tag.split('}', 1)[1]


def _text(element: Optional[ET.Element], tag: str) -> Optional[str]:
    if element is None:
        return None
    child = element.find(tag)
    return child.text.strip() if child is not None and child.text else None


def _substitute(value: Optional[str], properties: Dict[str, str]) -> Optional[str]:
    if not value:
        return value
    for _ in range(5):
        new = re.sub(r'\$\{([^}]+)\}', lambda m: properties.get(m.group(1), m.group(0)), value)
        if new == value:
            break
        value = new
    return value


def _parse_pom(path: str, content: str) -> Optional[Dict[str, Any]]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        logger.warning(f'Cannot parse {path}: {e}')
        return None
    _strip_ns(root)
    parent = root.find('parent')
    props = root.find('properties')
    properties = {child.tag: (child.text or '').strip() for child in (props if props is not None else [])
                  if isinstance(child.tag, str)}
    group_id = _text(root, 'groupId') or _text(parent, 'groupId')
    version = _text(root, 'version') or _text(parent, 'version')
    properties.update({'project.groupId': group_id or '', 'project.version': version or '',
                       'project.parent.version': _text(parent, 'version') or ''})
    module: Dict[str, Any] = {
        'build_tool': 'maven',
        'artifact_id': _text(root, 'artifactId'),
        'group_id': group_id,
        'version': version,
        'packaging': _text(root, 'packaging') or 'jar',
        'name': _text(root, 'name'),
        'description': _text(root, 'description'),
        'modules': [m.text.strip() for m in root.findall('modules/module') if m.text],
    }
    if parent is not None:
        module['parent'] = {'group_id': _text(parent, 'groupId'), 'artifact_id': _text(parent, 'artifactId'),
                            'version': _text(parent, 'version')}
        if _text(parent, 'artifactId') == 'spring-boot-starter-parent':
            module['spring_boot_version'] = _text(parent, 'version')
    for key in ('java.version', 'maven.compiler.release', 'maven.compiler.source', 'maven.compiler.target'):
        if properties.get(key):
            module['java_version'] = properties[key]
            break
    for dep in root.findall('dependencyManagement/dependencies/dependency'):
        if _text(dep, 'artifactId') == 'spring-boot-dependencies':
            module['spring_boot_version'] = _substitute(_text(dep, 'version'), properties)
    if not module.get('spring_boot_version') and properties.get('spring-boot.version'):
        module['spring_boot_version'] = properties['spring-boot.version']

    dependencies = []
    for dep in root.findall('dependencies/dependency'):
        dependencies.append({
            'group_id': _substitute(_text(dep, 'groupId'), properties),
            'artifact_id': _substitute(_text(dep, 'artifactId'), properties),
            'version': _substitute(_text(dep, 'version'), properties),
            'scope': _text(dep, 'scope') or 'compile',
            'optional': _text(dep, 'optional') == 'true',
        })
    module['dependencies'] = dependencies
    return module


def _gradle_settings(context: EnrichmentContext) -> Dict[str, Dict[str, Any]]:
    """Directory -> {'root_name', 'includes'} from settings.gradle(.kts) files."""
    settings = {}
    for path, content in context.files('settings.gradle', 'settings.gradle.kts'):
        includes = []
        for match in _GRADLE_INCLUDE.finditer(content):
            includes.extend(re.findall(r'''["']:?([^"']+)["']''', match.group(1)))
        root_name = _GRADLE_ROOT_NAME.search(content)
        settings[posixpath.dirname(path)] = {'root_name': root_name.group(1) if root_name else None,
                                             'includes': includes}
    return settings


def _parse_gradle(path: str, content: str, settings: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    directory = posixpath.dirname(path)
    name = posixpath.basename(directory) if directory else None
    if directory in settings and settings[directory]['root_name']:
        name = settings[directory]['root_name']
    module: Dict[str, Any] = {'build_tool': 'gradle', 'artifact_id': name or 'root', 'packaging': 'jar'}
    group = _GRADLE_GROUP.search(content)
    version = _GRADLE_VERSION.search(content)
    module['group_id'] = group.group(1) if group else None
    module['version'] = version.group(1) if version else None
    boot = _GRADLE_BOOT_PLUGIN.search(content) or _GRADLE_BOOT_PLUGIN_LEGACY.search(content)
    if boot:
        module['spring_boot_version'] = boot.group(1)
    for pattern in _GRADLE_JAVA:
        java = pattern.search(content)
        if java:
            module['java_version'] = java.group(1).replace('_', '.')
            break
    dependencies = []
    for match in _GRADLE_DEP.finditer(content):
        configuration, group_id, artifact_id, dep_version = match.groups()
        dependencies.append({'group_id': group_id, 'artifact_id': artifact_id, 'version': dep_version,
                             'scope': configuration, 'optional': False})
    module['dependencies'] = dependencies
    module['project_dependencies'] = [m.group(2).replace(':', '/').split('/')[-1]
                                      for m in _GRADLE_PROJECT_DEP.finditer(content)]
    module['modules'] = settings.get(directory, {}).get('includes', [])
    return module


def enrich(context: EnrichmentContext) -> None:
    modules: Dict[str, Dict[str, Any]] = {}  # module id -> parsed data (+ '_path')
    gradle_settings = _gradle_settings(context)
    for path, content in context.files():
        base = posixpath.basename(path)
        parsed = None
        if base == 'pom.xml':
            parsed = _parse_pom(path, content)
        elif base in GRADLE_FILES:
            parsed = _parse_gradle(path, content, gradle_settings)
        if not parsed or not parsed.get('artifact_id'):
            continue
        key = parsed['artifact_id']
        if key in modules:  # two modules with the same artifactId: qualify with the directory
            key = f"{key}@{posixpath.dirname(path) or '.'}"
        parsed['_path'] = path
        modules[key] = parsed

    by_artifact = {data['artifact_id']: key for key, data in modules.items()}
    by_directory = {posixpath.dirname(data['_path']): key for key, data in modules.items()}

    # Inherit versions from a parent module of the repository
    for data in modules.values():
        parent_key = by_artifact.get((data.get('parent') or {}).get('artifact_id') or '')
        directory = posixpath.dirname(data['_path'])
        if parent_key is None and directory:
            parent_dir = posixpath.dirname(directory)
            while parent_key is None:
                parent_key = by_directory.get(parent_dir)
                if not parent_dir:
                    break
                parent_dir = posixpath.dirname(parent_dir)
        parent = modules.get(parent_key) if parent_key else None
        if parent is not None and parent is not data:
            for field in ('spring_boot_version', 'java_version', 'group_id', 'version'):
                if not data.get(field) and parent.get(field):
                    data[field] = parent[field]

    # First every module node, so that module -> module edges can be added in any order
    prepared = []
    for key, data in modules.items():
        path = data.pop('_path')
        dependencies = data.pop('dependencies', [])
        project_dependencies = data.pop('project_dependencies', [])
        attributes = {k: v for k, v in data.items() if v not in (None, '', [])}
        attributes['file'] = path
        attributes['dependency_count'] = len(dependencies)
        module_id = context.add_resource('module', key, attributes, file_path=path, start_line=1)
        prepared.append((key, module_id, path, attributes, dependencies, project_dependencies))

    summary = []
    for key, module_id, path, attributes, dependencies, project_dependencies in prepared:
        context.add_edge(module_id, path, 'defined_in')
        context.add_edge(module_id, context.directory_of(path), 'builds')
        for dep in dependencies:
            if not dep.get('artifact_id'):
                continue
            internal = by_artifact.get(dep['artifact_id'])
            if internal and internal != key:
                context.add_edge(module_id, f'module:{internal}', 'depends_on', scope=dep.get('scope'))
                continue
            dep_key = f"{dep.get('group_id') or '?'}:{dep['artifact_id']}"
            dep_id = context.add_resource('dependency', dep_key, {
                'group_id': dep.get('group_id'), 'artifact_id': dep['artifact_id'],
                'versions': {}, 'starter': dep['artifact_id'].startswith('spring-boot-starter')})
            node = context.graph.nodes[dep_id]['data']
            if dep.get('version'):
                node.attributes.setdefault('versions', {})[key] = dep['version']
            context.add_edge(module_id, dep_id, 'depends_on', scope=dep.get('scope'), version=dep.get('version'),
                             optional=dep.get('optional') or None)
        for project in project_dependencies:
            internal = by_artifact.get(project)
            if internal and internal != key:
                context.add_edge(module_id, f'module:{internal}', 'depends_on', scope='project')
        summary.append({'id': module_id, **{k: attributes.get(k) for k in
                                            ('build_tool', 'group_id', 'version', 'java_version',
                                             'spring_boot_version', 'packaging')}})
    context.shared['modules'] = summary
