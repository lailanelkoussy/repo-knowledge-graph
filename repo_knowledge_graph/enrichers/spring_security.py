"""
Security posture of a Spring application and the effective authorization of every entry point.

Read statically from the Java configuration (lambda and chained DSL styles, comments ignored):

- **filter chains**: every `SecurityFilterChain` bean / `configure(HttpSecurity)` method with its conditions
  (`@ConditionalOnProperty` evaluated against the configuration, `@Profile`), scope (`securityMatcher`),
  authorization rules in declaration order, CSRF, CORS, headers (frame options, CSP, HSTS), session policy,
  authentication mechanisms (HTTP Basic, form login, OAuth2 resource server / login, custom filters);
- **method security**: `@EnableMethodSecurity` / `@EnableGlobalMethodSecurity` (pre/post, secured, JSR-250),
  active or not; `WebSecurityCustomizer.ignoring()` patterns; CORS configurations and `@CrossOrigin`;
- **risky configuration**: actuator exposure, H2 console, debug, stack traces in errors, cookie flags, SQL
  logging, secrets written as literals instead of `${ENV}` references.

Each endpoint gets `attributes['access']`: `level` (public, anonymous, authenticated, role, authority,
expression, denied, unknown), `roles`, the decisions that led to it (`via`: filter-chain rule with file:line,
method / class annotation and whether method security makes it effective), the same computation for the
other conditional chains (`alternatives`) and the candidate findings of the endpoint. A resource node
`security_posture:application` holds the whole posture and every finding (CWE, severity, confidence,
location, hint to verify). Findings are candidates for an auditor, never verdicts.
"""
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .base import EnrichmentContext, annotation, annotation_value, as_list

logger = logging.getLogger(__name__)

HTTP_SECURITY_TYPES = ('HttpSecurity', 'ServerHttpSecurity')
LEVEL_RANK = {'unknown': -1, 'public': 0, 'anonymous': 0, 'authenticated': 1, 'expression': 2, 'authority': 2,
              'role': 2, 'denied': 3}
STATE_CHANGING = ('POST', 'PUT', 'PATCH', 'DELETE')
AUTH_PATH = re.compile(r'(login|logout|signin|signup|register|auth|token|refresh|password|users/?$)', re.I)
FIND_BY_ID = {'findById', 'getById', 'getOne', 'getReferenceById', 'findOne', 'deleteById', 'existsById',
              'findByIdOrNull'}
FIND_BY_ID_PATTERN = re.compile(r'^(find|get|load|delete|read)\w*By\w*Id$')
OWNERSHIP_TYPES = {'Principal', 'Authentication', 'JwtAuthenticationToken', 'UserDetails', 'OAuth2User',
                   'OidcUser', 'Jwt'}
OWNERSHIP_ANNOTATIONS = {'AuthenticationPrincipal', 'CurrentUser', 'CurrentSecurityContext'}
OWNERSHIP_CALLS = {'getAuthentication', 'getPrincipal', 'getCurrentUser', 'currentUser', 'getContext',
                   'getUserPrincipal', 'getRemoteUser', 'isUserInRole', 'getName'}
SENSITIVE_INPUT_FIELDS = {'role', 'roles', 'admin', 'isadmin', 'authorities', 'authority', 'permissions',
                          'privileges', 'superuser', 'issuperuser'}
SENSITIVE_OUTPUT = re.compile(r'(password|passwd|pwd|secret|token|apikey|api_key|credential|salt|passwordhash)$',
                              re.I)
ERROR_DETAIL_CALLS = {'getMessage', 'getLocalizedMessage', 'printStackTrace', 'getStackTrace', 'toString'}

_MATCHER_RULE = re.compile(
    r'(?:requestMatchers|antMatchers|mvcMatchers|pathMatchers|regexMatchers)\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)\s*'
    r'\.\s*(hasRole|hasAnyRole|hasAuthority|hasAnyAuthority|authenticated|permitAll|denyAll|anonymous|'
    r'fullyAuthenticated|rememberMe|access|hasIpAddress)\s*\(([^)]*(?:\([^)]*\)[^)]*)*)\)')
_ANY_REQUEST = re.compile(r'any(?:Request|Exchange)\s*\(\s*\)\s*\.\s*(\w+)\s*\(([^)]*(?:\([^)]*\)[^)]*)*)\)')
_SCOPE = re.compile(r'(?:securityMatcher|antMatcher|regexMatcher|mvcMatcher)\s*\(\s*"([^"]+)"')
_DISABLED = r'(?:\s*\(\s*(?:\w+::disable|\w+\s*->\s*\w+\s*\.\s*disable\s*\(\s*\))\s*\)|\s*\(\s*\)\s*\.\s*disable\s*\(\s*\))'


def _disabled(feature: str, text: str) -> Optional[re.Match]:
    return re.search(r'\b' + feature + _DISABLED, text)


