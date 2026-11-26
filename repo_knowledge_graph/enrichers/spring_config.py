"""
Spring configuration: `application*.properties` / `application*.yml` (and `bootstrap*`), per profile.

Adds `config_property` nodes (`config:<key>`, values per profile) with `defined_in` edges to their files,
and `reads_config` edges from the code that reads them:
    ${...} placeholders in any annotation (@Value, @Scheduled, @KafkaListener, @ConditionalOnProperty...),
    @ConfigurationProperties classes (prefix + field, relaxed binding),
    @ConditionalOnProperty(prefix, name), Environment.getProperty("key").
A property read but never defined gets `defined: false`.

Secret-looking values (password, secret, token, credentials, private / api / access keys, credentials
embedded in URLs) are replaced by '***' in the property nodes and, unless disabled, in the content of
the configuration files and their chunks, so they reach neither the graph file, nor the search index,
nor the MCP tools.
"""
import logging
import posixpath
import re
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .base import EnrichmentContext, annotation, annotation_value, as_list

logger = logging.getLogger(__name__)

CONFIG_FILE = re.compile(r'^(application|bootstrap)(?:-([\w.\-]+))?\.(ya?ml|properties)$', re.I)
SECRET_KEY = re.compile(r'(password|passwd|pwd|secret|token|credential|private[-_.]?key|api[-_.]?key|'
                        r'access[-_.]?key|client[-_.]?secret|auth[-_.]?key|signing[-_.]?key)', re.I)
URL_CREDENTIALS = re.compile(r'(://[^/:@\s]+:)([^@/\s]+)(@)')
PLACEHOLDER = re.compile(r'\$\{([^}:]+)(?::([^}]*))?\}')
MASK = '***'


def applies(context: EnrichmentContext) -> bool:
    return any(CONFIG_FILE.match(posixpath.basename(p)) for p in context.analyses) or any(
        '${' in str((e.get('attributes') or {}).get('annotations')) for _, e in context.java_declarations())


def normalize_key(key: str) -> str:
    """Relaxed binding: 'app.paymentTimeout', 'app.payment-timeout' and 'APP_PAYMENT_TIMEOUT' match."""
    return re.sub(r'[-_]', '', key).lower()


def kebab(name: str) -> str:
    return re.sub(r'(?<=[a-z0-9])([A-Z])', r'-\1', name).lower()


def is_secret(key: str, value: Any) -> bool:
    if value is None or str(value).strip() == '':
        return False
    text = str(value).strip()
    if PLACEHOLDER.fullmatch(text):
        return False  # '${DB_PASSWORD}' only references an environment variable
    return bool(SECRET_KEY.search(key.rsplit('.', 1)[-1]) or SECRET_KEY.search(key))


def mask_value(key: str, value: Any) -> Any:
    if value is None:
        return None
    if is_secret(key, value):
        return MASK
    if isinstance(value, str) and URL_CREDENTIALS.search(value):
        return URL_CREDENTIALS.sub(r'\1' + MASK + r'\3', value)
    return value


# ---------------------------------------------------------------------- parsing

def parse_properties(content: str) -> Iterator[Tuple[str, str, int]]:
    """(key, value, line) of a .properties file."""
    pending, start = '', 0
    for number, raw in enumerate(content.splitlines(), 1):
        line = raw.strip() if not pending else raw.lstrip()
        if not pending and (not line or line[0] in '#!'):
            continue
        if not pending:
            start = number
        if line.endswith('\\') and not line.endswith('\\\\'):
            pending += line[:-1]
            continue
        line, pending = pending + line, ''
        match = re.match(r'((?:\\.|[^=:\s])+)\s*[=:\s]\s*(.*)$', line)
        if match:
            yield match.group(1).replace('\\', ''), match.group(2).strip(), start
        else:
            yield line, '', start


def _flatten(value: Any, prefix: str, out: Dict[str, Any]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten(v, f'{prefix}.{k}' if prefix else str(k), out)
    elif isinstance(value, list) and value and all(isinstance(v, (dict, list)) for v in value):
        for i, v in enumerate(value):
            _flatten(v, f'{prefix}[{i}]', out)
    elif isinstance(value, list):
        out[prefix] = ','.join('' if v is None else str(v) for v in value)
    else:
        out[prefix] = '' if value is None else (str(value).lower() if isinstance(value, bool) else str(value))


def _yaml_key_lines(document: str, first_line: int) -> Dict[str, int]:
    """Best-effort line of each flattened key of a YAML document."""
    lines: Dict[str, int] = {}
    stack: List[Tuple[int, str]] = []
    for offset, raw in enumerate(document.splitlines()):
        match = re.match(r'^(\s*)(?:-\s+)?([\w.\-\[\]"\']+)\s*:', raw)
        if not match or raw.lstrip().startswith('#'):
            continue
        indent, key = len(match.group(1)), match.group(2).strip('"\'')
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, key))
        lines.setdefault('.'.join(k for _, k in stack), first_line + offset)
    return lines


