"""
Spring web layer: REST endpoints, their security and error handling, and outgoing HTTP calls.

- `endpoint` nodes (`endpoint:<METHOD> <path>`) for every @GetMapping / @PostMapping / ... /
  @RequestMapping handler method, with path / query parameters, request body, response type,
  consumes / produces, status, validation, security; `handled_by` edge to the controller method.
  Constants (`ApiPaths.ORDERS`) and `${...}` placeholders are resolved when their value is known.
- Security: @PreAuthorize / @PostAuthorize / @Secured / @RolesAllowed (method or class) and, with
  confidence 'inferred', the rules of SecurityFilterChain beans (`requestMatchers(...).hasRole(...)`).
- @ControllerAdvice / @ExceptionHandler: `handles` edges (handler method -> exception class).
- Contract-first APIs: operations of OpenAPI specifications matched to their controller methods by the
  `openapi` enricher become endpoints too (prefixed by the controller's class-level @RequestMapping),
  with the summary, parameters and schemas of the specification; unimplemented operations get
  `implemented: false`.
- Outgoing calls: @FeignClient and @HttpExchange interfaces, RestTemplate, WebClient, RestClient and
  MockMvc requests give `calls_endpoint` edges, to the endpoint of the repository when the path
  matches one (monorepos), otherwise to an `external` endpoint node. MockMvc requests target the
  application itself: when no endpoint matches they are only recorded on the test method.
"""
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from ..java_extractor import MAPPING_ANNOTATIONS, combine_paths, resolve_constant_refs
from .base import (
    EnrichmentContext,
    annotation,
    annotation_names,
    annotation_value,
    as_list,
    enum_name,
    strip_class_literal,
)
from .spring_config import resolve_placeholders

logger = logging.getLogger(__name__)

SECURITY_ANNOTATIONS = ('PreAuthorize', 'PostAuthorize', 'Secured', 'RolesAllowed', 'PermitAll', 'DenyAll')
EXCHANGE_ANNOTATIONS = {'GetExchange': 'GET', 'PostExchange': 'POST', 'PutExchange': 'PUT',
                        'PatchExchange': 'PATCH', 'DeleteExchange': 'DELETE', 'HttpExchange': None}
RESPONSE_WRAPPERS = {'ResponseEntity', 'Mono', 'Flux', 'Optional', 'CompletableFuture', 'Callable',
                     'DeferredResult', 'HttpEntity', 'CompletionStage', 'Page', 'Slice'}
REST_TEMPLATE_METHODS = {
    'getForObject': 'GET', 'getForEntity': 'GET', 'postForObject': 'POST', 'postForEntity': 'POST',
    'postForLocation': 'POST', 'put': 'PUT', 'delete': 'DELETE', 'patchForObject': 'PATCH',
    'headForHeaders': 'HEAD', 'optionsForAllow': 'OPTIONS', 'exchange': None, 'execute': None,
}
MOCK_MVC_BUILDERS = 'org.springframework.test.web.servlet.request.MockMvcRequestBuilders'
HTTP_VERBS = {'get': 'GET', 'post': 'POST', 'put': 'PUT', 'patch': 'PATCH', 'delete': 'DELETE',
              'head': 'HEAD', 'options': 'OPTIONS'}
_MATCHER_RULE = re.compile(
    r'(?:requestMatchers|antMatchers|mvcMatchers|pathMatchers|regexMatchers)\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)\s*'
    r'\.\s*(hasRole|hasAnyRole|hasAuthority|hasAnyAuthority|authenticated|permitAll|denyAll|anonymous|'
    r'fullyAuthenticated|rememberMe|access|hasIpAddress)\s*\(([^)]*)\)')
_ANY_REQUEST = re.compile(r'anyRequest\s*\(\s*\)\s*\.\s*(\w+)\s*\(([^)]*)\)')


def applies(context: EnrichmentContext) -> bool:
    if context.shared.get('openapi_operations') or context.shared.get('openapi_unimplemented'):
        return True
    web = set(MAPPING_ANNOTATIONS) | set(EXCHANGE_ANNOTATIONS) | {'FeignClient', 'ExceptionHandler'}
    for _, entity in context.java_declarations():
        attributes = entity.get('attributes') or {}
        if set(annotation_names(attributes)) & web:
            return True
        for invocation in attributes.get('invocations') or []:
            if (invocation.get('receiver_type') or '').rsplit('.', 1)[-1] in ('RestTemplate', 'WebClient', 'RestClient'):
                return True
    return False


