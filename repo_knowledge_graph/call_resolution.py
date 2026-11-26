"""
Second-pass resolution of chained Java calls, once every declaration of the repository is known.

During extraction, `em.createQuery(q).getResultList()` or `repository.findById(id).orElseThrow()` only
know the receiver of the first call. Each invocation keeps the site of the call producing its receiver
(`receiver_site`, also set for `var q = em.createQuery(...); q.getResultList()`). This module computes the
result type of every call site, innermost first:

1. call to a method declared in the repository: its declared return type (and generic arguments);
2. call on a library type: the LIBRARY_RETURNS table below (JDK, Jakarta, Spring), where `SELF` keeps the
   receiver type (fluent APIs), `ARG0` / `ARG1` take a generic argument of the receiver (Optional<T>.get());
3. otherwise unknown.

Resolved invocations get `receiver_type`, a `name` when the target is declared in the repository (added to
the method's `calls`, so the call graph gains an edge), and `resolved_by: "chain"`.
"""
import logging
from typing import Any, Dict, List, Optional, Tuple

from .entity_registry import EntityRegistry

logger = logging.getLogger(__name__)

TypeInfo = Tuple[Optional[str], List[str]]  # (resolved type, generic type arguments)

OPTIONAL_UNWRAP = ('get', 'orElse', 'orElseThrow', 'orElseGet', 'getOrNull')
FLUENT = 'SELF'