def parse_yaml(content: str) -> Iterator[Tuple[str, str, int, Optional[str]]]:
    """(key, value, line, document profile) for every leaf of every document of a YAML file."""
    try:
        import yaml
    except ImportError:  # pragma: no cover - pyyaml is a dependency
        logger.warning('pyyaml is not installed: YAML configuration files are not analysed')
        return
    documents = re.split(r'^---\s*$', content, flags=re.M)
    line = 1
    for document in documents:
        try:
            data = yaml.safe_load(document)
        except yaml.YAMLError as e:
            logger.warning(f'Invalid YAML configuration document: {e}')
            data = None
        if isinstance(data, dict):
            flat: Dict[str, Any] = {}
            _flatten(data, '', flat)
            profile = flat.get('spring.config.activate.on-profile') or flat.get('spring.profiles')
            key_lines = _yaml_key_lines(document, line)
            for key, value in flat.items():
                yield key, value, key_lines.get(key, line), profile
        line += document.count('\n') + 1


def _mask_file_content(path: str, content: str) -> str:
    """Content of a configuration file with secret values replaced by '***'."""
    out = []
    yaml_file = not path.lower().endswith('.properties')
    for raw in content.split('\n'):
        stripped = raw.strip()
        if not stripped or stripped[0] in '#!':
            out.append(raw)
            continue
        if yaml_file:
            match = re.match(r'^(\s*(?:-\s+)?([\w.\-"\']+)\s*:\s*)(\S.*?)(\s+#.*)?$', raw)
        else:
            match = re.match(r'^(\s*((?:\\.|[^=:\s])+)\s*[=:\s]\s*)(\S.*?)()$', raw)
        if match:
            key, value = match.group(2).strip('"\''), match.group(3)
            masked = mask_value(key, value.strip('"\''))
            if masked != value.strip('"\''):
                out.append(match.group(1) + masked + (match.group(4) or ''))
                continue
        out.append(URL_CREDENTIALS.sub(r'\1' + MASK + r'\3', raw))
    return '\n'.join(out)


# ---------------------------------------------------------------------- enrichment

def collect_properties(context: EnrichmentContext) -> Dict[str, Dict[str, Any]]:
    properties: Dict[str, Dict[str, Any]] = {}
    from_main: set = set()  # (key, profile) set by a main configuration file: test resources do not override it
    for path, content in context.files('.yml', '.yaml', '.properties'):
        match = CONFIG_FILE.match(posixpath.basename(path))
        if not match:
            continue
        is_test = '/test/' in f'/{path}'
        file_profile = match.group(2) or 'default'
        entries: Iterator[Tuple[str, str, int, Optional[str]]]
        if path.lower().endswith('.properties'):
            entries = ((k, v, line, None) for k, v, line in parse_properties(content))
        else:
            entries = parse_yaml(content)
        for key, value, line, document_profile in entries:
            if key in ('spring.config.activate.on-profile', 'spring.profiles'):
                continue
            profile = document_profile or file_profile
            record = properties.setdefault(key, {'values': {}, 'files': [], 'line': line, 'file': path})
            if is_test and (key, profile) in from_main:
                if path not in record['files']:
                    record['files'].append(path)
                continue
            if not is_test:
                if (key, profile) not in from_main and record['file'] != path:
                    record['file'], record['line'] = path, line  # point at the main definition
                from_main.add((key, profile))
            record['values'][profile] = mask_value(key, value) if context.mask_config_values else value
            if path not in record['files']:
                record['files'].append(path)
            if is_secret(key, value):
                record['secret'] = True
    return properties