# ---------------------------------------------------------------------- helpers

def normalize_path(path: str) -> str:
    path = re.sub(r'\{([^}:]+):[^}]*\}', r'{\1}', path or '/')  # {id:[0-9]+} -> {id}
    path = re.sub(r'/+', '/', path)
    if len(path) > 1:
        path = path.rstrip('/')
    return path or '/'


def path_regex(path: str) -> re.Pattern:
    """Regex matching concrete or templated paths against an endpoint path ('{id}' = one segment)."""
    parts = []
    for segment in normalize_path(path).split('/'):
        if re.fullmatch(r'\{[^}]*\}|\*', segment):
            parts.append(r'[^/]+')
        elif segment == '**':
            parts.append(r'.*')
        else:
            parts.append(re.escape(segment))
    return re.compile('^' + '/'.join(parts) + '/?$')


def ant_matches(pattern: str, path: str) -> bool:
    """Spring AntPathMatcher subset: '**' any number of segments, '*' one segment part, '{x}' one segment."""
    regex = ''
    i = 0
    pattern = normalize_path(pattern)
    while i < len(pattern):
        if pattern.startswith('/**', i):
            regex += '(/.*)?'
            i += 3
        elif pattern.startswith('**', i):
            regex += '.*'
            i += 2
        elif pattern[i] == '*':
            regex += '[^/]*'
            i += 1
        elif pattern[i] == '{':
            end = pattern.find('}', i)
            regex += '[^/]+'
            i = end + 1 if end > 0 else len(pattern)
        else:
            regex += re.escape(pattern[i])
            i += 1
    return re.fullmatch(regex + '/?', normalize_path(path)) is not None


def unwrap_response(type_text: Optional[str]) -> Optional[str]:
    text = (type_text or '').strip()
    for _ in range(3):
        match = re.match(r'^([\w.]+)\s*<(.*)>$', text)
        if not match or match.group(1).rsplit('.', 1)[-1] not in RESPONSE_WRAPPERS:
            break
        text = match.group(2).strip()
    if text in ('?', 'Void', 'void', 'Object'):  # ResponseEntity<?>: the body type is not declared
        return (type_text or '').strip() or None
    return text or None


def _strings(value: Any) -> List[str]:
    return [v for v in as_list(value) if isinstance(v, str)]


def _template_vars(path: str) -> List[str]:
    return re.findall(r'\{([^}:]+)(?::[^}]*)?\}', path or '')


def _clean_rule_args(args: str) -> List[str]:
    return re.findall(r'"([^"]*)"', args)


# ---------------------------------------------------------------------- enrichment