# (receiver simple type, member) -> result: FQN, 'SELF', 'ARG0', 'ARG1', or (FQN, ['ARG0'...])
LIBRARY_RETURNS: Dict[Tuple[str, str], Any] = {
    **{('Optional', m): 'ARG0' for m in OPTIONAL_UNWRAP},
    ('Optional', 'filter'): FLUENT, ('Optional', 'or'): FLUENT,
    ('Optional', 'of'): ('java.util.Optional', ['ARG0']), ('Optional', 'ofNullable'): ('java.util.Optional', ['ARG0']),
    **{(t, 'get'): 'ARG0' for t in ('List', 'ArrayList', 'LinkedList', 'Collection')},
    ('Iterator', 'next'): 'ARG0', ('Map', 'get'): 'ARG1', ('HashMap', 'get'): 'ARG1', ('Map', 'getOrDefault'): 'ARG1',
    **{(t, 'stream'): ('java.util.stream.Stream', ['ARG0']) for t in ('List', 'ArrayList', 'Set', 'Collection')},
    **{('Stream', m): FLUENT for m in ('filter', 'sorted', 'distinct', 'limit', 'skip', 'peek')},
    ('Stream', 'findFirst'): ('java.util.Optional', ['ARG0']), ('Stream', 'findAny'): ('java.util.Optional', ['ARG0']),
    **{(t, m): FLUENT for t in ('StringBuilder', 'StringBuffer') for m in ('append', 'insert', 'reverse', 'delete')},
    **{(t, 'toString'): 'java.lang.String' for t in ('StringBuilder', 'StringBuffer', 'StringJoiner', 'Object')},
    **{('String', m): 'java.lang.String' for m in ('trim', 'strip', 'toLowerCase', 'toUpperCase', 'substring',
                                                   'replace', 'replaceAll', 'formatted', 'concat', 'format',
                                                   'valueOf', 'join', 'repeat', 'intern')},
    # persistence
    ('EntityManager', 'createQuery'): 'jakarta.persistence.TypedQuery',
    ('EntityManager', 'createNativeQuery'): 'jakarta.persistence.Query',
    ('EntityManager', 'createNamedQuery'): 'jakarta.persistence.TypedQuery',
    **{(t, m): FLUENT for t in ('Query', 'TypedQuery') for m in ('setParameter', 'setMaxResults', 'setFirstResult',
                                                                'setHint', 'setLockMode')},
    ('Session', 'createQuery'): 'org.hibernate.query.Query', ('Session', 'createNativeQuery'): 'org.hibernate.query.NativeQuery',
    ('DataSource', 'getConnection'): 'java.sql.Connection',
    ('Connection', 'createStatement'): 'java.sql.Statement',
    ('Connection', 'prepareStatement'): 'java.sql.PreparedStatement',
    ('Statement', 'executeQuery'): 'java.sql.ResultSet', ('PreparedStatement', 'executeQuery'): 'java.sql.ResultSet',
    # processes, files, network
    ('Runtime', 'getRuntime'): 'java.lang.Runtime', ('Runtime', 'exec'): 'java.lang.Process',
    **{('ProcessBuilder', m): FLUENT for m in ('command', 'directory', 'redirectErrorStream', 'inheritIO',
                                               'redirectOutput', 'redirectInput')},
    ('ProcessBuilder', 'start'): 'java.lang.Process',
    ('HttpServletResponse', 'getWriter'): 'java.io.PrintWriter',
    ('HttpServletResponse', 'getOutputStream'): 'jakarta.servlet.ServletOutputStream',
    ('Paths', 'get'): 'java.nio.file.Path', ('Path', 'of'): 'java.nio.file.Path',
    **{('Path', m): 'java.nio.file.Path' for m in ('resolve', 'normalize', 'toAbsolutePath', 'getParent',
                                                    'resolveSibling', 'relativize', 'toRealPath')},
    ('File', 'toPath'): 'java.nio.file.Path',
    ('URI', 'create'): 'java.net.URI', ('URI', 'toURL'): 'java.net.URL', ('URL', 'openConnection'): 'java.net.URLConnection',
    ('HttpClient', 'newHttpClient'): 'java.net.http.HttpClient',
    ('HttpRequest', 'newBuilder'): 'java.net.http.HttpRequest.Builder',
    **{('Builder', m): FLUENT for m in ('uri', 'header', 'GET', 'POST', 'PUT', 'DELETE', 'timeout')},
    # web
    **{('WebClient', m): 'org.springframework.web.reactive.function.client.WebClient.RequestHeadersUriSpec'
       for m in ('get', 'post', 'put', 'patch', 'delete', 'head', 'options', 'method')},
    **{(t, m): FLUENT for t in ('RequestHeadersUriSpec', 'RequestBodyUriSpec', 'RequestHeadersSpec', 'RequestBodySpec')
       for m in ('uri', 'header', 'headers', 'accept', 'contentType', 'body', 'bodyValue', 'cookie', 'attribute')},
    **{(t, 'retrieve'): 'org.springframework.web.reactive.function.client.WebClient.ResponseSpec'
       for t in ('RequestHeadersUriSpec', 'RequestBodyUriSpec', 'RequestHeadersSpec', 'RequestBodySpec')},
    **{('RestClient', m): 'org.springframework.web.client.RestClient.RequestHeadersUriSpec'
       for m in ('get', 'post', 'put', 'patch', 'delete', 'method')},
    ('HttpServletRequest', 'getParameter'): 'java.lang.String', ('HttpServletRequest', 'getHeader'): 'java.lang.String',
    ('HttpServletRequest', 'getQueryString'): 'java.lang.String', ('HttpServletRequest', 'getRequestURI'): 'java.lang.String',
    ('HttpServletRequest', 'getSession'): 'jakarta.servlet.http.HttpSession',
    ('HttpServletRequest', 'getInputStream'): 'jakarta.servlet.ServletInputStream',
    ('MultipartFile', 'getOriginalFilename'): 'java.lang.String', ('MultipartFile', 'getInputStream'): 'java.io.InputStream',
    # security, crypto, xml
    ('SecurityContextHolder', 'getContext'): 'org.springframework.security.core.context.SecurityContext',
    ('SecurityContext', 'getAuthentication'): 'org.springframework.security.core.Authentication',
    ('MessageDigest', 'getInstance'): 'java.security.MessageDigest', ('Cipher', 'getInstance'): 'javax.crypto.Cipher',
    ('KeyGenerator', 'getInstance'): 'javax.crypto.KeyGenerator', ('Mac', 'getInstance'): 'javax.crypto.Mac',
    ('DocumentBuilderFactory', 'newInstance'): 'javax.xml.parsers.DocumentBuilderFactory',
    ('DocumentBuilderFactory', 'newDocumentBuilder'): 'javax.xml.parsers.DocumentBuilder',
    ('SAXParserFactory', 'newInstance'): 'javax.xml.parsers.SAXParserFactory',
    ('SAXParserFactory', 'newSAXParser'): 'javax.xml.parsers.SAXParser',
    ('XMLInputFactory', 'newInstance'): 'javax.xml.stream.XMLInputFactory',
    ('XMLInputFactory', 'newFactory'): 'javax.xml.stream.XMLInputFactory',
    ('TransformerFactory', 'newInstance'): 'javax.xml.transform.TransformerFactory',
    ('SpelExpressionParser', 'parseExpression'): 'org.springframework.expression.Expression',
    ('ExpressionParser', 'parseExpression'): 'org.springframework.expression.Expression',
    ('ScriptEngineManager', 'getEngineByName'): 'javax.script.ScriptEngine',
    ('ObjectMapper', 'readerFor'): 'com.fasterxml.jackson.databind.ObjectReader',
    ('ResponseEntity', 'ok'): 'org.springframework.http.ResponseEntity',
    ('LoggerFactory', 'getLogger'): 'org.slf4j.Logger',
    ('DataFetcherResult', 'newResult'): 'graphql.execution.DataFetcherResult.Builder',
    ('UriComponentsBuilder', 'fromUriString'): 'org.springframework.web.util.UriComponentsBuilder',
    ('UriComponentsBuilder', 'fromHttpUrl'): 'org.springframework.web.util.UriComponentsBuilder',
    # tests: MockMvc, Mockito, RestAssured (fluent APIs)
    ('MockMvc', 'perform'): 'org.springframework.test.web.servlet.ResultActions',
    **{('ResultActions', m): FLUENT for m in ('andExpect', 'andDo')},
    ('ResultActions', 'andReturn'): 'org.springframework.test.web.servlet.MvcResult',
    **{('MockMvcRequestBuilders', m): 'org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder'
       for m in ('get', 'post', 'put', 'patch', 'delete', 'request', 'multipart')},
    ('Mockito', 'when'): 'org.mockito.stubbing.OngoingStubbing', ('BDDMockito', 'given'): 'org.mockito.BDDMockito.BDDMyOngoingStubbing',
    **{('OngoingStubbing', m): FLUENT for m in ('thenReturn', 'thenThrow', 'thenAnswer', 'thenCallRealMethod')},
    **{('BDDMyOngoingStubbing', m): FLUENT for m in ('willReturn', 'willThrow', 'willAnswer')},
    ('RestAssured', 'given'): 'io.restassured.specification.RequestSpecification',
    ('Assertions', 'assertThat'): 'org.assertj.core.api.AbstractAssert',
    **{('MockMvcResultMatchers', m): f'org.springframework.test.web.servlet.result.{m[0].upper()}{m[1:]}ResultMatchers'
       for m in ('status', 'content', 'jsonPath', 'header', 'model', 'view', 'cookie', 'redirectedUrl')},
}
# Every member of these types returns the same type (fluent DSLs); `build()` of a builder is handled apart
FLUENT_TYPES = ('RequestSpecification', 'Response', 'ValidatableResponse', 'MockHttpServletRequestBuilder',
                'AbstractAssert', 'ObjectAssert', 'StringAssert', 'ListAssert', 'HttpSecurity')