def _placeholders(value: Any) -> Iterator[Tuple[str, Optional[str]]]:
    if isinstance(value, str):
        for match in PLACEHOLDER.finditer(value):
            yield match.group(1).strip(), match.group(2)
    elif isinstance(value, list):
        for item in value:
            yield from _placeholders(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _placeholders(item)


def resolve_placeholders(value: str, config: Dict[str, Dict[str, Any]], profile: str = 'default') -> str:
    """Replace '${key:default}' with the value of the property (default profile) or its default."""
    def replace(match):
        key, default = match.group(1).strip(), match.group(2)
        record = config.get(key)
        if record:
            values = record['values']
            if profile in values:
                return str(values[profile])
            if 'default' in values:
                return str(values['default'])
        return default if default is not None else match.group(0)
    return PLACEHOLDER.sub(replace, value) if isinstance(value, str) else value


def enrich(context: EnrichmentContext) -> None:
    properties = collect_properties(context)
    normalized = {normalize_key(k): k for k in properties}
    profiles = sorted({p for record in properties.values() for p in record['values']})

    if context.mask_config_values:
        for path in {f for record in properties.values() for f in record['files']}:
            analysis = context.analyses[path]
            analysis.file_node.content = _mask_file_content(path, analysis.file_node.content)
            for chunk in analysis.chunks:
                chunk.content = _mask_file_content(path, chunk.content)

    def node_for(key: str, default: Optional[str] = None) -> str:
        actual = properties.get(key) and key or normalized.get(normalize_key(key))
        if actual:
            record = properties[actual]
            return context.add_resource('config_property', actual, {
                'values': record['values'], 'defined': True, 'files': record['files'],
                'secret': record.get('secret', False)}, file_path=record['file'], start_line=record['line'])
        attributes: Dict[str, Any] = {'values': {}, 'defined': False, 'files': []}
        if default is not None:
            attributes['default_in_code'] = default
        return context.add_resource('config_property', key, attributes)

    for key, record in properties.items():
        node_id = node_for(key)
        for path in record['files']:
            context.add_edge(node_id, path, 'defined_in')

    for path, entity in context.java_declarations():
        attributes = entity.get('attributes') or {}
        if attributes.get('parameter'):
            continue  # parameters are handled through their method's 'params'
        reader = context.registry.lookup(entity.get('name', ''))
        if not reader:
            continue
        annotations = list(attributes.get('annotations') or [])
        for param in attributes.get('params') or []:
            annotations.extend(param.get('annotations') or [])
        for item in annotations:
            for key, default in _placeholders(item.get('args')):
                context.add_edge(reader, node_for(key, default), 'reads_config', via=f"@{item.get('name')}")

        conditional = annotation(attributes, 'ConditionalOnProperty')
        if conditional:
            prefix = annotation_value(conditional, 'prefix') or ''
            for name in as_list(annotation_value(conditional, 'name', 'value')):
                if isinstance(name, str):
                    key = f'{prefix}.{name}' if prefix else name
                    context.add_edge(reader, node_for(key), 'reads_config', via='@ConditionalOnProperty',
                                     having_value=annotation_value(conditional, 'havingValue'))
            context.set_framework_attribute(reader, 'spring', 'condition_property',
                                  {'prefix': prefix, 'name': annotation_value(conditional, 'name', 'value'),
                                   'having_value': annotation_value(conditional, 'havingValue')})

        properties_annotation = annotation(attributes, 'ConfigurationProperties')
        if properties_annotation:
            prefix = annotation_value(properties_annotation, 'prefix', 'value') or ''
            context.set_framework_attribute(reader, 'spring', 'configuration_properties', prefix or '(root)')
            target = reader
            if entity.get('type') in ('method', 'api_endpoint'):  # @Bean @ConfigurationProperties
                target = context.resolve(attributes.get('return_type_resolved'), path) or reader
            for member in context.members_of(target):
                member_attributes = member.get('attributes') or {}
                if member.get('type') != 'variable' or 'static' in (member_attributes.get('modifiers') or []):
                    continue
                field_name = member['name'].rsplit('::', 1)[-1]
                key = f'{prefix}.{kebab(field_name)}' if prefix else kebab(field_name)
                context.add_edge(reader, node_for(key), 'reads_config', via='@ConfigurationProperties',
                                 field=field_name)

        for invocation in attributes.get('invocations') or []:
            if invocation.get('member') not in ('getProperty', 'getRequiredProperty', 'containsProperty'):
                continue
            receiver = (invocation.get('receiver_type') or invocation.get('receiver') or '').lower()
            args = invocation.get('args') or []
            if 'environment' in receiver or receiver.endswith('env') or 'propertyresolver' in receiver:
                if args and isinstance(args[0], str):
                    context.add_edge(reader, node_for(args[0]), 'reads_config', via='Environment')

    context.shared['config'] = properties
    context.shared['profiles'] = profiles


def property_value(config: Dict[str, Dict[str, Any]], key: str) -> Optional[str]:
    record = config.get(key)
    if not record:
        return None
    values = record['values']
    return values.get('default', next(iter(values.values()), None))


__all__ = ['applies', 'enrich', 'resolve_placeholders', 'property_value', 'mask_value', 'is_secret',
           'normalize_key', 'kebab', 'parse_properties', 'parse_yaml']