class WebEnricher:
    def __init__(self, context: EnrichmentContext):
        self.context = context
        self.config = context.shared.get('config') or {}
        self.served: Dict[str, Tuple[str, re.Pattern]] = {}  # endpoint id -> (method, path regex)

    def resolve_text(self, value: str) -> str:
        value = resolve_constant_refs(value, self.context.constants)
        return resolve_placeholders(value, self.config)

    def run(self) -> None:
        clients = []
        for path, entity in self.context.java_declarations():
            if entity.get('type') not in ('api_endpoint', 'method'):
                continue
            owner = entity['name'].split('::', 1)[0]
            owner_decl = self.context.java_types.get(self.context.registry.lookup(owner) or owner)
            owner_attributes = (owner_decl or {}).get('attributes') or {}
            if annotation(owner_attributes, 'FeignClient', 'HttpExchange') or (
                    owner_decl and owner_decl.get('type') == 'interface'
                    and set(annotation_names(entity.get('attributes'))) & set(EXCHANGE_ANNOTATIONS)):
                clients.append((path, entity, owner_decl))
            elif entity.get('type') == 'api_endpoint':
                self.add_served(path, entity, owner_attributes)
        self.add_openapi_operations()
        for path, entity, owner_decl in clients:
            self.add_client(path, entity, owner_decl or {})
        self.add_exception_handlers()
        self.add_filter_chain_rules()
        self.add_outgoing_calls()

    # ------------------------------------------------------------------ served endpoints

    def add_served(self, path: str, entity: Dict[str, Any], owner_attributes: Dict[str, Any]) -> List[str]:
        context = self.context
        handler = context.registry.lookup(entity['name'])
        if not handler:
            return []
        attributes = entity.get('attributes') or {}
        mapping: Dict[str, Any] = next((a for a in attributes.get('annotations') or []
                                        if a.get('name') in MAPPING_ANNOTATIONS), {})
        class_mapping = annotation(owner_attributes, 'RequestMapping') or {}
        params = attributes.get('params') or []

        path_params: List[Dict[str, Any]] = []
        query_params: List[Dict[str, Any]] = []
        headers: List[Dict[str, Any]] = []
        cookies: List[Dict[str, Any]] = []
        body: Optional[Dict[str, Any]] = None
        for param in params:
            param_attributes = {'annotations': param.get('annotations')}
            for kind, target in (('PathVariable', path_params), ('RequestParam', query_params),
                                 ('RequestHeader', headers), ('CookieValue', cookies)):
                item = annotation(param_attributes, kind)
                if item:
                    name = annotation_value(item, 'value', 'name')
                    record = {'name': name if isinstance(name, str) and name else param.get('name'),
                              'type': param.get('type')}
                    required = annotation_value(item, 'required')
                    if required is not None:
                        record['required'] = required
                    default = annotation_value(item, 'defaultValue')
                    if default is not None:
                        record['default'] = default
                    target.append(record)
            if annotation(param_attributes, 'RequestBody', 'RequestPart', 'ModelAttribute'):
                body = {'name': param.get('name'), 'type': param.get('type'),
                        'type_resolved': param.get('type_resolved'),
                        'validated': bool(annotation(param_attributes, 'Valid', 'Validated'))}

        security = []
        for source, attrs in (('method', attributes), ('class', owner_attributes)):
            for item in attrs.get('annotations') or []:
                if item.get('name') in SECURITY_ANNOTATIONS:
                    value = annotation_value(item, 'value')
                    security.append({'annotation': item['name'], 'value': value, 'level': source})
        response_status = annotation_value(annotation(attributes, 'ResponseStatus'), 'value', 'code')
        operation = annotation(attributes, 'Operation')
        summary = annotation_value(operation, 'summary') if operation else None
        return_type = attributes.get('return_type')
        produces = _strings(annotation_value(mapping, 'produces')) or _strings(annotation_value(class_mapping, 'produces'))
        consumes = _strings(annotation_value(mapping, 'consumes')) or _strings(annotation_value(class_mapping, 'consumes'))

        endpoint_ids = []
        for item in attributes.get('endpoints') or [{'path': entity.get('endpoint'), 'methods': entity.get('methods')}]:
            full_path = normalize_path(self.resolve_text(item.get('path') or '/'))
            for http_method in item.get('methods') or ['ANY']:
                key = f'{http_method} {full_path}'
                endpoint_id = context.add_resource('endpoint', key, {
                    'http_method': http_method,
                    'path': full_path,
                    'path_params': path_params or [{'name': v} for v in _template_vars(full_path)],
                    'query_params': query_params,
                    'headers': headers,
                    'cookies': cookies,
                    'request_body': body,
                    'response_type': unwrap_response(return_type),
                    'return_type': return_type,
                    'consumes': consumes,
                    'produces': produces,
                    'status': enum_name(response_status) if response_status else None,
                    'validated': bool(body and body['validated']) or bool(annotation(owner_attributes, 'Validated')),
                    'deprecated': bool(annotation(attributes, 'Deprecated')),
                    'security': security,
                    'summary': summary or attributes.get('javadoc'),
                    'controller': context.registry.lookup(entity['name'].split('::', 1)[0]),
                    'handler': handler,
                    'throws': attributes.get('throws') or [],
                    'external': False,
                }, file_path=path, start_line=int(entity.get('start_line') or 0))
                context.add_edge(endpoint_id, handler, 'handled_by')
                self.served[endpoint_id] = (http_method, path_regex(full_path))
                endpoint_ids.append(endpoint_id)
        spring = context.framework_attributes(handler, 'spring')
        spring['endpoints'] = list(dict.fromkeys(list(spring.get('endpoints') or []) + endpoint_ids))
        return endpoint_ids

    # ------------------------------------------------------------------ OpenAPI (contract-first)

    def add_openapi_operations(self) -> None:
        context = self.context
        declarations = {context.registry.lookup(e['name']): (p, e) for p, e in context.java_declarations()
                        if e.get('type') in ('method', 'api_endpoint')}
        tag_bases: Dict[str, set] = {}  # tag -> class-level prefixes of the controllers implementing it
        for operation in context.shared.get('openapi_operations') or []:
            handler = operation['handler']
            if handler not in declarations:
                continue
            path, entity = declarations[handler]
            owner = entity['name'].split('::', 1)[0]
            owner_attributes = (context.java_types.get(context.registry.lookup(owner) or owner) or {}).get(
                'attributes') or {}
            bases = self._mapping_paths(owner_attributes)
            for tag in operation.get('tags') or []:
                tag_bases.setdefault(tag, set()).update(bases)
            routes = [{'path': combine_paths(base, operation['path']), 'methods': [operation['method']]}
                      for base in bases]
            synthetic = {**entity, 'endpoint': routes[0]['path'], 'methods': [operation['method']],
                         'attributes': {**(entity.get('attributes') or {}), 'endpoints': routes}}
            for endpoint_id in self.add_served(path, synthetic, owner_attributes):
                self._apply_spec(endpoint_id, operation)
        for operation in context.shared.get('openapi_unimplemented') or []:
            # Same prefix as the implemented operations of its tag(s), when they agree on one
            prefixes = set().union(*(tag_bases.get(t, set()) for t in operation.get('tags') or [])) \
                if operation.get('tags') else set()
            route = normalize_path(combine_paths(next(iter(prefixes)), operation['path'])
                                   if len(prefixes) == 1 else operation['path'])
            key = f"{operation['method']} {route}"
            endpoint_id = context.add_resource('endpoint', key, {
                'http_method': operation['method'], 'path': route, 'external': False,
                'implemented': False, 'ambiguous_handlers': operation.get('ambiguous_handlers')},
                file_path=operation['spec_file'])
            self._apply_spec(endpoint_id, operation)

    @staticmethod
    def _mapping_paths(owner_attributes: Dict[str, Any]) -> List[str]:
        item = annotation(owner_attributes, 'RequestMapping')
        paths = _strings(annotation_value(item, 'value', 'path')) if item else []
        return paths or ['']

    def _apply_spec(self, endpoint_id: str, operation: Dict[str, Any]) -> None:
        attributes = self.context.graph.nodes[endpoint_id]['data'].attributes
        parameters = operation.get('parameters') or {}
        attributes['source'] = 'openapi'
        attributes['openapi'] = {k: operation.get(k) for k in ('spec_file', 'operation_id', 'tags', 'responses',
                                                              'security', 'match', 'servers')
                                 if operation.get(k) not in (None, [], {})}
        if operation.get('summary'):
            attributes['summary'] = operation['summary']
        if parameters.get('path'):
            attributes['path_params'] = parameters['path']
        if parameters.get('query'):
            attributes['query_params'] = parameters['query']
        if parameters.get('header'):
            attributes['headers'] = parameters['header']
        body = operation.get('request_body')
        if body and body.get('schema'):
            attributes['request_body'] = {**(attributes.get('request_body') or {}), 'schema': body['schema']}
        success = next((v for k, v in sorted(operation.get('responses', {}).items()) if k.startswith('2')), None)
        if success and not attributes.get('response_schema'):
            attributes['response_schema'] = success
        if operation.get('deprecated'):
            attributes['deprecated'] = True

    # ------------------------------------------------------------------ outgoing calls

    def match_served(self, http_method: Optional[str], url: str) -> Optional[str]:
        path = re.sub(r'^[a-z]+://[^/]+', '', url or '', flags=re.I)
        path = re.sub(r'\?.*$', '', path)
        path = normalize_path(re.sub(r'%[sd]', '{x}', path))
        for endpoint_id, (method, regex) in self.served.items():
            if http_method and method not in (http_method, 'ANY'):
                continue
            if regex.match(path) or path_regex(path).match(endpoint_id.split(' ', 1)[1]):
                return endpoint_id
        return None

    def link_call(self, caller: str, http_method: Optional[str], url: str, via: str, **attributes: Any) -> None:
        if not url:
            return
        url = self.resolve_text(url)
        target = self.match_served(http_method, url)
        if target is None and via == 'mockmvc':
            # MockMvc calls the application itself: never an external endpoint
            requests = self.context.framework_attributes(caller, 'test').setdefault('unmatched_requests', [])
            if f'{http_method} {url}' not in requests:
                requests.append(f'{http_method} {url}')
            return
        if target is None:
            key = f"{http_method or 'ANY'} {url}"
            target = self.context.add_resource('endpoint', key, {
                'http_method': http_method or 'ANY', 'path': url, 'external': True, 'client': via,
                **{k: v for k, v in attributes.items() if v is not None}})
        self.context.add_edge(caller, target, 'calls_endpoint', via=via, http_method=http_method)

    def add_client(self, path: str, entity: Dict[str, Any], owner_decl: Dict[str, Any]) -> None:
        method = self.context.registry.lookup(entity['name'])
        if not method:
            return
        owner_attributes = owner_decl.get('attributes') or {}
        feign = annotation(owner_attributes, 'FeignClient')
        exchange = annotation(owner_attributes, 'HttpExchange')
        base = ''
        client = None
        if feign:
            client = annotation_value(feign, 'name', 'value', 'contextId')
            url = annotation_value(feign, 'url')
            prefix = annotation_value(feign, 'path') or ''
            base = combine_paths(str(url) if isinstance(url, str) else '', str(prefix)) if (url or prefix) else ''
        elif exchange:
            base = annotation_value(exchange, 'url', 'value') or ''
        attributes = entity.get('attributes') or {}
        for item in attributes.get('annotations') or []:
            name = item.get('name')
            if name in MAPPING_ANNOTATIONS or name in EXCHANGE_ANNOTATIONS:
                verb = MAPPING_ANNOTATIONS.get(name) or EXCHANGE_ANNOTATIONS.get(name)
                if not verb:
                    verb = enum_name((_strings(annotation_value(item, 'method')) or ['ANY'])[0]).upper()
                paths = _strings(annotation_value(item, 'value', 'path', 'url')) or ['']
                for sub in paths:
                    full = combine_paths(base, sub) if base and not str(base).startswith('http') else (str(base) + sub)
                    self.link_call(method, verb, full, 'feign' if feign else 'http_interface', service=client)
                self.context.framework_attributes(method, 'spring')['http_client'] = client or base or True

    def add_outgoing_calls(self) -> None:
        context = self.context
        for path, entity in context.java_declarations():
            attributes = entity.get('attributes') or {}
            invocations = attributes.get('invocations') or []
            if not invocations:
                continue
            caller = context.registry.lookup(entity['name'])
            if not caller:
                continue
            verbs: List[Tuple[int, str]] = []
            pending: List[Tuple[int, str]] = []
            mock_mvc_wildcard = MOCK_MVC_BUILDERS + '.*' in (context.analyses[path].file_node.imports or [])
            for invocation in invocations:
                receiver = (invocation.get('receiver_type') or invocation.get('receiver') or '')
                receiver_simple = receiver.rsplit('.', 1)[-1]
                member = invocation.get('member')
                args = invocation.get('args') or []
                first = args[0] if args else None
                url = first if isinstance(first, str) else (first.get('ref') if isinstance(first, dict) else None)
                lowered = receiver_simple.lower()
                if lowered in ('resttemplate', 'testresttemplate') or lowered.endswith('resttemplate'):
                    if member in REST_TEMPLATE_METHODS and isinstance(url, str):
                        verb = REST_TEMPLATE_METHODS[member]
                        if verb is None and len(args) > 1 and isinstance(args[1], dict):
                            verb = enum_name(args[1].get('ref') or args[1].get('expr') or '').upper() or None
                        self.link_call(caller, verb, url, 'rest_template')
                elif lowered in ('webclient', 'restclient', 'webtestclient') or lowered.endswith(('webclient', 'restclient')):
                    if member in HTTP_VERBS:
                        verbs.append((int(invocation.get('line') or 0), HTTP_VERBS[member]))
                elif member == 'uri' and isinstance(url, str):
                    pending.append((int(invocation.get('line') or 0), url))
                elif member in HTTP_VERBS and isinstance(url, str) and (
                        (invocation.get('name') or '').startswith(MOCK_MVC_BUILDERS + '::')
                        or invocation.get('name') == member and mock_mvc_wildcard):
                    self.link_call(caller, HTTP_VERBS[member], url, 'mockmvc')
            # WebClient / RestClient: '.get().uri("/x")' chains (verb and uri may be on different lines)
            for line, url in pending:
                candidates = [v for v in verbs if v[0] <= line] or verbs
                if candidates:
                    verb = max(candidates)[1]
                    self.link_call(caller, verb, url, 'web_client')

    # ------------------------------------------------------------------ errors & security

    def add_exception_handlers(self) -> None:
        context = self.context
        for path, entity in context.java_declarations():
            attributes = entity.get('attributes') or {}
            item = annotation(attributes, 'ExceptionHandler')
            if not item:
                continue
            handler = context.registry.lookup(entity['name'])
            if not handler:
                continue
            exceptions = [strip_class_literal(v) for v in as_list(annotation_value(item, 'value'))]
            if not exceptions:
                exceptions = [p.get('type') for p in attributes.get('params') or []
                              if str(p.get('type', '')).endswith(('Exception', 'Error', 'Throwable'))]
            resolved_names = []
            for exception in exceptions:
                if not exception:
                    continue
                resolved = context.resolve(exception, path) or exception
                resolved_names.append(resolved)
                if context.declared(resolved):
                    context.add_edge(handler, resolved, 'handles')
            owner = entity['name'].split('::', 1)[0]
            owner_attributes = (context.java_types.get(context.registry.lookup(owner) or owner) or {}).get('attributes')
            status = annotation_value(annotation(attributes, 'ResponseStatus'), 'value', 'code')
            context.framework_attributes(handler, 'spring')['exception_handler'] = {
                'exceptions': resolved_names,
                'status': enum_name(status) if status else None,
                'global': bool(annotation(owner_attributes, 'ControllerAdvice', 'RestControllerAdvice')),
            }

    def add_filter_chain_rules(self) -> None:
        rules = security_rules(self.context)
        if not rules:
            return
        for endpoint_id in self.served:
            node = self.context.graph.nodes[endpoint_id]['data']
            for rule in rules:
                if rule['http_method'] and rule['http_method'] not in (node.attributes.get('http_method'), 'ANY'):
                    continue
                if any(ant_matches(p, node.attributes.get('path', '/')) for p in rule['patterns']):
                    node.attributes['security_filter_chain'] = {
                        'rule': rule['rule'], 'args': rule['args'], 'patterns': rule['patterns'],
                        'file': rule['file'], 'line': rule['line'], 'confidence': 'inferred'}
                    break