def strip_comments(content: str) -> str:
    """Java source without comments (same length and line breaks, so that offsets and lines are kept)."""
    out = []
    i, n = 0, len(content)
    while i < n:
        c = content[i]
        if c == '"' or c == "'":
            j = i + 1
            while j < n and content[j] != c and content[j] != '\n':
                j += 2 if content[j] == '\\' else 1
            out.append(content[i:j + 1])
            i = j + 1
        elif content.startswith('//', i):
            j = content.find('\n', i)
            j = n if j < 0 else j
            out.append(' ' * (j - i))
            i = j
        elif content.startswith('/*', i):
            j = content.find('*/', i + 2)
            j = n if j < 0 else j + 2
            out.append(re.sub(r'[^\n]', ' ', content[i:j]))
            i = j
        else:
            out.append(c)
            i += 1
    return ''.join(out)


def _strings(text: str) -> List[str]:
    return re.findall(r'"([^"]*)"', text)


def decision(rule: str, args: List[str], raw: str = '') -> Tuple[str, List[str]]:
    """Filter-chain rule -> (level, roles)."""
    if rule in ('permitAll',):
        return 'public', []
    if rule == 'anonymous':
        return 'anonymous', []
    if rule in ('authenticated', 'fullyAuthenticated', 'rememberMe'):
        return 'authenticated', []
    if rule in ('hasRole', 'hasAnyRole'):
        return 'role', [a.removeprefix('ROLE_') for a in args] or [raw.strip()]
    if rule in ('hasAuthority', 'hasAnyAuthority'):
        return 'authority', args or [raw.strip()]
    if rule == 'denyAll':
        return 'denied', []
    if rule == 'access':
        return expression_level(args[0] if args else raw)
    return 'unknown', []


def expression_level(expression: Any) -> Tuple[str, List[str]]:
    """SpEL of @PreAuthorize / access(...) -> (level, roles)."""
    text = ' '.join(str(e) for e in as_list(expression)).strip()
    if not text:
        return 'unknown', []
    if re.fullmatch(r'permitAll(\(\))?', text):
        return 'public', []
    if re.fullmatch(r'denyAll(\(\))?', text):
        return 'denied', []
    roles = re.findall(r"hasA?n?y?Role\s*\(([^)]*)\)", text)
    if roles:
        names = [r for group in roles for r in (re.findall(r"'([^']*)'", group) or [group.strip()])]
        return 'role', [n.removeprefix('ROLE_') for n in names]
    authorities = re.findall(r"hasA?n?y?Authority\s*\(([^)]*)\)", text)
    if authorities:
        return 'authority', [a for group in authorities for a in (re.findall(r"'([^']*)'", group) or [group.strip()])]
    if re.fullmatch(r'is(Fully)?Authenticated\(\)', text):
        return 'authenticated', []
    if re.fullmatch(r'isAnonymous\(\)', text):
        return 'anonymous', []
    return 'expression', []


def ant_covers(broad: str, narrow: str) -> bool:
    """`broad` matches every path matched by `narrow` (subset of AntPathMatcher semantics)."""
    if broad == narrow or broad in ('/**', '**'):
        return True
    if broad.endswith('/**'):
        return narrow.startswith(broad[:-3]) and (len(narrow) == len(broad) - 3 or narrow[len(broad) - 3] == '/')
    return False