SPRING_DATA_BASES = {'Repository', 'CrudRepository', 'ListCrudRepository', 'PagingAndSortingRepository',
                     'JpaRepository', 'MongoRepository', 'ReactiveCrudRepository', 'R2dbcRepository'}
SPRING_DATA_RETURNS = {
    'findById': ('java.util.Optional', 'T'), 'findOne': ('java.util.Optional', 'T'),
    'getById': ('T', None), 'getReferenceById': ('T', None), 'getOne': ('T', None),
    'save': ('T', None), 'saveAndFlush': ('T', None),
    'findAll': ('java.util.List', 'T'), 'findAllById': ('java.util.List', 'T'), 'saveAll': ('java.util.List', 'T'),
}


def simple(type_name: Optional[str]) -> str:
    return (type_name or '').split('<')[0].rsplit('.', 1)[-1]


def library_result(receiver: TypeInfo, member: str) -> TypeInfo:
    rtype, rargs = receiver
    rule = LIBRARY_RETURNS.get((simple(rtype), member))
    if rule is None:
        name = simple(rtype)
        if name.endswith('Builder') and rtype:
            if member == 'build':
                # pkg.Order.OrderBuilder -> pkg.Order (nested builder), DataFetcherResult.Builder -> DataFetcherResult,
                # UriComponentsBuilder -> UriComponents
                base = rtype.split('<')[0]
                parent = base.rsplit('.', 1)[0] if '.' in base else ''
                if name == 'Builder' or (parent and simple(parent) == name[:-len('Builder')]):
                    return parent, []
                return base[:-len('Builder')], []
            return rtype, rargs
        if name in FLUENT_TYPES or name.endswith('Assert'):
            return rtype, rargs
        if name.endswith('ResultMatchers'):  # status().isOk(), jsonPath("$.id").value(1)
            return 'org.springframework.test.web.servlet.ResultMatcher', []
        return None, []
    if rule == FLUENT:
        return rtype, rargs
    if isinstance(rule, str) and rule.startswith('ARG'):
        index = int(rule[3:])
        return (rargs[index], []) if index < len(rargs) else (None, [])
    if isinstance(rule, tuple):
        name, arg_rules = rule
        return name, [rargs[int(a[3:])] for a in arg_rules if a.startswith('ARG') and int(a[3:]) < len(rargs)]
    return rule, []


