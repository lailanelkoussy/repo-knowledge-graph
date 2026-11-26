"""
Security facts: sources of untrusted input, sinks (dangerous API calls) and how each sink argument is built.

For every Java method, `attributes['security']` lists:

- `sources`: parameters bound from the request / a message (`@RequestParam`, `@RequestBody`, `@InputArgument`,
  `@Payload`..., or unannotated parameters of a handler) and calls returning untrusted data
  (`request.getParameter(...)`, `file.getOriginalFilename()`...);
- `sinks`: calls matching security_catalog.yaml (SQL, commands, expressions, paths, SSRF, redirects, XXE,
  deserialization, LDAP, XPath, logs, headers, weak crypto, hard-coded keys, insecure randomness, trust-all
  TLS), each with its CWE and a `status` computed from the shape and origins of the dangerous argument:

    tainted              the argument comes from a source of this method (request parameter, getParameter())
    depends_on_param     it comes from a parameter of the method: the callers decide (inter-procedural, see `taint`)
    depends_on_field     it comes from a field (configuration, injected value...)
    depends_on_call      it comes from the result of other calls
    unknown              lambda parameter or unresolved name
    sanitized            it went through a sanitizer for this category (Long.parseLong, htmlEscape...)
    safe_by_construction the argument is constant (parameterized SQL, fixed command, literal URL)
    weak_value           a constant known to be weak (MD5, DES, ECB, hard-coded key)
    dangerous_api        the call is risky whatever its arguments (ObjectInputStream.readObject...)
    mitigated            a dangerous API hardened in the same method (XXE features set)

  plus `dynamic: true` when the argument is built by concatenation / format / StringBuilder.
- `sanitizers`: sanitizer calls made by the method.

Statuses are facts about the code, not verdicts: `depends_on_param` sinks are resolved by the inter-procedural
propagation (`taint`), and every sink carries `file:line` for verification.
"""
import logging
import re
from typing import Any, Dict, Iterable, List, Set, Tuple

from ..security_catalog import Catalog, Sink, load_catalog, simple
from .base import EnrichmentContext, annotation, annotation_names, annotation_value
from .taint import propagate, strip_private

logger = logging.getLogger(__name__)

STATUS_RANK = ['tainted', 'weak_value', 'dangerous_api', 'depends_on_param', 'depends_on_field', 'depends_on_call',
               'unknown', 'mitigated', 'sanitized', 'safe_by_construction']
DYNAMIC_SHAPES = ('concat', 'format', 'builder')
LISTENERS = ('KafkaListener', 'RabbitListener', 'JmsListener', 'SqsListener', 'EventListener',
             'TransactionalEventListener', 'StreamListener', 'KafkaHandler', 'RabbitHandler')
TRUST_MANAGERS = ('X509TrustManager', 'X509ExtendedTrustManager', 'TrustManager')
# Values of these types cannot carry an injection payload once converted to text
TYPED_VALUES = {'int', 'long', 'short', 'byte', 'double', 'float', 'boolean', 'char', 'Integer', 'Long', 'Short',
                'Byte', 'Double', 'Float', 'Boolean', 'Character', 'BigDecimal', 'BigInteger', 'Number', 'UUID',
                'LocalDate', 'LocalDateTime', 'LocalTime', 'Instant', 'OffsetDateTime', 'ZonedDateTime', 'Date',
                'Duration', 'AtomicInteger', 'AtomicLong', 'OptionalInt', 'OptionalLong'}
NUMERIC_MEMBERS = {'length', 'size', 'count', 'hashCode', 'ordinal', 'intValue', 'longValue', 'doubleValue',
                   'getTime', 'toEpochMilli', 'getYear', 'compareTo', 'indexOf', 'isEmpty', 'equals', 'contains'}


def applies(context: EnrichmentContext) -> bool:
    return any(True for _ in context.java_declarations())


