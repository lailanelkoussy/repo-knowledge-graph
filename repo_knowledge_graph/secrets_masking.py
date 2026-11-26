"""
Masking of secrets hard-coded in source files (Java, Python, JavaScript / TypeScript...).

Configuration files (application*.yml / .properties) are masked by enrichers/spring_config.py. This
module covers the code itself, so that a literal such as `JWT_SECRET = "s3cr3t..."` reaches neither the
graph file, nor the search index, nor the answers of the MCP tools:

- assignments or named arguments whose name looks secret (`password`, `secret`, `token`, `apiKey`...)
  and whose value looks like a real secret (no spaces, long enough, not a placeholder or a key name);
- `${key:default}` placeholders whose key looks secret: the hard-coded default is masked;
- well-known credential formats whatever the name (private keys, AWS / GitHub / Slack / OpenAI-style keys).

The fact that a secret was hard-coded is kept (CWE-798): `mask_code` returns findings with their line,
recorded on the file node (`attributes['hardcoded_secrets']`) and on the constant's entity.
"""
import re
from typing import Any, Dict, Iterable, List, Set, Tuple

MASK = '***'

SECRET_NAME = re.compile(
    r'(password|passwd|pwd|secret|token|api[-_]?key|access[-_]?key|private[-_]?key|client[-_]?secret|'
    r'credential|auth[-_]?key|signing[-_]?key|encryption[-_]?key)', re.I)
# Names that designate a label about a secret, not the secret itself (TOKEN_HEADER, PASSWORD_PARAM...)
LABEL_NAME = re.compile(r'(header|param|parameter|prefix|suffix|name|field|label|message|msg|path|url|uri|'
                        r'endpoint|pattern|regex|format|type|claim|column|property|attribute|key_name)s?$', re.I)
# NAME = "value" / NAME: "value" / name("value") / setPassword("value")
ASSIGNMENT = re.compile(
    r'(?P<name>[A-Za-z_$][\w$.]*)\s*(?:[:=]|\(\s*)\s*(?P<q>["\'])(?P<value>[^"\'\n]{6,512})(?P=q)')
PLACEHOLDER_DEFAULT = re.compile(r'\$\{(?P<key>[^}:]+):(?P<value>[^}]+)\}')
PRIVATE_KEY = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----', re.S)
KNOWN_FORMATS = [
    ('aws_access_key', re.compile(r'\b(AKIA|ASIA)[0-9A-Z]{16}\b')),
    ('github_token', re.compile(r'\bgh[pousr]_[A-Za-z0-9]{36,}\b')),
    ('slack_token', re.compile(r'\bxox[abposr]-[A-Za-z0-9-]{10,}\b')),
    ('api_key', re.compile(r'\bsk-[A-Za-z0-9_-]{20,}\b')),
    ('google_api_key', re.compile(r'\bAIza[0-9A-Za-z_-]{35}\b')),
    ('jwt', re.compile(r'\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b')),
]


def looks_secret(name: str, value: str) -> bool:
    """True when `name` designates a secret and `value` looks like a real one (not a label or a key)."""
    if not name or not value or not SECRET_NAME.search(name) or LABEL_NAME.search(name):
        return False
    value = value.strip()
    if len(value) < 8 or any(c.isspace() for c in value) or value == MASK:
        return False
    if value.isalpha() and sum(c.isupper() for c in value) <= 1:
        return False  # a plain word such as "Authorization" or "Bearer"
    if value.startswith(('${', '#{', '{{', '$', '%', '+', '-', '=', '?')) or re.search(r'%\w+%', value):
        return False  # reference to the environment / configuration, or a shell / batch variable / operator
    lowered = value.lower()
    if re.fullmatch(r'[a-z0-9]+([._-][a-z0-9]+)+', lowered) and SECRET_NAME.search(lowered):
        return False  # a property or header name such as "spring.datasource.password", "x-api-key"
    if re.fullmatch(r'[a-z_]+', lowered) and SECRET_NAME.search(lowered):
        return False  # a label such as "password", "access_token"
    if value.startswith(('/', 'http://', 'https://')) and not re.search(r'://[^/:@\s]+:[^@/\s]+@', value):
        return False  # a path or a URL without credentials
    return True


def mask_code(text: str) -> Tuple[str, List[Dict[str, Any]]]:
    """(masked text, findings) for a source file. Line numbers are preserved."""
    findings: List[Dict[str, Any]] = []

    def line_of(index: int) -> int:
        return text.count('\n', 0, index) + 1

    replacements: List[Tuple[int, int, str]] = []
    for match in PRIVATE_KEY.finditer(text):
        body = match.group(0)
        masked = re.sub(r'[^\n]+', MASK, body)  # same number of lines
        replacements.append((match.start(), match.end(), masked))
        findings.append({'line': line_of(match.start()), 'kind': 'private_key', 'name': None})
    for kind, pattern in KNOWN_FORMATS:
        for match in pattern.finditer(text):
            replacements.append((match.start(), match.end(), MASK))
            findings.append({'line': line_of(match.start()), 'kind': kind, 'name': None})
    for match in ASSIGNMENT.finditer(text):
        name, value = match.group('name'), match.group('value')
        if looks_secret(name.rsplit('.', 1)[-1], value):
            replacements.append((match.start('value'), match.end('value'), MASK))
            findings.append({'line': line_of(match.start()), 'kind': 'hardcoded_secret', 'name': name})
    for match in PLACEHOLDER_DEFAULT.finditer(text):
        if looks_secret(match.group('key').rsplit('.', 1)[-1], match.group('value')):
            replacements.append((match.start('value'), match.end('value'), MASK))
            findings.append({'line': line_of(match.start()), 'kind': 'hardcoded_default',
                             'name': match.group('key')})
    if not replacements:
        return text, []
    out, cursor = [], 0
    for start, end, value in sorted(replacements):
        if start < cursor:
            continue  # overlapping matches: the first one wins
        out.append(text[cursor:start])
        out.append(value)
        cursor = end
    out.append(text[cursor:])
    unique = {(f['line'], f['kind'], f['name']): f for f in findings}
    return ''.join(out), sorted(unique.values(), key=lambda f: f['line'])


def secret_values(text: str) -> Set[str]:
    """The secret literals `mask_code` would mask (used to scrub structured attributes)."""
    values: Set[str] = set()
    for _, pattern in KNOWN_FORMATS:
        values.update(m.group(0) for m in pattern.finditer(text))
    for match in ASSIGNMENT.finditer(text):
        if looks_secret(match.group('name').rsplit('.', 1)[-1], match.group('value')):
            values.add(match.group('value'))
    for match in PLACEHOLDER_DEFAULT.finditer(text):
        if looks_secret(match.group('key').rsplit('.', 1)[-1], match.group('value')):
            values.add(match.group('value'))
    return values


def scrub(value: Any, secrets: Iterable[str]) -> Any:
    """Replace every occurrence of the given secret literals inside nested dicts / lists / strings."""
    secrets = [s for s in secrets if s]
    if not secrets:
        return value

    def walk(item: Any) -> Any:
        if isinstance(item, str):
            for secret in secrets:
                if secret in item:
                    item = item.replace(secret, MASK)
            return item
        if isinstance(item, list):
            return [walk(v) for v in item]
        if isinstance(item, dict):
            return {k: walk(v) for k, v in item.items()}
        return item

    return walk(value)