class _ChainResolver:
    """Resolves the call sites of the Java methods of one repository (see module docstring)."""

    def __init__(self, registry: EntityRegistry):
        self.registry = registry
        self.stats = {'invocations': 0, 'typed_before': 0, 'typed_after': 0, 'new_calls': 0}

    def spring_data_return(self, receiver: Optional[str], member: str, imports: List[str]) -> TypeInfo:
        """Methods inherited from Spring Data base interfaces (findById -> Optional<T>, save -> T...)."""
        rule = SPRING_DATA_RETURNS.get(member)
        target = self.registry.resolve(receiver, imports) if receiver and rule else None
        attributes = ((self.registry.entities.get(target) or {}).get('attributes') or {}) if target else {}
        for parent in attributes.get('extends') or []:
            if rule and simple(parent.get('name')) in SPRING_DATA_BASES and parent.get('type_args'):
                domain = self.qualify(parent['type_args'][0], imports, attributes.get('package')) or ''
                wrapper, arg = rule
                return (domain, []) if wrapper == 'T' else (wrapper, [domain] if arg else [])
        return None, []

    def qualify(self, type_name: Optional[str], imports: List[str], package: Optional[str] = None) -> Optional[str]:
        """Canonical name of a type declared in the repository ('Order' -> 'a.Order'), else the name itself."""
        if not type_name or '<' in type_name:
            return type_name
        target = self.registry.resolve(type_name, imports + ([package] if package else []))
        if target and self.registry.entities.get(target, {}).get('declaring_chunk_ids') and                 set(self.registry.entities[target].get('type') or []) & {'class', 'interface', 'record', 'enum'}:
            return target
        return type_name

    def declared_return(self, name: Optional[str], imports: List[str], receiver: Optional[str] = None,
                        member: Optional[str] = None) -> TypeInfo:
        target = self.registry.resolve(name, imports) if name else None
        if (not target or not self.registry.entities.get(target, {}).get('declaring_chunk_ids')) and receiver and member:
            target = resolve_member(self.registry, receiver, member, imports)
        if not target or not self.registry.entities.get(target, {}).get('declaring_chunk_ids'):
            return None, []
        attributes = self.registry.entities[target].get('attributes') or {}
        package = self._package_of(target)
        return (self.qualify(attributes.get('return_type_resolved'), imports, package),
                [self.qualify(a, imports, package) or a for a in attributes.get('return_type_args') or []])

    def _package_of(self, canonical: str) -> Optional[str]:
        owner = canonical.split('::', 1)[0]
        attributes = (self.registry.entities.get(owner) or {}).get('attributes') or {}
        return attributes.get('package')

    def method(self, attributes: Dict[str, Any], imports: List[str]) -> None:
        invocations = attributes.get('invocations') or []
        by_site = {inv.get('site'): inv for inv in invocations if inv.get('site')}
        results: Dict[str, TypeInfo] = {}
        self.stats['invocations'] += len(invocations)
        self.stats['typed_before'] += sum(1 for i in invocations if i.get('receiver_type') or i.get('name'))
        for inv in sorted(invocations, key=lambda i: (i.get('line', 0), i.get('column', 0))):
            if inv.get('site'):
                self.result_of(inv, attributes, by_site, results, imports)
        self.stats['typed_after'] += sum(1 for i in invocations if i.get('receiver_type') or i.get('name'))

    def result_of(self, inv: Dict[str, Any], attributes: Dict[str, Any], by_site: Dict[str, Dict[str, Any]],
                  results: Dict[str, TypeInfo], imports: List[str], depth: int = 0) -> TypeInfo:
        site = inv.get('site') or ''
        if site in results:
            return results[site]
        results[site] = (None, [])  # cycle guard
        receiver: TypeInfo = (inv.get('receiver_type'), [])
        if not receiver[0] and inv.get('receiver_site') in by_site and depth < 12:
            receiver = self.result_of(by_site[inv['receiver_site']], attributes, by_site, results, imports, depth + 1)
            if receiver[0]:
                inv['receiver_type'] = receiver[0]
                inv['resolved_by'] = 'chain'
                if not inv.get('name'):
                    candidate = _member_name(receiver[0], inv['member'], self.registry, imports)
                    if candidate:
                        inv['name'] = candidate
                        if candidate not in attributes.setdefault('calls', []):
                            attributes['calls'].append(candidate)
                            self.stats['new_calls'] += 1
        result = self.declared_return(inv.get('name'), imports, receiver[0], inv.get('member'))
        if not result[0] and receiver[0]:
            result = self.spring_data_return(receiver[0], inv['member'], imports)
        if not result[0] and receiver[0]:
            result = library_result(receiver, inv['member'])
        results[site] = result
        return result