def worst(statuses: Iterable[str]) -> str:
    statuses = list(statuses) or ['unknown']
    return min(statuses, key=lambda s: STATUS_RANK.index(s) if s in STATUS_RANK else len(STATUS_RANK))


class SecurityEnricher:
    def __init__(self, context: EnrichmentContext, catalog: Catalog):
        self.context = context
        self.catalog = catalog
        self.handlers = self._handlers()
        self.summary: Dict[str, Dict[str, int]] = {'sinks': {}, 'categories': {}, 'sources': {}, 'taint': {}}
        self.by_site: Dict[str, Dict[str, Any]] = {}     # invocations of the method being classified
        self.param_types: Dict[str, str] = {}
        self.typed_values = TYPED_VALUES

    def _typed_value(self, part: Dict[str, Any]) -> bool:
        """A number, boolean, UUID or date: its text cannot change the structure of a query / command."""
        if part.get('shape') == 'param':
            return simple(self.param_types.get(part.get('name') or '')) in TYPED_VALUES
        if part.get('shape') == 'local' and simple(part.get('type')) in TYPED_VALUES:
            return True
        if part.get('shape') in ('call', 'local') and part.get('site') in self.by_site:
            return self._typed_call(self.by_site[part['site']])
        return False

    def _typed_call(self, inv: Dict[str, Any], depth: int = 0) -> bool:
        """The call returns a typed value; `x.toString()` / `String.valueOf(x)` of a typed value counts too."""
        if inv.get('member') in NUMERIC_MEMBERS:
            return True
        if inv.get('member') in ('toString', 'name') and inv.get('receiver_site') in self.by_site and depth < 4:
            return self._typed_call(self.by_site[inv['receiver_site']], depth + 1)
        if simple(inv.get('receiver_type')) in TYPED_VALUES and inv.get('member') in ('toString', 'valueOf'):
            return True
        target = self.context.registry.resolve(inv['name']) if inv.get('name') else None
        if not self.context.declared(target) and inv.get('receiver_type'):
            from ..call_resolution import resolve_member
            target = resolve_member(self.context.registry, inv['receiver_type'], inv.get('member', ''), [])
        record = self.context.registry.entities.get(target) if target else None
        return_type = ((record or {}).get('attributes') or {}).get('return_type')
        return bool(return_type) and simple(return_type) in TYPED_VALUES

    # ------------------------------------------------------------------ entry points of the application

    def _handlers(self) -> Set[str]:
        """Methods invoked by the framework with request / message data."""
        handlers = {t for _, t, d in self.context.graph.edges(data=True) if d.get('relation') == 'handled_by'}
        for name, record in self.context.registry.entities.items():
            attributes = record.get('attributes') or {}
            spring = attributes.get('spring') or {}
            if spring.get('listener') or spring.get('event_listener') or (attributes.get('graphql') or {}).get('root'):
                handlers.add(name)
            if set(annotation_names(attributes)) & set(LISTENERS):
                handlers.add(name)
        return handlers

    def param_sources(self, canonical: str, attributes: Dict[str, Any]) -> List[Dict[str, Any]]:
        sources = []
        handler = canonical in self.handlers
        listener = bool(set(annotation_names(attributes)) & set(LISTENERS)) or \
            bool((attributes.get('spring') or {}).get('listener'))
        for param in attributes.get('params') or []:
            names = [a.get('name') for a in param.get('annotations') or [] if isinstance(a, dict)]
            kind = next((self.catalog.param_annotations[n] for n in names if n in self.catalog.param_annotations), None)
            implicit = False
            if kind is None and handler and simple(param.get('type')) not in self.catalog.framework_param_types \
                    and not set(names) & {'AuthenticationPrincipal', 'CurrentSecurityContext', 'Value'}:
                kind, implicit = ('message' if listener else 'http_request'), True
            if kind:
                source = {'kind': kind, 'param': param.get('name'), 'type': param.get('type')}
                if implicit:
                    source['implicit'] = True
                else:
                    source['annotation'] = next(n for n in names if n in self.catalog.param_annotations)
                if 'Valid' in names or 'Validated' in names:
                    source['validated'] = True
                sources.append(source)
        return sources

    # ------------------------------------------------------------------ classification

    def effective_origins(self, origins: Iterable[str], category: str) -> Tuple[List[str], List[str], bool]:
        """(attacker-relevant origins, call sites, went through a sanitizer for this category)."""
        effective, calls, sanitized = [], [], False
        for origin in origins:
            if origin.startswith('via:'):
                _, member, rest = origin.split(':', 2)
                sanitizer = self.catalog.sanitizer(None, member) or next(
                    (s for s in self.catalog.sanitizers if member in s.members), None)
                if sanitizer and sanitizer.cleans(category):
                    sanitized = True
                    continue
                effective.append(rest)
            elif origin.startswith('call:'):
                calls.append(origin[len('call:'):])
            else:
                effective.append(origin)
        return effective, calls, sanitized

    def classify_arg(self, shape: Dict[str, Any], sink: Sink, method_sources: Set[str],
                     source_sites: Dict[str, str]) -> Dict[str, Any]:
        origins = shape.get('origins') or []
        typed = False
        if shape.get('shape') == 'concat' and shape.get('parts'):
            # Only the parts that are not constant and not typed values (numbers, UUIDs, dates) matter
            origins = []
            for part in shape['parts']:
                if self._typed_value(part):
                    typed = True
                elif part.get('shape') not in ('literal', 'constant'):
                    origins.extend(part.get('origins') or [])
        elif self._typed_value(shape):
            origins, typed = [], True
        effective, calls, sanitized = self.effective_origins(origins, sink.category)
        sanitized = sanitized or typed
        dynamic = shape.get('shape') in DYNAMIC_SHAPES or shape.get('built_by') in DYNAMIC_SHAPES
        result: Dict[str, Any] = {'shape': shape.get('shape'), 'dynamic': dynamic}
        tainted_by = [o for o in effective if o.startswith('param:') and o[len('param:'):] in method_sources]
        tainted_by += [f'source:{source_sites[c]}' for c in calls if c in source_sites]
        if sink.dangerous_values is not None:
            value = shape.get('value')
            if shape.get('constant') and value is not None:
                weak = bool(sink.dangerous_values.search(str(value))) and not (
                    sink.safe_values is not None and sink.safe_values.search(str(value)))
                result.update(status='weak_value' if weak else 'safe_by_construction', value=str(value)[:80])
                return result
            if shape.get('constant'):
                result['status'] = 'weak_value' if sink.category == 'hardcoded_key' else 'safe_by_construction'
                return result
        if tainted_by:
            result.update(status='tainted', origins=tainted_by[:6])
        elif shape.get('constant') and not effective:
            result['status'] = 'safe_by_construction'
        elif typed and not effective and not calls:
            result.update(status='sanitized', typed_values=True)
        elif any(o.startswith('param:') for o in effective):
            result.update(status='depends_on_param', origins=[o for o in effective if o.startswith('param:')][:6])
        elif any(o.startswith('field:') for o in effective):
            result.update(status='depends_on_field', origins=[o for o in effective if o.startswith('field:')][:6])
        elif effective:
            result.update(status='unknown', origins=effective[:6])
        elif sanitized:
            result['status'] = 'sanitized'
        elif calls:
            result['status'] = 'depends_on_call'
        else:
            result['status'] = 'safe_by_construction' if shape.get('constant') else 'unknown'
        if sanitized and result['status'] != 'sanitized':
            result['partially_sanitized'] = True
        return result

    def mitigated(self, sink: Sink, invocations: List[Dict[str, Any]]) -> List[str]:
        found = []
        for member, pattern in sink.mitigated_by:
            for inv in invocations:
                if inv.get('member') != member:
                    continue
                values = [str(s.get('value') or s.get('name') or s.get('text') or '') for s in inv.get('arg_shapes') or []]
                values += [str(a) if not isinstance(a, dict) else str(a.get('ref') or a.get('expr') or '')
                           for a in inv.get('args') or []]
                if any(pattern.search(v) for v in values):
                    found.append(f"{member}({values[0][:60] if values else ''})")
        return found

    def method_sinks(self, canonical: str, attributes: Dict[str, Any], path: str,
                     method_sources: Set[str], source_sites: Dict[str, str]) -> List[Dict[str, Any]]:
        sinks = []
        invocations = attributes.get('invocations') or []
        self.by_site = {inv['site']: inv for inv in invocations if inv.get('site')}
        self.param_types = {p.get('name'): p.get('type') for p in attributes.get('params') or []}
        owner = canonical.split('::', 1)[0]
        context_names = f"{canonical.rsplit('::', 1)[-1]} {owner.rsplit('.', 1)[-1]}"
        for inv in invocations:
            for sink in self.catalog.sinks_for(inv.get('receiver_type'), inv.get('member', '')):
                if sink.context is not None and not sink.context.search(context_names):
                    continue
                if sink.context_exclude is not None and sink.context_exclude.search(canonical.rsplit('::', 1)[-1]):
                    continue
                record: Dict[str, Any] = {
                    'id': sink.id, 'category': sink.category, 'cwe': sink.cwe, 'severity': sink.severity,
                    'api': f"{simple(inv.get('receiver_type'))}.{inv.get('member')}", 'file': path,
                    'line': inv.get('line'), 'site': inv.get('site')}
                shapes = inv.get('arg_shapes') or []
                if sink.always or sink.arg == 'none':
                    mitigations = self.mitigated(sink, invocations)
                    record['status'] = 'mitigated' if mitigations else 'dangerous_api'
                    if mitigations:
                        record['mitigated_by'] = mitigations
                elif sink.arg == 'any':
                    if not shapes:
                        continue
                    classified = [self.classify_arg(s, sink, method_sources, source_sites) for s in shapes]
                    index = min(range(len(classified)), key=lambda i: STATUS_RANK.index(classified[i]['status']))
                    record.update(classified[index], arg_index=index, _shape=shapes[index])
                else:
                    index = int(sink.arg)
                    if index >= len(shapes):
                        continue
                    record.update(self.classify_arg(shapes[index], sink, method_sources, source_sites),
                                  arg_index=index, _shape=shapes[index])
                sinks.append(record)
        sinks.extend(self.redirect_returns(canonical, attributes, path, method_sources))
        return sinks

    def redirect_returns(self, canonical: str, attributes: Dict[str, Any], path: str,
                         method_sources: Set[str]) -> List[Dict[str, Any]]:
        """Handler returning "redirect:" + input (Spring MVC view name)."""
        if canonical not in self.handlers:
            return []
        found = []
        for shape in (attributes.get('dataflow') or {}).get('return_shapes') or []:
            parts = shape.get('parts') or []
            first = str(parts[0].get('value') or '') if parts else ''
            if shape.get('shape') == 'concat' and first.startswith(('redirect:', 'forward:')) and not shape.get('constant'):
                sink = Sink(id='redirect.view_name', category='open_redirect', cwe=601, severity='medium',
                            receivers=frozenset(), members=frozenset())
                record = {'id': sink.id, 'category': sink.category, 'cwe': sink.cwe, 'severity': sink.severity,
                          'api': 'return "redirect:" + ...', 'file': path, 'line': shape.get('line')}
                record.update(self.classify_arg(shape, sink, method_sources, {}), _shape=shape)
                found.append(record)
        return found

    # ------------------------------------------------------------------ structural checks

    def structural_sinks(self) -> Dict[str, List[Dict[str, Any]]]:
        """Trust-all TLS: TrustManager accepting every certificate, HostnameVerifier always true."""
        found: Dict[str, List[Dict[str, Any]]] = {}
        for type_name, decl in self.context.java_types.items():
            bases = {simple(b) for b in decl.get('bases') or []}
            path = decl['_path']
            lines = (self.context.analyses[path].file_node.content or '').splitlines()
            for member in self.context.members_of(type_name):
                name = member.get('name', '').rsplit('::', 1)[-1]
                body = '\n'.join(lines[(member.get('start_line') or 1) - 1:member.get('end_line') or 0])
                canonical = self.context.registry.lookup(member['name'])
                if not canonical:
                    continue
                if bases & set(TRUST_MANAGERS) and name in ('checkServerTrusted', 'checkClientTrusted') \
                        and 'throw' not in body and not (member.get('attributes') or {}).get('invocations'):
                    found.setdefault(canonical, []).append(self._tls(name, path, member, 'tls.trust_all_certificates'))
                if 'HostnameVerifier' in bases and name == 'verify' and re.search(r'return\s+true\s*;', body):
                    found.setdefault(canonical, []).append(self._tls(name, path, member, 'tls.hostname_verifier_true'))
        for path, entity in self.context.java_declarations():
            canonical = self.context.registry.lookup(entity.get('name', ''))
            for inv in (entity.get('attributes') or {}).get('invocations') or []:
                texts = ' '.join(str(s.get('text') or s.get('name') or '') for s in inv.get('arg_shapes') or [])
                if canonical and inv.get('member') in ('setHostnameVerifier', 'setDefaultHostnameVerifier', 'setSSLHostnameVerifier') \
                        and (re.search(r'->\s*true\b', texts) or 'ALLOW_ALL' in texts or 'Noop' in texts):
                    found.setdefault(canonical, []).append(
                        self._tls(inv['member'], path, {'start_line': inv.get('line')}, 'tls.hostname_verifier_true'))
                if canonical and inv.get('member') == '<init>' and simple(inv.get('receiver_type')) in (
                        'NoopHostnameVerifier', 'AllowAllHostnameVerifier', 'TrustAllStrategy', 'TrustSelfSignedStrategy'):
                    found.setdefault(canonical, []).append(
                        self._tls(inv['receiver_type'], path, {'start_line': inv.get('line')}, 'tls.hostname_verifier_true'))
        return found

    @staticmethod
    def _tls(api: str, path: str, member: Dict[str, Any], sink_id: str) -> Dict[str, Any]:
        return {'id': sink_id, 'category': 'trust_all_certificates', 'cwe': 295, 'severity': 'high',
                'api': api, 'file': path, 'line': member.get('start_line'), 'status': 'dangerous_api'}

    def persistence_sinks(self, canonical: str, attributes: Dict[str, Any], path: str) -> List[Dict[str, Any]]:
        """MyBatis statements (${} is string substitution, #{} a bound parameter) and Spring Data @Query."""
        found = []
        mybatis = attributes.get('mybatis') or {}
        sql = mybatis.get('sql') or ''
        if sql:
            placeholders = sorted(set(re.findall(r'\$\{\s*([\w.]+)', sql)))
            record: Dict[str, Any] = {'id': 'sql.mybatis', 'category': 'sql_injection', 'cwe': 89, 'severity': 'high',
                                      'api': f"MyBatis {mybatis.get('statement', 'statement')}",
                                      'file': mybatis.get('source') or path, 'line': mybatis.get('line') or attributes.get('start_line')}
            if placeholders:
                params = self._mybatis_params(attributes)
                roots = [p.split('.')[0] for p in placeholders]
                origins = sorted({f"param:{params.get(r) or params.get('_single') or r}" for r in roots})
                record.update(id='sql.mybatis_dollar', status='depends_on_param', dynamic=True, shape='concat',
                              origins=origins, placeholders=['${' + p + '}' for p in placeholders])
            else:
                record.update(status='safe_by_construction', shape='literal', dynamic=False)
            found.append(record)
        query = (attributes.get('jpa') or {}).get('query') or {}
        if query.get('kind') in ('jpql', 'native') and query.get('text'):
            spel = '#{' in query['text']
            found.append({'id': 'sql.spring_data_query', 'category': 'sql_injection', 'cwe': 89, 'severity': 'high',
                          'api': f"@Query({query['kind']})", 'file': path, 'line': attributes.get('start_line'),
                          'status': 'unknown' if spel else 'safe_by_construction', 'shape': 'literal',
                          'dynamic': spel})
        return found

    @staticmethod
    def _mybatis_params(attributes: Dict[str, Any]) -> Dict[str, str]:
        """MyBatis placeholder root -> Java parameter name (@Param("x") or the parameter name itself)."""
        mapping = {}
        params = attributes.get('params') or []
        for param in params:
            item = annotation({'annotations': param.get('annotations')}, 'Param')
            name = annotation_value(item, 'value') if item else None
            mapping[str(name or param.get('name'))] = param.get('name')
        if len(params) == 1:  # a single parameter object: ${property} refers to it
            mapping.setdefault('_single', params[0].get('name'))
        return mapping

    # ------------------------------------------------------------------ run

    def run(self) -> None:
        context = self.context
        structural = self.structural_sinks()
        methods: Dict[str, Dict[str, Any]] = {}
        for path, entity in context.java_declarations():
            if entity.get('type') not in ('method', 'api_endpoint', 'constructor'):
                continue
            canonical = context.registry.lookup(entity.get('name', ''))
            if not canonical:
                continue
            attributes = context.entity_attributes(canonical)
            sources = self.param_sources(canonical, attributes)
            source_sites: Dict[str, str] = {}
            for inv in attributes.get('invocations') or []:
                kind = self.catalog.source_kind(inv.get('receiver_type'), inv.get('member', ''))
                if kind and inv.get('site'):
                    source_sites[inv['site']] = kind
                    sources.append({'kind': kind, 'call': f"{simple(inv.get('receiver_type'))}.{inv['member']}",
                                    'line': inv.get('line'), 'site': inv['site']})
            method_sources = {s['param'] for s in sources if s.get('param')}
            sinks = self.method_sinks(canonical, attributes, path, method_sources, source_sites)
            sinks += self.persistence_sinks(canonical, attributes, path)
            sinks += structural.get(canonical, [])
            sanitizers = sorted({inv['member'] for inv in attributes.get('invocations') or []
                                 if inv.get('member') in self.catalog.sanitizer_members})
            if not (sources or sinks or sanitizers):
                continue
            security: Dict[str, Any] = {}
            if sources:
                security['sources'] = sources
            if sinks:
                security['sinks'] = sinks
            if sanitizers:
                security['sanitizers'] = sanitizers
            attributes['security'] = security
            methods[canonical] = {'path': path, 'sources': sources, 'source_sites': source_sites, 'sinks': sinks}
        try:
            self.summary['taint'] = propagate(self, methods)
        except RecursionError:  # pathological call chains: keep the intra-procedural statuses
            logger.warning('taint propagation skipped (recursion limit)')
        for facts in methods.values():
            strip_private(facts['sinks'])
            for sink in facts['sinks']:
                self.summary['sinks'][sink['status']] = self.summary['sinks'].get(sink['status'], 0) + 1
                self.summary['categories'][sink['category']] = self.summary['categories'].get(sink['category'], 0) + 1
            for source in facts['sources']:
                self.summary['sources'][source['kind']] = self.summary['sources'].get(source['kind'], 0) + 1
        context.shared['security'] = self.summary


def enrich(context: EnrichmentContext) -> None:
    SecurityEnricher(context, load_catalog()).run()


__all__ = ['applies', 'enrich', 'STATUS_RANK', 'worst']