class SecurityPosture:
    def __init__(self, context: EnrichmentContext):
        self.context = context
        self.config: Dict[str, Dict[str, Any]] = context.shared.get('config') or {}
        self.findings: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ helpers

    def main_files(self, *extensions: str) -> Iterable[Tuple[str, str]]:
        for path, content in self.context.files(*extensions):
            if '/test/' not in f'/{path}' and '/tests/' not in f'/{path}':
                yield path, content

    def value(self, key: str, profile: str = 'default') -> Optional[str]:
        record = self.config.get(key)
        if not record:
            return None
        value = (record.get('values') or {}).get(profile)
        return None if value is None else str(value)

    def finding(self, finding_id: str, cwe: int, severity: str, title: str, confidence: str = 'medium',
                **details: Any) -> Dict[str, Any]:
        item = {'id': finding_id, 'cwe': cwe, 'severity': severity, 'title': title, 'confidence': confidence,
                **{k: v for k, v in details.items() if v not in (None, '', [], {})}}
        self.findings.append(item)
        return item

    def resolve_role(self, role: str) -> str:
        """`@roles.OWNER_ADMIN` / `Roles.ADMIN` -> the constant value when it is known."""
        if re.fullmatch(r'[\w ]+', role):
            return role
        name = role.rsplit('.', 1)[-1]
        constants = self.context.constants
        for key, value in constants.items():
            if key == name or key.endswith('.' + name) or key.endswith('::' + name):
                return str(value).removeprefix('ROLE_')
        return role

    # ------------------------------------------------------------------ conditions

    def condition(self, attributes_list: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        """Status of a configuration class / bean: active, inactive or conditional (with the reason)."""
        status, reasons = 'active', []
        for attributes in attributes_list:
            on_property = annotation(attributes, 'ConditionalOnProperty')
            if on_property:
                prefix = annotation_value(on_property, 'prefix') or ''
                names = as_list(annotation_value(on_property, 'name', 'value'))
                having = str(annotation_value(on_property, 'havingValue') or '')
                if_missing = bool(annotation_value(on_property, 'matchIfMissing'))
                for name in names:
                    key = f'{prefix}.{name}' if prefix else str(name)
                    value = self.value(key)
                    if value is None:
                        active = if_missing
                    elif having:
                        active = value.lower() == having.lower()
                    else:
                        active = value.lower() != 'false'
                    reasons.append(f"{key}={value if value is not None else '(unset)'}"
                                   f" {'matches' if active else 'does not match'} havingValue={having or '!false'}")
                    profiles = [p for p, v in ((self.config.get(key) or {}).get('values') or {}).items()
                                if p != 'default' and (str(v).lower() == having.lower() if having else
                                                       str(v).lower() != 'false') != active]
                    if not active:
                        status = 'inactive' if not profiles else 'conditional'
                    if profiles:
                        reasons.append(f"differs in profile(s) {', '.join(sorted(profiles))}")
                        if status == 'active':
                            status = 'conditional'
            profile = annotation(attributes, 'Profile')
            if profile:
                wanted = [str(p) for p in as_list(annotation_value(profile, 'value'))]
                active_profiles = [p.strip() for p in (self.value('spring.profiles.active') or '').split(',') if p.strip()]
                if active_profiles:
                    hit = any(p in active_profiles or (p.startswith('!') and p[1:] not in active_profiles)
                              for p in wanted)
                    if not hit:
                        status = 'inactive'
                elif not any(p.startswith('!') or p == 'default' for p in wanted):
                    status = 'conditional' if status == 'active' else status
                reasons.append(f"@Profile({', '.join(wanted)})")
        return {'status': status, 'reasons': reasons}

    # ------------------------------------------------------------------ configuration

    def chains(self) -> List[Dict[str, Any]]:
        found = []
        for type_name, decl in self.context.java_types.items():
            path = decl['_path']
            if '/test/' in f'/{path}':
                continue
            content = self.context.analyses[path].file_node.content or ''
            if 'HttpSecurity' not in content:
                continue
            lines = strip_comments(content).splitlines(keepends=True)
            class_attributes = decl.get('attributes') or {}
            for member in self.context.members_of(type_name):
                attributes = member.get('attributes') or {}
                if not any(str(p.get('type', '')).split('<')[0] in HTTP_SECURITY_TYPES
                           for p in attributes.get('params') or []):
                    continue
                start, end = int(member.get('start_line') or 1), int(member.get('end_line') or 0)
                text = ''.join(lines[start - 1:end or None])
                chain = self.parse_chain(text, path, start)
                order = annotation_value(annotation(attributes, 'Order') or annotation(class_attributes, 'Order'), 'value')
                chain.update(name=member['name'], file=path, line=start,
                             condition=self.condition([class_attributes, attributes]))
                if order is not None:
                    chain['order'] = order
                found.append(chain)
        return sorted(found, key=lambda c: (self._order(c.get('order')), 0 if c.get('scope') else 1, c['file'], c['line']))

    @staticmethod
    def _order(order: Any) -> int:
        try:
            return int(str(order).replace('Ordered.HIGHEST_PRECEDENCE', '-2147483648').replace('Ordered.LOWEST_PRECEDENCE', '2147483647'))
        except ValueError:
            return 0

    def parse_chain(self, text: str, path: str, start_line: int) -> Dict[str, Any]:
        def line_of(offset: int) -> int:
            return start_line + text.count('\n', 0, offset)

        rules: List[Dict[str, Any]] = []
        for match in _MATCHER_RULE.finditer(text):
            matcher_args, rule, rule_args = match.groups()
            method = re.search(r'HttpMethod\.(\w+)', matcher_args)
            patterns = _strings(matcher_args) or (['/**'] if method else [re.sub(r'\s+', '', matcher_args)[:80]])
            level, roles = decision(rule, _strings(rule_args), rule_args)
            rules.append({'patterns': patterns, 'http_method': method.group(1) if method else None, 'rule': rule,
                          'level': level, 'roles': [self.resolve_role(r) for r in roles],
                          'file': path, 'line': line_of(match.start()), '_offset': match.start()})
        for match in _ANY_REQUEST.finditer(text):
            level, roles = decision(match.group(1), _strings(match.group(2)), match.group(2))
            rules.append({'patterns': ['/**'], 'http_method': None, 'rule': match.group(1), 'level': level,
                          'roles': [self.resolve_role(r) for r in roles], 'any_request': True,
                          'file': path, 'line': line_of(match.start()), '_offset': match.start()})
        rules.sort(key=lambda r: int(r['_offset']))
        for rule in rules:
            rule.pop('_offset')
        session = re.search(r'SessionCreationPolicy\.(\w+)', text)
        filters = re.findall(r'addFilter(?:Before|After|At)?\s*\(\s*(?:new\s+)?([\w.]+)', text)
        frame = 'disabled' if _disabled('frameOptions', text) else 'sameOrigin' if re.search(r'frameOptions[^;]*sameOrigin', text) \
            else 'deny (default)'
        chain: Dict[str, Any] = {
            'scope': _SCOPE.findall(text),
            'rules': rules,
            'csrf': {'enabled': not _disabled('csrf', text),
                     'ignoring': _strings(' '.join(re.findall(r'ignoring(?:Request|Ant)Matchers\s*\(([^)]*)\)', text)))},
            'cors': 'disabled' if _disabled('cors', text) else 'configured' if re.search(r'\bcors\s*\(', text) else 'not configured',
            'headers': {'disabled': bool(_disabled('headers', text)), 'frame_options': frame,
                        'content_security_policy': bool(re.search(r'contentSecurityPolicy', text)),
                        'hsts_disabled': bool(_disabled('httpStrictTransportSecurity', text))},
            'session_policy': session.group(1) if session else 'IF_REQUIRED (default)',
            'authentication': [m for m in ('httpBasic', 'formLogin', 'oauth2ResourceServer', 'oauth2Login', 'saml2Login',
                                           'x509', 'rememberMe', 'jwt') if re.search(r'\b' + m + r'\s*\(', text)
                               and not _disabled(m, text)],
            'custom_filters': sorted(set(f.rstrip('(') for f in filters)),
        }
        return chain

    def method_security(self) -> List[Dict[str, Any]]:
        found = []
        for type_name, decl in self.context.java_types.items():
            attributes = decl.get('attributes') or {}
            for name in ('EnableMethodSecurity', 'EnableGlobalMethodSecurity', 'EnableReactiveMethodSecurity'):
                item = annotation(attributes, name)
                if not item:
                    continue
                modern = name != 'EnableGlobalMethodSecurity'
                found.append({'annotation': name, 'class': type_name, 'file': decl['_path'],
                              'line': decl.get('start_line'), 'pre_post': self._flag(item, 'prePostEnabled', modern),
                              'secured': self._flag(item, 'securedEnabled', False),
                              'jsr250': self._flag(item, 'jsr250Enabled', False),
                              'condition': self.condition([attributes])})
        return found

    @staticmethod
    def _flag(item: Dict[str, Any], key: str, default: bool) -> bool:
        value = annotation_value(item, key)
        return default if value is None else str(value).lower() == 'true'

    def ignored_patterns(self) -> List[Dict[str, Any]]:
        found = []
        for path, content in self.main_files('.java'):
            if 'ignoring' not in content:
                continue
            text = strip_comments(content)
            for match in re.finditer(r'ignoring\s*\(\s*\)\s*\.\s*(?:requestMatchers|antMatchers|mvcMatchers)\s*\(([^)]*)\)', text):
                for pattern in _strings(match.group(1)):
                    found.append({'pattern': pattern, 'file': path, 'line': text.count('\n', 0, match.start()) + 1})
        return found

    def cors(self) -> List[Dict[str, Any]]:
        found = []
        for path, content in self.main_files('.java'):
            if 'Cors' not in content and 'CrossOrigin' not in content and 'allowedOrigin' not in content:
                continue
            text = strip_comments(content)
            origins = re.findall(r'(?:setAllowedOrigins|setAllowedOriginPatterns|addAllowedOrigin|addAllowedOriginPattern|'
                                 r'allowedOrigins|allowedOriginPatterns)\s*\(([^;]*?)\)\s*[;.)]', text)
            credentials = re.search(r'(?:setAllowCredentials|allowCredentials)\s*\(\s*true\s*\)', text)
            if origins:
                values = sorted({v for group in origins for v in _strings(group)})
                first = re.search(r'(?:setAllowedOrigins|addAllowedOrigin|allowedOrigins|setAllowedOriginPatterns|'
                                  r'addAllowedOriginPattern|allowedOriginPatterns)\s*\(', text)
                found.append({'kind': 'configuration', 'origins': values, 'credentials': bool(credentials),
                              'file': path, 'line': text.count('\n', 0, first.start()) + 1 if first else None})
            for match in re.finditer(r'@CrossOrigin\b(\s*\(([^)]*)\))?', text):
                args = match.group(2) or ''
                named = re.search(r'(?:origins|value)\s*=\s*(\{[^}]*\}|"[^"]*")', args)
                values = _strings(named.group(1)) if named else _strings(args) if args and '=' not in args else []
                found.append({'kind': '@CrossOrigin', 'origins': values or ['*'],
                              'credentials': bool(re.search(r'allowCredentials\s*=\s*"true"', args)),
                              'file': path, 'line': text.count('\n', 0, match.start()) + 1})
        return found

    def spring_security_present(self, chains: List[Dict[str, Any]], method_security: List[Dict[str, Any]]) -> bool:
        if chains or method_security:
            return True
        return any('org.springframework.security' in content for _, content in self.main_files('.java')) or \
            any('spring-boot-starter-security' in content or 'spring-security' in content
                for _, content in self.context.files('pom.xml', '.gradle', '.gradle.kts'))

    # ------------------------------------------------------------------ risky configuration

    def risky_properties(self) -> List[Dict[str, Any]]:
        rules: List[Tuple[str, Any, int, str, str]] = [
            ('management.endpoints.web.exposure.include', re.compile(r'\*|heapdump|env|threaddump|configprops|shutdown|beans|mappings|loggers'),
             200, 'high', 'Actuator endpoints exposed over HTTP (env / heapdump reveal secrets)'),
            ('management.endpoint.shutdown.enabled', 'true', 749, 'high', 'Actuator shutdown endpoint enabled'),
            ('management.endpoint.env.show-values', 'always', 200, 'high', 'Actuator env shows values'),
            ('management.security.enabled', 'false', 306, 'high', 'Actuator security disabled'),
            ('spring.h2.console.enabled', 'true', 489, 'medium', 'H2 web console enabled'),
            ('spring.h2.console.settings.web-allow-others', 'true', 489, 'high', 'H2 console reachable remotely'),
            ('debug', 'true', 489, 'low', 'Debug mode'),
            ('trace', 'true', 489, 'low', 'Trace mode'),
            ('spring.jpa.show-sql', 'true', 532, 'low', 'SQL statements written to the logs'),
            ('server.error.include-stacktrace', re.compile(r'always|on[-_]param'), 209, 'medium', 'Stack traces in error responses'),
            ('server.error.include-message', 'always', 209, 'low', 'Exception messages in error responses'),
            ('server.error.include-exception', 'true', 209, 'low', 'Exception class in error responses'),
            ('server.servlet.session.cookie.secure', 'false', 614, 'medium', 'Session cookie sent over plain HTTP'),
            ('server.servlet.session.cookie.http-only', 'false', 1004, 'medium', 'Session cookie readable by scripts'),
            ('server.ssl.enabled', 'false', 319, 'low', 'TLS explicitly disabled'),
            ('spring.devtools.remote.secret', None, 489, 'medium', 'DevTools remote access configured'),
        ]
        found = []
        for key, expected, cwe, severity, title in rules:
            record = self.config.get(key)
            if not record or not self._main(record):
                continue
            for profile, value in (record.get('values') or {}).items():
                text = str(value).strip().lower()
                hit = expected is None or (expected.search(text) if isinstance(expected, re.Pattern) else text == expected)
                if hit:
                    found.append(self.finding(f'config.{key}', cwe, severity, title, 'high', property=key,
                                              profile=profile, value=str(value)[:80], file=record.get('file'),
                                              line=record.get('line')))
        for level_key in ('logging.level.root', 'logging.level.org.springframework.security'):
            record = self.config.get(level_key)
            if record and self._main(record):
                for profile, value in (record.get('values') or {}).items():
                    if str(value).strip().upper() in ('DEBUG', 'TRACE'):
                        found.append(self.finding(f'config.{level_key}', 532, 'low', 'Verbose security / root logging',
                                                  'high', property=level_key, profile=profile, value=str(value),
                                                  file=record.get('file'), line=record.get('line')))
        for key, record in sorted(self.config.items()):
            if not record.get('secret') or not self._main(record):
                continue
            for profile, value in (record.get('values') or {}).items():
                text = str(value)
                if text and '${' not in text and text not in ('', '""', "''"):
                    found.append(self.finding('config.secret_literal', 798, 'medium',
                                              'Secret written in the configuration instead of an environment reference',
                                              'medium', property=key, profile=profile, file=record.get('file'),
                                              line=record.get('line')))
                    break
        return found

    @staticmethod
    def _main(record: Dict[str, Any]) -> bool:
        return any('/test/' not in f'/{f}' for f in record.get('files') or [record.get('file', '')])

    # ------------------------------------------------------------------ access matrix

    def endpoint_path(self, node) -> str:
        attributes = node.attributes
        if attributes.get('protocol') == 'graphql' or attributes.get('http_method') == 'GRAPHQL':
            return self.value('spring.graphql.path') or self.value('dgs.graphql.path') or '/graphql'
        return str(attributes.get('path') or '/')

    def chain_decision(self, chain: Dict[str, Any], http_method: str, path: str) -> Optional[Dict[str, Any]]:
        from .spring_web import ant_matches
        if chain['scope'] and not any(ant_matches(p, path) for p in chain['scope']):
            return None
        for rule in chain['rules']:
            if rule['http_method'] and http_method not in (rule['http_method'], 'ANY'):
                continue
            if any(p.startswith('/') and ant_matches(p, path) for p in rule['patterns']):
                return {'source': 'filter_chain', 'chain': chain['name'], 'rule': rule['rule'], 'level': rule['level'],
                        'roles': rule['roles'], 'patterns': rule['patterns'], 'file': rule['file'], 'line': rule['line']}
        return {'source': 'filter_chain', 'chain': chain['name'], 'rule': '(no matching rule)', 'level': 'unknown',
                'roles': [], 'file': chain['file'], 'line': chain['line']}

    def annotation_decision(self, node, method_security: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        items = node.attributes.get('security') or []
        if not isinstance(items, list) or not items:
            return None
        method_level = [i for i in items if i.get('level') == 'method']
        item = (method_level or items)[0]
        name = item.get('annotation')
        value = item.get('value')
        if name in ('PreAuthorize', 'PostAuthorize'):
            level, roles = expression_level(value)
            kind = 'pre_post'
        elif name in ('Secured', 'RolesAllowed'):
            level, roles = 'role', [str(v).removeprefix('ROLE_') for v in as_list(value)]
            kind = 'secured' if name == 'Secured' else 'jsr250'
        elif name == 'PermitAll':
            level, roles, kind = 'public', [], 'jsr250'
        elif name == 'DenyAll':
            level, roles, kind = 'denied', [], 'jsr250'
        else:
            return None
        enabling = [m for m in method_security if m[kind]]
        effective = any(m['condition']['status'] == 'active' for m in enabling)
        reasons = sorted({r for m in enabling if m['condition']['status'] != 'active' for r in m['condition']['reasons']})
        result = {'source': 'annotation', 'annotation': name, 'value': value, 'scope': item.get('level'),
                  'level': level, 'roles': [self.resolve_role(str(r)) for r in roles], 'effective': effective}
        if not effective and enabling:
            result['effective_if'] = reasons or ['a conditional configuration is active']
        return result

    @staticmethod
    def combine(decisions: List[Dict[str, Any]]) -> Tuple[str, List[str]]:
        """Filter chain then effective method annotation: the strictest decision wins."""
        level, roles = 'unknown', []
        for item in decisions:
            if item.get('source') == 'annotation' and not item.get('effective'):
                continue
            if level == 'unknown' or LEVEL_RANK.get(item['level'], -1) > LEVEL_RANK.get(level, -1):
                level, roles = item['level'], list(item.get('roles') or [])
        return level, roles

    def access_matrix(self, chains, method_security, ignored, present) -> Dict[str, int]:
        from .base import ResourceNode
        from .spring_web import ant_matches
        summary: Dict[str, int] = {}
        live = [c for c in chains if c['condition']['status'] == 'active'] or \
            [c for c in chains if c['condition']['status'] == 'conditional']
        others = [c for c in chains if c not in live]
        for node_id, data in list(self.context.graph.nodes(data=True)):
            node = data.get('data')
            if not isinstance(node, ResourceNode) or node.node_type != 'endpoint' or node.attributes.get('external'):
                continue
            http_method = str(node.attributes.get('http_method') or 'ANY')
            path = self.endpoint_path(node)
            decisions: List[Dict[str, Any]] = []
            confidence = 'inferred'
            if any(ant_matches(i['pattern'], path) for i in ignored):
                item = next(i for i in ignored if ant_matches(i['pattern'], path))
                decisions.append({'source': 'web_ignoring', 'level': 'public', 'roles': [], **item})
            elif chains:
                chain_item = next((d for d in (self.chain_decision(c, http_method, path) for c in live) if d), None)
                if chain_item:
                    decisions.append(chain_item)
                    if all(c['condition']['status'] == 'conditional' for c in live):
                        confidence = 'conditional'
            elif present:
                decisions.append({'source': 'spring_boot_default', 'level': 'authenticated', 'roles': []})
            else:
                decisions.append({'source': 'no_spring_security', 'level': 'public', 'roles': []})
            annotation_item = self.annotation_decision(node, method_security)
            if annotation_item:
                decisions.append(annotation_item)
            level, roles = self.combine(decisions)
            access: Dict[str, Any] = {'level': level, 'roles': roles, 'via': decisions, 'confidence': confidence}
            alternatives = []
            unconditional = any(m['condition']['status'] == 'active' and not m['condition']['reasons']
                                for m in method_security)
            for chain in others:
                item = self.chain_decision(chain, http_method, path)
                if item:
                    alt_level, alt_roles = self.combine(
                        [item] + ([annotation_item] if annotation_item and unconditional else []))
                    alternatives.append({'chain': chain['name'], 'status': chain['condition']['status'],
                                         'when': chain['condition']['reasons'], 'level': alt_level, 'roles': alt_roles})
            if alternatives:
                access['alternatives'] = alternatives
            node.attributes['access'] = access
            node.attributes['access']['findings'] = self.endpoint_findings(node, node_id, http_method, path, access,
                                                                           annotation_item)
            summary[level] = summary.get(level, 0) + 1
        return summary

    # ------------------------------------------------------------------ endpoint findings

    def endpoint_findings(self, node, node_id: str, http_method: str, path: str, access: Dict[str, Any],
                          annotation_item: Optional[Dict[str, Any]]) -> List[str]:
        entry = node_id.removeprefix('endpoint:')
        location = {'endpoint': entry, 'file': node.file_path, 'line': node.start_line}
        ids = []

        def add(item: Dict[str, Any]) -> None:
            ids.append(item['id'])

        level = access['level']
        if http_method in STATE_CHANGING and level in ('public', 'anonymous'):
            auth_like = bool(AUTH_PATH.search(path))
            add(self.finding('access.public_state_changing', 306, 'low' if auth_like else 'medium',
                             f'{http_method} endpoint reachable without authentication'
                             + (' (authentication endpoint: expected to be public)' if auth_like else ''),
                             'medium', hint='Check that anonymous callers may perform this change.', **location))
        if annotation_item and not annotation_item['effective']:
            conditional = annotation_item.get('effective_if')
            add(self.finding('access.annotation_not_enforced', 862, 'medium' if conditional else 'high',
                             f"@{annotation_item['annotation']} is not enforced: "
                             + (f"method security is only enabled when the configuration changes ({conditional[0]})"
                                if conditional else 'method security is not enabled'),
                             'high', hint='Enable @EnableMethodSecurity or protect the path in the filter chain; '
                             'check which configuration is deployed.', **location))
        handler = node.attributes.get('handler')
        if handler and _template_vars(path) and level not in ('role', 'authority', 'denied') and \
                not (annotation_item and annotation_item['effective'] and annotation_item['level'] == 'expression'):
            lookup = self.object_lookup(handler)
            if lookup and not self.ownership_signal(handler):
                add(self.finding('access.idor_candidate', 639, 'medium',
                                 f'Object fetched by identifier ({lookup}) with no reference to the current user',
                                 'low', hint='Call the endpoint with the identifier of another user\'s object.',
                                 **location))
        body = node.attributes.get('request_body') or {}
        if body:
            body_type = self.context.resolve(body.get('type_resolved') or body.get('type'), node.file_path or '') \
                if hasattr(self.context, 'resolve') else None
            fields = self.fields_of(body_type)
            jpa = (self.context.entity_attributes(body_type).get('jpa') or {}) if body_type else {}
            sensitive = sorted(f for f in fields if f.lower() in SENSITIVE_INPUT_FIELDS)
            if jpa.get('kind') == 'entity' or sensitive:
                add(self.finding('input.mass_assignment', 915, 'medium',
                                 'Request body bound to ' + ('a JPA entity' if jpa.get('kind') == 'entity'
                                                             else f"a type exposing {', '.join(sensitive)}"),
                                 'medium', type=body_type, fields=sensitive,
                                 hint='Send extra fields (id, role, owner) and check whether they are persisted.',
                                 **location))
            if not body.get('validated') and fields:
                add(self.finding('input.missing_validation', 20, 'low', 'Request body without @Valid', 'medium',
                                 type=body_type, **location))
        response = self.response_type(node) if node.attributes.get('http_method') != 'GRAPHQL' else None  # schema decides
        if response:
            exposed = sorted(f for f, attrs in self.fields_with_attributes(response).items()
                             if SENSITIVE_OUTPUT.search(f) and not self.hidden_from_json(attrs))
            if exposed:
                add(self.finding('output.sensitive_fields', 200, 'medium',
                                 f"Response type {response.rsplit('.', 1)[-1]} serialises {', '.join(exposed)}",
                                 'medium', type=response, fields=exposed, **location))
        return ids

    def response_type(self, node) -> Optional[str]:
        raw = str(node.attributes.get('response_type') or '')
        if not raw or raw in ('void', 'String'):
            return None
        inner = re.findall(r'[\w.]+', raw)
        name = inner[-1] if inner else raw
        resolved = self.context.resolve(name, node.file_path or '') if hasattr(self.context, 'resolve') else None
        return resolved if resolved and resolved in self.context.java_types else None

    def fields_with_attributes(self, type_name: Optional[str]) -> Dict[str, Dict[str, Any]]:
        found: Dict[str, Dict[str, Any]] = {}
        seen: Set[str] = set()
        while type_name and type_name in self.context.java_types and type_name not in seen:
            seen.add(type_name)
            for member in self.context.members_of(type_name):
                attributes = member.get('attributes') or {}
                if member.get('type') == 'variable' and 'static' not in (attributes.get('modifiers') or []):
                    found.setdefault(member['name'].rsplit('::', 1)[-1], attributes)
            bases = self.context.java_types[type_name].get('bases') or []
            type_name = self.context.resolve(bases[0], self.context.java_types[type_name]['_path']) if bases else None
        return found

    def fields_of(self, type_name: Optional[str]) -> List[str]:
        return list(self.fields_with_attributes(type_name))

    @staticmethod
    def hidden_from_json(attributes: Dict[str, Any]) -> bool:
        if annotation(attributes, 'JsonIgnore', 'Transient', 'JsonBackReference'):
            return True
        prop = annotation(attributes, 'JsonProperty')
        return bool(prop and 'WRITE_ONLY' in str(annotation_value(prop, 'access') or ''))

    def reachable(self, method: str, depth: int = 4) -> List[str]:
        graph = self.context.graph
        seen, frontier = {method}, [method]
        for _ in range(depth):
            following = []
            for current in frontier:
                targets = [t for _, t, d in graph.out_edges(current, data=True) if d.get('sites') or d.get('relation') == 'calls']
                owner, _, member = current.partition('::')
                targets += [f'{impl}::{member}' for _, impl, d in graph.out_edges(owner, data=True)
                            if d.get('relation') == 'implemented_by' and member] if owner in graph else []
                for target in targets:
                    if target not in seen and target in graph:
                        seen.add(target)
                        following.append(target)
            frontier = following
        return list(seen)

    def object_lookup(self, handler: str) -> Optional[str]:
        for method in self.reachable(handler):
            for invocation in self.context.entity_attributes(method).get('invocations') or []:
                member = invocation.get('member') or ''
                if member in FIND_BY_ID or FIND_BY_ID_PATTERN.match(member):
                    return f"{str(invocation.get('receiver_type') or invocation.get('receiver') or '').rsplit('.', 1)[-1]}.{member}"
        return None

    def ownership_signal(self, handler: str) -> bool:
        attributes = self.context.entity_attributes(handler)
        for param in attributes.get('params') or []:
            names = {a.get('name') for a in param.get('annotations') or [] if isinstance(a, dict)}
            if names & OWNERSHIP_ANNOTATIONS or str(param.get('type', '')).split('<')[0] in OWNERSHIP_TYPES:
                return True
        for method in self.reachable(handler):
            for invocation in self.context.entity_attributes(method).get('invocations') or []:
                receiver = str(invocation.get('receiver_type') or invocation.get('receiver') or '')
                if 'SecurityContext' in receiver or invocation.get('member') in OWNERSHIP_CALLS - {'getName', 'getContext'}:
                    return True
        return False

    def exception_handler_findings(self) -> None:
        for name, record in self.context.registry.entities.items():
            attributes = record.get('attributes') or {}
            if not (attributes.get('spring') or {}).get('exception_handler'):
                continue
            returns = ' '.join(str(o) for o in (attributes.get('dataflow') or {}).get('returns') or [])
            calls = {i.get('member') for i in attributes.get('invocations') or []}
            leaked = sorted(calls & {'getMessage', 'getLocalizedMessage', 'getStackTrace', 'printStackTrace'})
            if leaked and (returns or 'printStackTrace' in leaked):
                node = self.context.graph.nodes[name]['data'] if name in self.context.graph else None
                self.finding('output.error_details', 209, 'low', f"Exception handler uses {', '.join(leaked)}",
                             'low', method=name, file=getattr(node, 'file_path', None),
                             line=getattr(node, 'start_line', None),
                             hint='Trigger the error and check whether internal details reach the response.')

    # ------------------------------------------------------------------ posture findings

    def chain_findings(self, chains: List[Dict[str, Any]], cors: List[Dict[str, Any]]) -> None:
        for chain in chains:
            if chain['condition']['status'] == 'inactive':
                continue
            where = {'chain': chain['name'], 'file': chain['file'], 'line': chain['line'],
                     'condition': chain['condition']['status']}
            stateless = chain['session_policy'] == 'STATELESS'
            authenticates = bool(chain['authentication'] or chain['custom_filters']) or \
                any(r['level'] not in ('public', 'anonymous') for r in chain['rules'])
            if not chain['csrf']['enabled'] and not stateless and authenticates:
                self.finding('config.csrf_disabled', 352, 'medium',
                             'CSRF protection disabled while sessions or browser credentials may be used'
                             + (f" ({', '.join(chain['authentication'])})" if chain['authentication'] else ''),
                             'medium', hint='A cross-site form can replay authenticated state-changing requests.',
                             **where)
            if chain['headers']['disabled']:
                self.finding('config.headers_disabled', 693, 'medium', 'Security headers disabled', 'high', **where)
            elif chain['headers']['frame_options'] == 'disabled':
                self.finding('config.frame_options_disabled', 1021, 'low', 'X-Frame-Options disabled (clickjacking)',
                             'high', **where)
            if chain['headers']['hsts_disabled']:
                self.finding('config.hsts_disabled', 319, 'low', 'HSTS disabled', 'high', **where)
            for index, rule in enumerate(chain['rules']):
                for later in chain['rules'][index + 1:]:
                    if rule['http_method'] and rule['http_method'] != later['http_method']:
                        continue
                    if later['level'] == rule['level'] and later['roles'] == rule['roles']:
                        continue
                    shadowed = [p for p in later['patterns'] if p.startswith('/') and any(
                        ant_covers(b, p) for b in rule['patterns'] if b.startswith('/'))]
                    if shadowed and len(shadowed) == len([p for p in later['patterns'] if p.startswith('/')]):
                        self.finding('config.shadowed_rule', 284, 'high' if rule['level'] in ('public', 'anonymous') else 'medium',
                                     f"Rule at line {later['line']} ({later['rule']} on {', '.join(shadowed)}) never "
                                     f"applies: an earlier rule at line {rule['line']} ({rule['rule']} on "
                                     f"{', '.join(rule['patterns'])}) matches first", 'high',
                                     file=chain['file'], line=later['line'], chain=chain['name'])
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for item in cors:
            if '*' in item['origins']:
                groups.setdefault('credentials' if item['credentials'] else 'plain', []).append(item)
        for kind, items in sorted(groups.items()):
            locations = [f"{i['file']}:{i['line']}" for i in items]
            if kind == 'credentials':
                self.finding('config.cors_wildcard_credentials', 942, 'high', 'CORS allows any origin with credentials',
                             'high', file=items[0]['file'], line=items[0]['line'], locations=locations[:20])
            else:
                self.finding('config.cors_wildcard', 942, 'low',
                             f"CORS allows any origin ({len(items)} place(s), without credentials)", 'high',
                             file=items[0]['file'], line=items[0]['line'], locations=locations[:20])

    # ------------------------------------------------------------------ run

    def run(self) -> None:
        chains = self.chains()
        method_security = self.method_security()
        present = self.spring_security_present(chains, method_security)
        ignored = self.ignored_patterns()
        cors = self.cors()
        self.chain_findings(chains, cors)
        risky = self.risky_properties()
        access = self.access_matrix(chains, method_security, ignored, present)
        self.exception_handler_findings()
        for chain in chains:
            for rule in chain['rules']:
                rule.pop('file', None)
        posture = {'spring_security': present, 'filter_chains': chains, 'method_security': method_security,
                   'ignored_paths': ignored, 'cors': cors, 'risky_properties': [r['id'] for r in risky],
                   'access_summary': access, 'findings': self.findings}
        if present or access or self.findings:
            self.context.add_resource('security_posture', 'application', posture, name='Security posture')
        self.context.shared['security_posture'] = {'access': access, 'findings': len(self.findings)}


def _template_vars(path: str) -> List[str]:
    return re.findall(r'\{(\w+)', path)


def applies(context: EnrichmentContext) -> bool:
    return any(True for _ in context.java_declarations())


def enrich(context: EnrichmentContext) -> None:
    SecurityPosture(context).run()


__all__ = ['applies', 'enrich', 'strip_comments', 'expression_level', 'decision', 'ant_covers']