def resolve_chained_invocations(analyses: Dict[str, Any], registry: EntityRegistry, import_prefixes) -> Dict[str, int]:
    """Resolve chained calls of every Java method in place; returns counters for logging and tests."""
    resolver = _ChainResolver(registry)
    for path in sorted(analyses):
        if not path.lower().endswith('.java'):
            continue
        file_node = analyses[path].file_node
        imports = import_prefixes(file_node)
        for entity in file_node.declared_entities:
            attributes = entity.get('attributes') if isinstance(entity, dict) else None
            if not attributes or not attributes.get('invocations'):
                continue
            resolver.method(attributes, imports)
            _sync_registry(registry, entity, attributes)
    stats = resolver.stats
    if stats['invocations']:
        logger.debug(f"Chained calls: {stats['typed_before']}/{stats['invocations']} typed before, "
                     f"{stats['typed_after']} after")
    return stats


def _sync_registry(registry: EntityRegistry, entity: Dict[str, Any], attributes: Dict[str, Any]) -> None:
    """The registry keeps a copy of the attributes: report the resolved invocations and calls into it."""
    canonical = registry.lookup(entity.get('name', ''))
    record = registry.entities.get(canonical) if canonical else None
    if not record:
        return
    target = record.setdefault('attributes', {})
    by_site = {inv.get('site'): inv for inv in target.get('invocations') or []}
    for inv in attributes.get('invocations') or []:
        by_site[inv.get('site')] = dict(inv)
    target['invocations'] = list(by_site.values())
    target['calls'] = list(dict.fromkeys(list(target.get('calls') or []) + list(attributes.get('calls') or [])))


def resolve_member(registry: EntityRegistry, receiver: Optional[str], member: str, imports: List[str],
                   depth: int = 0) -> Optional[str]:
    """Canonical method `member` of type `receiver`, looked up in the type then in its supertypes
    (`pet.getId()` where getId is declared in BaseEntity)."""
    if not receiver or depth > 6:
        return None
    for candidate in (f'{receiver}::{member}', f"{simple(receiver)}.{member}"):
        target = registry.resolve(candidate, imports)
        if target and registry.entities.get(target, {}).get('declaring_chunk_ids'):
            return target
    owner = registry.resolve(receiver, imports)
    record = registry.entities.get(owner) if owner else None
    if not record or not record.get('declaring_chunk_ids'):
        return None
    package = (record.get('attributes') or {}).get('package')
    for base in (record.get('attributes') or {}).get('bases') or []:
        found = resolve_member(registry, registry.resolve(base, imports + ([package] if package else [])) or base,
                               member, imports, depth + 1)
        if found:
            return found
    return None


def _member_name(receiver: str, member: str, registry: EntityRegistry, imports: List[str]) -> Optional[str]:
    """Name of `member` on `receiver` when that method is declared in the repository (supertypes included)."""
    for candidate in (f'{receiver}::{member}', f"{simple(receiver)}.{member}"):
        target = registry.resolve(candidate, imports)
        if target and registry.entities.get(target, {}).get('declaring_chunk_ids'):
            return candidate
    return resolve_member(registry, receiver, member, imports)