def security_rules(context: EnrichmentContext) -> List[Dict[str, Any]]:
    """Authorization rules of the SecurityFilterChain beans, in declaration order (cached in context.shared)."""
    if 'security_rules' in context.shared:
        return context.shared['security_rules']
    from .spring_security import strip_comments
    rules: List[Dict[str, Any]] = []
    for path, content in context.files('.java'):
        if '/test/' in f'/{path}':
            continue
        content = strip_comments(content)  # commented-out configurations are not rules
        if 'SecurityFilterChain' not in content and 'WebSecurityConfigurerAdapter' not in content \
                and 'authorizeHttpRequests' not in content and 'authorizeExchange' not in content:
            continue
        for match in _MATCHER_RULE.finditer(content):
            matcher_args, rule, rule_args = match.groups()
            method = re.search(r'HttpMethod\.(\w+)', matcher_args)
            patterns = _clean_rule_args(matcher_args) or (['/**'] if method else [])
            rules.append({'patterns': patterns, 'http_method': method.group(1) if method else None,
                          'rule': rule, 'args': _clean_rule_args(rule_args), 'file': path,
                          'line': content.count('\n', 0, match.start()) + 1})
        for match in _ANY_REQUEST.finditer(content):
            rules.append({'patterns': ['/**'], 'http_method': None, 'rule': match.group(1),
                          'args': _clean_rule_args(match.group(2)), 'file': path, 'any_request': True,
                          'line': content.count('\n', 0, match.start()) + 1})
    rules.sort(key=lambda r: (r['file'], r['line']))  # declaration order: anyRequest() comes last
    context.shared['security_rules'] = rules
    return rules


def enrich(context: EnrichmentContext) -> None:
    WebEnricher(context).run()
