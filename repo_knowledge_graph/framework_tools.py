"""
MCP tools over the framework knowledge (Java / Spring Boot enrichment, see enrichers/).

Registered by KnowledgeGraphMCPServer.register_tools; every tool returns a dict with a short `text`
summary plus structured data, is paginated with limit / offset where it lists things, and returns a
structured error instead of raising (see KnowledgeGraphMCPServer._tool).
"""
import json
from typing import TYPE_CHECKING, Annotated, Any, Dict, Literal, Optional

from pydantic import Field

from . import framework_queries as fq

if TYPE_CHECKING:  # pragma: no cover
    from .mcp_server import KnowledgeGraphMCPServer

MAX_TEXT_ITEMS = 60


def _compact(value: Any, size: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= size else text[:size] + '...'


def _endpoint_line(attributes: Dict[str, Any], node_id: str) -> str:
    security = [f"@{s['annotation']}({s.get('value') or ''})" for s in attributes.get('security') or []]
    chain = attributes.get('security_filter_chain')
    if chain:
        security.append(f"filter chain: {chain['rule']}({', '.join(chain.get('args') or [])})")
    handler = attributes.get('handler') or ('external' if attributes.get('external') else '?')
    return (f"- {attributes.get('http_method')} {attributes.get('path')} -> {handler}"
            + (f"  [{'; '.join(security)}]" if security else '')
            + (f"  ({attributes.get('client')})" if attributes.get('external') and attributes.get('client') else ''))


def register_framework_tools(server: 'KnowledgeGraphMCPServer') -> None:
    kg = server.knowledge_graph
    tool = server._tool
    if getattr(server, 'osv_client', None) is None:
        from .vulnerability_lookup import OsvClient
        server.osv_client = OsvClient()  # network only with OSV_LOOKUP=1

    @tool("Overview of the project: languages, build modules, Java and Spring Boot versions, number of beans by "
          "stereotype, endpoints, JPA entities, repositories, tables, messaging channels, configuration properties, "
          "profiles and tests. Call it first on a Java / Spring Boot repository.")
    async def get_project_overview() -> dict:
        overview = fq.project_overview(kg)
        return {**overview, 'text': fq.format_overview(overview)}

    @tool("List the REST endpoints of the repository (Spring MVC / WebFlux, plus Flask, FastAPI, Django REST, "
          "Actix... routes when enriched) with HTTP method, path, handler method and security. "
          "include_external=true also lists outgoing calls (Feign clients, RestTemplate, WebClient...).")
    async def list_endpoints(
        http_method: Annotated[Optional[str], Field(description="GET, POST, PUT, PATCH, DELETE...")] = None,
        path_prefix: Annotated[Optional[str], Field(description="Only paths starting with this prefix.")] = None,
        include_external: Annotated[bool, Field(description="Also list outgoing (external) endpoints.")] = False,
        limit: Annotated[int, Field(description="Maximum number of endpoints.")] = 50,
        offset: Annotated[int, Field(description="Number of endpoints to skip.")] = 0,
    ) -> dict:
        nodes = fq.list_endpoints(kg, http_method, path_prefix, include_external)
        page, info = server._page(nodes, limit, offset)
        items = [{'id': n.id, 'http_method': n.attributes.get('http_method'), 'path': n.attributes.get('path'),
                  'handler': n.attributes.get('handler'), 'location': fq.location(n),
                  'external': bool(n.attributes.get('external')),
                  'secured': bool(n.attributes.get('security') or n.attributes.get('security_filter_chain'))}
                 for n in page]
        if not nodes:
            text = 'No endpoints found' + (' (was the graph built with the Spring enrichers?)' if not
                                           fq.resources(kg, 'endpoint') else ' matching the filters') + '.'
            return {'endpoints': [], 'count': 0, **info, 'text': text}
        text = f"{info['total']} endpoint(s), showing {len(page)}:\n" + '\n'.join(
            _endpoint_line(n.attributes, n.id) for n in page)
        return {'endpoints': items, 'count': len(items), **info, 'text': text}

    @tool("Details of one endpoint: handler (file:line), path / query parameters, request body, response type, "
          "consumes / produces, status, validation, security (annotations and SecurityFilterChain rules), "
          "exception handlers and known callers. Accepts 'GET /api/orders/{id}', a concrete path "
          "('GET /api/orders/42') or an endpoint id.")
    async def get_endpoint(
        endpoint: Annotated[str, Field(description="Endpoint, e.g. 'POST /api/orders' or 'endpoint:GET /x'.")],
    ) -> dict:
        endpoint_id = fq.find_endpoint(kg, endpoint)
        details = fq.endpoint_details(kg, endpoint_id)
        lines = [f"{details.get('http_method')} {details.get('path')}"
                 + (' (external)' if details.get('external') else '')
                 + (f" (served on {details['http_path']})" if details.get('http_path') else '')]
        for handler in details.get('handlers') or []:
            lines.append(f"Handler: {handler['id']}" + (f" [{handler['location']}]" if handler.get('location') else ''))
        if details.get('implemented') is False:
            lines.append('Not implemented: declared in the OpenAPI specification only')
        for key, label in (('path_params', 'Path params'), ('query_params', 'Query params'),
                           ('headers', 'Headers'), ('request_body', 'Body'), ('response_type', 'Response'),
                           ('response_schema', 'Response schema'), ('openapi', 'OpenAPI'),
                           ('arguments', 'GraphQL arguments'), ('schema_file', 'Schema'),
                           ('status', 'Status'), ('consumes', 'Consumes'), ('produces', 'Produces'),
                           ('security', 'Security'), ('security_filter_chain', 'Filter chain'),
                           ('exception_handlers', 'Exception handlers'), ('callers', 'Callers'),
                           ('summary', 'Summary')):
            if details.get(key) not in (None, [], {}, ''):
                lines.append(f'{label}: {_compact(details[key], 300)}')
        return {**details, 'text': '\n'.join(lines)}

    @tool("Trace the execution flow from an endpoint, a method or a messaging channel: handler -> calls -> "
          "interface implementations -> repositories -> JPA entities -> tables, plus published messages / events "
          "and their consumers and outgoing HTTP calls. Returns an indented tree.")
    async def trace_flow(
        start: Annotated[str, Field(description="Endpoint ('POST /api/orders'), method / class name, or channel id.")],
        max_depth: Annotated[int, Field(description="Maximum depth (1-10).")] = 6,
        limit: Annotated[int, Field(description="Maximum number of steps.")] = 60,
        include_trivial: Annotated[bool, Field(description="Also show calls to fields / record accessors and "
                                                           "Lombok getters / setters.")] = False,
    ) -> dict:
        server._validate_positive_int(max_depth, 'max_depth', maximum=10)
        server._validate_positive_int(limit, 'limit')
        result = fq.trace_flow(kg, start, max_depth=max_depth, limit=limit, include_trivial=include_trivial)
        if result['truncated']:
            result['text'] += f'\n... truncated at {limit} steps (raise limit or start deeper).'
        return result

    @tool("List the Spring beans: stereotype (service, repository, rest_controller, configuration, bean_method...), "
          "bean name, type, scope, profiles, conditions, @Primary / @Qualifier.")
    async def list_beans(
        stereotype: Annotated[Optional[str], Field(description="component, service, repository, controller, "
                                                               "rest_controller, configuration, bean_method...")] = None,
        profile: Annotated[Optional[str], Field(description="Only beans restricted to this @Profile.")] = None,
        limit: Annotated[int, Field(description="Maximum number of beans.")] = 50,
        offset: Annotated[int, Field(description="Number of beans to skip.")] = 0,
    ) -> dict:
        beans = fq.list_beans(kg, stereotype, profile)
        page, info = server._page(beans, limit, offset)
        text = f"{info['total']} bean(s), showing {len(page)}:\n" + '\n'.join(
            f"- [{b['stereotype']}] {b['id']} (name: {b.get('bean_name')})"
            + (f" profiles={b['profiles']}" if b.get('profiles') else '') + (' @Primary' if b.get('primary') else '')
            for b in page)
        return {'beans': page, 'count': len(page), **info, 'text': text if beans else 'No Spring beans found.'}

    @tool("Dependencies of a bean: the beans it injects (constructor, field, setter, @Bean parameter) and the beans "
          "it is injected into, with the resolution confidence (exact, inferred, primary, qualifier, by_name, "
          "collection, ambiguous) and unresolved injection points.")
    async def get_bean_dependencies(
        bean: Annotated[str, Field(description="Bean class or @Bean method, e.g. 'OrderController'.")],
        direction: Annotated[Literal['both', 'injects', 'injected_into'], Field(description="Direction.")] = 'both',
        depth: Annotated[int, Field(description="Transitive depth (1-5).")] = 1,
    ) -> dict:
        server._validate_positive_int(depth, 'depth', maximum=5)
        result = fq.bean_dependencies(kg, bean, direction, depth)
        lines = [f"Bean {result['bean']}" + (f" [{result['location']}]" if result.get('location') else '')]
        for item in result.get('injects') or []:
            lines.append(f"{'  ' * item['depth']}injects -> {item['bean']} (via {item.get('via')}, "
                         f"{item.get('point')}, {item.get('confidence')})")
        for item in result.get('injected_into') or []:
            lines.append(f"{'  ' * item['depth']}injected into <- {item['bean']} (via {item.get('via')}, "
                         f"{item.get('confidence')})")
        for item in result.get('unresolved_injections') or []:
            lines.append(f"  unresolved: {item['name']}: {item['type']} (bean provided outside the repository?)")
        return {**result, 'text': '\n'.join(lines)}

    @tool("Implementations and subtypes of an interface or class (transitive), e.g. which classes implement "
          "OrderService.")
    async def find_implementations(
        type_name: Annotated[str, Field(description="Interface or class name.")],
    ) -> dict:
        result = fq.find_implementations(kg, type_name)
        text = f"Implementations of {result['type']}:\n" + ('\n'.join(
            f"- {i['id']}" + ('' if i['direct'] else ' (indirect)') + (f" [{i['location']}]" if i.get('location') else '')
            for i in result['implementations']) or '- none')
        if result['subtypes']:
            text += '\nSubtypes:\n' + '\n'.join(f"- {s['id']} (extends {s['parent']})" for s in result['subtypes'])
        return {**result, 'text': text}

    @tool("Entities (classes, methods, fields) carrying an annotation, with its arguments, e.g. 'Transactional', "
          "'Scheduled', 'PreAuthorize' or a qualified name 'org.springframework.transaction.annotation.Transactional'.")
    async def find_by_annotation(
        annotation: Annotated[str, Field(description="Annotation simple or qualified name, with or without '@'.")],
        entity_type: Annotated[Optional[str], Field(description="class, interface, method, api_endpoint, "
                                                                "variable...")] = None,
        limit: Annotated[int, Field(description="Maximum number of results.")] = 50,
        offset: Annotated[int, Field(description="Number of results to skip.")] = 0,
    ) -> dict:
        found = fq.find_by_annotation(kg, annotation, entity_type)
        page, info = server._page(found, limit, offset)
        text = f"{info['total']} entit(y/ies) annotated with @{annotation.lstrip('@')}:\n" + '\n'.join(
            f"- {f['id']} ({f['type']})" + (f" {_compact(f['args'], 120)}" if f['args'] else '')
            + (f" [{f['location']}]" if f.get('location') else '') for f in page)
        return {'annotation': annotation, 'results': page, 'count': len(page), **info, 'text': text}

    @tool("Persistence model: JPA entities (table, columns, id, associations with cardinality), Spring Data "
          "repositories (domain type, id type) and tables (from entities and SQL / Liquibase migrations, with the "
          "columns that differ between both). Focus on one entity with `entity`.")
    async def get_data_model(
        entity: Annotated[Optional[str], Field(description="Entity class to focus on (and its associations).")] = None,
    ) -> dict:
        model = fq.data_model(kg, entity)
        lines = []
        for item in model['entities']:
            lines.append(f"Entity {item['id']} -> table {item.get('table') or '-'} ({item['kind']})")
            for assoc in item.get('associations') or []:
                lines.append(f"  {assoc.get('field')}: {assoc.get('cardinality')} -> {assoc.get('target')}"
                             + (f" (mappedBy {assoc['mapped_by']})" if assoc.get('mapped_by') else ''))
        for repo in model['repositories']:
            lines.append(f"Repository {repo['id']} -> {repo['domain']} (id {repo.get('id_type')}, {repo.get('base')})")
        for table in model['tables']:
            line = f"Table {table.get('name', table['id'])} (sources: {', '.join(table.get('sources') or [])})"
            if table.get('drift'):
                line += f" drift: {_compact(table['drift'], 200)}"
            lines.append(line)
        return {**model, 'text': '\n'.join(lines) or 'No persistence model found.'}

    @tool("Spring configuration properties (application*.yml / .properties): values per profile (secrets masked), "
          "defining files and the code reading them (@Value, @ConfigurationProperties, @ConditionalOnProperty, "
          "Environment). Properties read but never defined are flagged defined=false.")
    async def get_config(
        key: Annotated[Optional[str], Field(description="Exact property key, e.g. 'spring.datasource.url'.")] = None,
        prefix: Annotated[Optional[str], Field(description="Key prefix, e.g. 'app.payment'.")] = None,
        profile: Annotated[Optional[str], Field(description="Only values of this profile ('default', 'prod'...).")] = None,
        undefined_only: Annotated[bool, Field(description="Only properties read by the code but never defined.")] = False,
        limit: Annotated[int, Field(description="Maximum number of properties.")] = 50,
        offset: Annotated[int, Field(description="Number of properties to skip.")] = 0,
    ) -> dict:
        items = fq.config_properties(kg, key, prefix, profile)
        if undefined_only:
            items = [i for i in items if not i['defined']]
        page, info = server._page(items, limit, offset)
        lines = [f"{info['total']} propert(y/ies), showing {len(page)}. Profiles: {', '.join(fq.profiles(kg)) or '-'}"]
        for item in page:
            values = ', '.join(f'{p}={v}' for p, v in item['values'].items()) or 'not defined'
            readers = ', '.join(r['entity'] for r in item['readers'][:3])
            lines.append(f"- {item['key']}: {values}" + (f"  <- {readers}" if readers else ''))
        return {'properties': page, 'count': len(page), **info, 'text': '\n'.join(lines)}

    @tool("Messaging: Kafka topics, RabbitMQ / JMS / SQS queues and application events, with their producers and "
          "consumers (listener methods, groups), plus @Scheduled jobs.")
    async def list_messaging(
        channel: Annotated[Optional[str], Field(description="Channel id or name, e.g. 'kafka:order-created'.")] = None,
    ) -> dict:
        channels = fq.messaging(kg, channel)
        jobs = [] if channel else fq.scheduled_jobs(kg)
        lines = []
        for item in channels:
            lines.append(f"{item['id']}")
            lines += [f"  produced by {p['entity']} ({p.get('via')})" for p in item['producers']]
            lines += [f"  consumed by {c['entity']}" + (f" (group {c['group']})" if c.get('group') else '')
                      for c in item['consumers']]
        for job in jobs:
            lines.append(f"Scheduled {job['id']}: {_compact(job['schedule'])}")
        return {'channels': channels, 'scheduled_jobs': jobs,
                'text': '\n'.join(lines) or 'No messaging channels or scheduled jobs found.'}

    @tool("Build modules (Maven / Gradle) and their library dependencies with versions and scopes; module-to-module "
          "dependencies of multi-module projects.")
    async def list_dependencies(
        module: Annotated[Optional[str], Field(description="Module id or artifactId, e.g. 'shop-api'.")] = None,
        name_query: Annotated[Optional[str], Field(description="Substring of the dependency, e.g. 'kafka'.")] = None,
        limit: Annotated[int, Field(description="Maximum number of dependencies.")] = 100,
        offset: Annotated[int, Field(description="Number of dependencies to skip.")] = 0,
    ) -> dict:
        items = fq.dependencies(kg, module, name_query)
        page, info = server._page(items, limit, offset)
        modules = [{'id': n.id, **{k: v for k, v in n.attributes.items() if k not in ('modules',)}}
                   for n in fq.resources(kg, 'module') if not module or module in (n.id, n.key)]
        text = '\n'.join(f"Module {m['id']}: Java {m.get('java_version', '?')}, Spring Boot "
                         f"{m.get('spring_boot_version', '-')}" for m in modules)
        text += '\n' + '\n'.join(f"- {d['module']} -> {d['dependency']}" + (f":{d['version']}" if d.get('version') else '')
                                 + (f" ({d['scope']})" if d.get('scope') else '') for d in page)
        return {'modules': modules, 'dependencies': page, 'count': len(page), **info, 'text': text.strip()
                or 'No build modules found.'}

    @tool("Dangerous API calls (sinks) from the security catalog — SQL, command, expression, path, SSRF, "
          "redirect, XXE, deserialization, LDAP, XPath, log / header injection, weak crypto, hard-coded keys, "
          "insecure randomness, trust-all TLS — with their CWE and the status of the dangerous argument: tainted "
          "(comes from a request input of the same method), depends_on_param / depends_on_field / depends_on_call "
          "(provenance decided elsewhere), unknown, weak_value, dangerous_api, mitigated, sanitized, "
          "safe_by_construction. Safe statuses are hidden unless include_safe=true or a status is requested. "
          "These are candidates to verify, not verdicts.")
    async def find_sinks(
        category: Annotated[Optional[str], Field(description="sql_injection, command_injection, path_traversal, "
                                                             "ssrf, xxe, deserialization, weak_crypto...")] = None,
        status: Annotated[Optional[str], Field(description="tainted, depends_on_param, safe_by_construction...")] = None,
        cwe: Annotated[Optional[int], Field(description="CWE number, e.g. 89.")] = None,
        include_safe: Annotated[bool, Field(description="Also list safe_by_construction / sanitized / mitigated.")] = False,
        path_prefix: Annotated[Optional[str], Field(description="Only files under this path (e.g. 'src/main').")] = None,
        limit: Annotated[int, Field(description="Maximum number of sinks.")] = 30,
        offset: Annotated[int, Field(description="Number of sinks to skip.")] = 0,
    ) -> dict:
        sinks = [fq._sink_brief(s) for s in fq.list_sinks(kg, category, status, cwe, include_safe, path_prefix)]
        page, info = server._page(sinks, limit, offset)
        lines = [f"{info['total']} sink(s), showing {len(page)}. Summary: "
                 + ', '.join(f'{k}={v}' for k, v in fq.security_summary(kg)['sinks_by_status'].items())]
        for s in page:
            detail = ', '.join(s.get('origins') or []) or (s.get('value') or '')
            lines.append(f"- [{s['status']}] {s['category']} CWE-{s['cwe']} {s['api']} at {s.get('location')} "
                         f"in {s['method']}" + (' (dynamic string)' if s.get('dynamic') else '')
                         + (f" <- {detail}" if detail else '')
                         + (f" reached from {', '.join(s['entries'])}" if s.get('entries') else
                            f" ({s['flow_count']} flow(s), see trace_taint)" if s.get('flow_count') else '')
                         + (f" mitigated by {', '.join(s['mitigated_by'])}" if s.get('mitigated_by') else ''))
        return {'sinks': page, 'count': len(page), **info, 'text': '\n'.join(lines)}

    @tool("Untrusted inputs: parameters bound from the request (@RequestParam, @PathVariable, @RequestBody, "
          "@RequestHeader, @CookieValue, unannotated handler parameters), GraphQL arguments, message payloads, and "
          "calls returning request data (getParameter, getHeader, getOriginalFilename...).")
    async def find_sources(
        kind: Annotated[Optional[str], Field(description="http_request, graphql_input, message, environment.")] = None,
        limit: Annotated[int, Field(description="Maximum number of sources.")] = 50,
        offset: Annotated[int, Field(description="Number of sources to skip.")] = 0,
    ) -> dict:
        sources = fq.list_sources(kg, kind)
        page, info = server._page(sources, limit, offset)
        lines = [f"{info['total']} untrusted input(s), showing {len(page)}:"]
        for s in page:
            what = f"param {s['param']}: {s.get('type')}" if s.get('param') else f"call {s.get('call')}"
            lines.append(f"- [{s['kind']}] {what} in {s['method']}"
                         + (' (implicit binding)' if s.get('implicit') else f" (@{s['annotation']})" if s.get('annotation') else '')
                         + (' @Valid' if s.get('validated') else ''))
        return {'sources': page, 'count': len(page), **info, 'text': '\n'.join(lines)}

    @tool("Source -> sink paths (taint flows): untrusted input (request parameter, getParameter(), message "
          "payload) followed through call sites and argument bindings down to a dangerous API call. Each flow "
          "gives the entry endpoint, every call crossed (file:line, parameter bound, interface dispatch) and a "
          "confidence (high: all resolved; medium: interface with several implementations or approximated value). "
          "Filter by endpoint ('GET /api/users'), method, class or channel ('kafka:orders'), category, CWE. "
          "Candidates to verify, not verdicts; sanitizers met on the way stop the flow for their categories.")
    async def trace_taint(
        target: Annotated[Optional[str], Field(description="Endpoint ('POST /api/orders'), method or class.")] = None,
        category: Annotated[Optional[str], Field(description="sql_injection, command_injection, path_traversal...")] = None,
        cwe: Annotated[Optional[int], Field(description="CWE number, e.g. 89.")] = None,
        min_confidence: Annotated[str, Field(description="high, medium or low (default: all).")] = 'low',
        limit: Annotated[int, Field(description="Maximum number of flows.")] = 20,
        offset: Annotated[int, Field(description="Number of flows to skip.")] = 0,
    ) -> dict:
        flows = fq.trace_taint(kg, target, category, cwe, min_confidence)
        page, info = server._page(flows, limit, offset)
        lines = [f"{info['total']} taint flow(s), showing {len(page)}."]
        for f in page:
            src = f['source']
            origin = f"param {src['param']}" if src.get('param') else f"{src.get('call')}"
            lines.append(f"- [{f['confidence']}] {f['category']} CWE-{f['cwe']}: {src.get('entry') or src.get('method')} "
                         f"({src.get('kind')}, {origin} in {src.get('method')})")
            for s in f['steps']:
                lines.append(f"    -> {s['to']}({s.get('param')}) called at {s['location']}"
                             + (' [interface dispatch]' if s.get('dispatch') else ''))
            lines.append(f"    => {f['sink']['api']} at {f['sink']['location']} in {f['sink']['method']}")
        return {'flows': page, 'count': len(page), **info, 'text': '\n'.join(lines)}

    @tool("Transitive callers of a method (through calls edges and interface methods) and the entry points "
          "reaching it (HTTP / GraphQL endpoints, message listeners), with call sites. Use it to know whether a "
          "sink can be reached from outside and to prioritise it.")
    async def find_callers(
        entity: Annotated[str, Field(description="Method, e.g. 'UserRepository::findByName'.")],
        max_depth: Annotated[int, Field(description="Maximum call depth.")] = 4,
        limit: Annotated[int, Field(description="Maximum number of callers.")] = 100,
    ) -> dict:
        result = fq.find_callers(kg, entity, max_depth, limit)
        lines = [f"Callers of {result['entity']} ({len(result['callers'])}):"]
        lines += [f"{'  ' * (c['depth'] - 1)}- {c['caller']} -> {c['callee']}"
                  + (f" at {', '.join(c['sites'])}" if c['sites'] else '') for c in result['callers']]
        lines.append('Entry points: ' + (', '.join(f"{e['entry']} (depth {e['depth']})" for e in result['entry_points'])
                                         or 'none found'))
        return {**result, 'text': '\n'.join(lines)}

    @tool("Security posture of the application: Spring Security filter chains (conditions evaluated against the "
          "configuration, rules in order, CSRF, CORS, headers, session policy, authentication mechanisms, custom "
          "filters), method security (enabled or not), WebSecurityCustomizer.ignoring() paths, CORS configurations, "
          "risky properties (actuator exposure, H2 console, stack traces, cookie flags, literal secrets) and every "
          "candidate finding with CWE, severity, confidence, location and a hint to verify it. Not verdicts.")
    async def get_security_posture(
        severity: Annotated[Optional[str], Field(description="Minimum severity of the findings: high, medium or low.")] = None,
        limit: Annotated[int, Field(description="Maximum number of findings.")] = 40,
        offset: Annotated[int, Field(description="Number of findings to skip.")] = 0,
    ) -> dict:
        posture = fq.security_posture(kg, severity)
        page, info = server._page(posture['findings'], limit, offset)
        lines = [f"Spring Security: {'yes' if posture['spring_security'] else 'not detected'}. Endpoint access: "
                 + (', '.join(f'{k}={v}' for k, v in posture['access_summary'].items()) or 'no endpoint')]
        for chain in posture['filter_chains']:
            lines.append(f"- chain {chain['name']} [{chain['condition']['status']}"
                         + (f": {'; '.join(chain['condition']['reasons'])}" if chain['condition']['reasons'] else '')
                         + f"] csrf={'on' if chain['csrf']['enabled'] else 'OFF'} session={chain['session_policy']} "
                         f"auth={','.join(chain['authentication'] + chain['custom_filters']) or '-'} cors={chain['cors']} "
                         f"frame={chain['headers']['frame_options']}")
            for rule in chain['rules']:
                lines.append(f"    line {rule['line']}: {rule['http_method'] or '*'} {', '.join(rule['patterns'])} -> "
                             f"{rule['rule']}" + (f"({', '.join(rule['roles'])})" if rule['roles'] else ''))
        for item in posture['method_security']:
            lines.append(f"- @{item['annotation']} on {item['class']} [{item['condition']['status']}] "
                         f"pre_post={item['pre_post']} secured={item['secured']} jsr250={item['jsr250']}")
        if posture['ignored_paths']:
            lines.append('- ignored by WebSecurityCustomizer: ' + ', '.join(i['pattern'] for i in posture['ignored_paths']))
        lines.append(f"Findings ({info['total']}, showing {len(page)}):")
        for f in page:
            where = f.get('endpoint') or f.get('property') or f.get('method') or f.get('chain') or ''
            lines.append(f"- [{f['severity']}/{f['confidence']}] CWE-{f['cwe']} {f['id']}: {f['title']}"
                         + (f" — {where}" if where else '') + (f" ({f['file']}:{f['line']})" if f.get('file') and f.get('line') else ''))
        result = {k: v for k, v in posture.items() if k != 'findings'}
        return {**result, 'findings': page, 'count': len(page), **info, 'text': '\n'.join(lines)}

    @tool("Attack surface: every entry point (HTTP and GraphQL endpoints, message listeners) with its effective "
          "authorization (public, anonymous, authenticated, role, authority, expression, denied, unknown; the "
          "filter-chain rule or annotation that decides it, and whether method security makes the annotation "
          "effective), untrusted inputs, taint flows starting there and candidate findings. Least protected first.")
    async def list_attack_surface(
        access: Annotated[Optional[str], Field(description="public, authenticated, role, broker...")] = None,
        http_method: Annotated[Optional[str], Field(description="GET, POST... or GRAPHQL.")] = None,
        path_prefix: Annotated[Optional[str], Field(description="Only paths starting with this prefix.")] = None,
        with_findings: Annotated[bool, Field(description="Only entries with findings or taint flows.")] = False,
        limit: Annotated[int, Field(description="Maximum number of entries.")] = 50,
        offset: Annotated[int, Field(description="Number of entries to skip.")] = 0,
    ) -> dict:
        rows = fq.attack_surface(kg, access, http_method, path_prefix, with_findings)
        page, info = server._page(rows, limit, offset)
        lines = [f"{info['total']} entry point(s), showing {len(page)}:"]
        for r in page:
            decided = r['decided_by']
            why = f"{decided.get('source')}:{decided.get('rule', '')}" + (f" line {decided['line']}" if decided.get('line') else '')
            if r.get('annotation'):
                why += f", @{r['annotation']['name']}" + ('' if r['annotation']['effective'] else ' (NOT enforced)')
            lines.append(f"- [{r['access']}{':' + ','.join(r['roles']) if r['roles'] else ''}] {r['entry']} ({why})"
                         + (f" inputs: {', '.join(str(i) for i in r['inputs'])}" if r['inputs'] else '')
                         + (f" flows: {', '.join(r['taint_flows'])}" if r['taint_flows'] else '')
                         + (f" findings: {', '.join(r['findings'])}" if r['findings'] else ''))
        return {'entries': page, 'count': len(page), **info, 'text': '\n'.join(lines)}

    @tool("Candidate security findings in one list: sinks reached by untrusted data (with their taint flow), weak "
          "values and dangerous APIs, secrets written in the code, security posture (access control, CSRF, CORS, IDOR, "
          "mass assignment, risky configuration) and known vulnerabilities of dependencies (after check_dependencies). "
          "Each finding has a stable id, CWE, severity, confidence, file:line, evidence and a hint to verify it. "
          "Candidates to verify, never verdicts.")
    async def list_findings(
        severity: Annotated[Optional[str], Field(description="Minimum severity: high, medium or low.")] = None,
        cwe: Annotated[Optional[int], Field(description="CWE number, e.g. 89.")] = None,
        kind: Annotated[Optional[str], Field(description="taint, sink, secret, posture or dependency.")] = None,
        min_confidence: Annotated[Optional[str], Field(description="high, medium or low.")] = None,
        path_prefix: Annotated[Optional[str], Field(description="Only files under this path.")] = None,
        include_unconfirmed: Annotated[bool, Field(description="Also sinks whose provenance is undecided "
                                                               "(depends_on_param / field / call, unknown).")] = False,
        limit: Annotated[int, Field(description="Maximum number of findings.")] = 30,
        offset: Annotated[int, Field(description="Number of findings to skip.")] = 0,
    ) -> dict:
        from . import findings as fd
        items = fd.filter_findings(fd.collect_findings(kg, include_unconfirmed), severity, cwe, kind, min_confidence,
                                   path_prefix)
        page, info = server._page(items, limit, offset)
        page = [{k: v for k, v in f.items() if k != 'flows'} for f in page]  # full paths: trace_taint
        lines = [f"{info['total']} finding(s), showing {len(page)}."]
        for f in page:
            lines.append(f"- {f['id']} [{f['severity']}/{f['confidence']}] CWE-{f.get('cwe')} {f['title']}"
                         + (f" at {f['file']}:{f['line']}" if f.get('file') and f.get('line') else f" in {f['file']}" if f.get('file') else '')
                         + (f" (entry {f['entry']})" if f.get('entry') else ''))
            lines.append(f"    verify: {f['hint']}")
        return {'findings': page, 'count': len(page), **info, 'text': '\n'.join(lines)}

    @tool("Export the candidate findings as a SARIF 2.1.0 log (GitHub code scanning, IDEs): one rule per finding "
          "type with CWE tags, taint flows as code flows, stable fingerprints.")
    async def export_sarif(
        severity: Annotated[Optional[str], Field(description="Minimum severity: high, medium or low.")] = None,
        min_confidence: Annotated[Optional[str], Field(description="high, medium or low.")] = None,
    ) -> dict:
        from . import findings as fd
        items = fd.filter_findings(fd.collect_findings(kg), severity, min_confidence=min_confidence)
        sarif = fd.to_sarif(items)
        return {'sarif': sarif, 'results': len(sarif['runs'][0]['results']),
                'text': f"SARIF 2.1.0 log with {len(sarif['runs'][0]['results'])} result(s) and "
                        f"{len(sarif['runs'][0]['tool']['driver']['rules'])} rule(s)."}

    @tool("Exact lines of a file (file:start-end, with optional context lines), numbered; secrets are masked and "
          "the content is flagged as untrusted. At most 200 lines.")
    async def get_code(
        file_path: Annotated[str, Field(description="File path (or a unique suffix of it).")],
        start_line: Annotated[int, Field(description="First line (1-based).")] = 1,
        end_line: Annotated[Optional[int], Field(description="Last line (default: start_line).")] = None,
        context: Annotated[int, Field(description="Lines added before and after.")] = 0,
    ) -> dict:
        result = fq.get_code(kg, file_path, start_line, end_line, context)
        return {**result, 'text': f"{result['file']}:{result['start_line']}-{result['end_line']}\n{result['code']}"}

    @tool("Regular-expression search in the content of the repository files (one match per line, secrets masked), "
          "with an optional path prefix. Useful to verify a candidate or find what the graph does not model.")
    async def grep_code(
        pattern: Annotated[str, Field(description="Python regular expression, e.g. 'createNativeQuery\\('.")],
        path_prefix: Annotated[Optional[str], Field(description="Only files under this path.")] = None,
        case_insensitive: Annotated[bool, Field(description="Ignore case.")] = False,
        max_results: Annotated[int, Field(description="Maximum number of matching lines.")] = 50,
    ) -> dict:
        result = fq.grep_code(kg, pattern, path_prefix, case_insensitive, max_results)
        lines = [f"{len(result['matches'])} match(es) in {result['files_with_matches']} file(s)"
                 + (' (truncated)' if result['truncated'] else '') + ':']
        lines += [f"- {m['file']}:{m['line']}: {m['code']}" for m in result['matches']]
        return {**result, 'text': '\n'.join(lines)}

    @tool("Where a library is used: files importing it (with the imports), number of methods they declare, and the "
          "entry points (endpoints, listeners) that reach those methods through the call graph. Distinguishes a "
          "library alert from a reachable risk. Offline.")
    async def get_dependency_usage(
        dependency: Annotated[str, Field(description="'group:artifact', 'dep:group:artifact' or a unique artifact id.")],
        include_tests: Annotated[bool, Field(description="Also count test files.")] = False,
    ) -> dict:
        result = fq.dependency_usage(kg, dependency, include_tests)
        lines = [f"{result['dependency']} {result.get('resolved_version') or '(version ' + str(result.get('version_source')) + ')'}"
                 f": imported by {len(result['files'])} file(s), {result['methods']} method(s)"]
        lines += [f"- {f['file']}: {', '.join(f['imports'][:4])}" for f in result['files'][:20]]
        lines.append('Entry points: ' + (', '.join(f"{e['entry']} (depth {e['depth']})" for e in result['entry_points'][:15])
                                         or 'none found'))
        return {**result, 'text': '\n'.join(lines)}

    @tool("Known vulnerabilities of the dependencies (OSV database), with the entry points that reach the code "
          "importing each vulnerable library. Network lookup is OPT-IN: it only happens when the server runs with "
          "OSV_LOOKUP=1 and sends nothing but dependency coordinates (group:artifact and version); otherwise cached "
          "answers are used. Dependencies whose version is managed by a BOM and not resolved are listed apart.")
    async def check_dependencies(
        include_tests: Annotated[bool, Field(description="Also check test-only dependencies.")] = False,
        refresh: Annotated[bool, Field(description="Ignore the cache (needs OSV_LOOKUP=1).")] = False,
    ) -> dict:
        result = fq.check_dependencies(kg, server.osv_client, include_tests, refresh)
        lines = [f"Checked {result['checked']} dependency version(s) (sources: "
                 + (', '.join(f'{k}={v}' for k, v in result['sources'].items()) or 'none') + ')'
                 + ('' if result['enabled'] else ' — OSV lookup disabled: set OSV_LOOKUP=1 to query osv.dev')]
        for item in result['vulnerable']:
            ids = ', '.join(f"{v['id']}[{v['severity']}]" for v in item['vulnerabilities'][:6])
            reach = ', '.join(e['entry'] for e in item['entry_points'][:4]) or f"imported by {item['files']} file(s)"
            lines.append(f"- {item['dependency']} {item['version']}: {ids} — reached from {reach}")
        if result['not_checked']:
            lines.append(f"Not checked ({len(result['not_checked'])}, version not resolved): "
                         + ', '.join(d['dependency'] for d in result['not_checked'][:10]))
        return {**result, 'text': '\n'.join(lines)}

    @tool("Test classes covering a class (or the class of a method): test kind (unit, web_slice, data_slice, "
          "integration) and how the link was found (annotation, subject, naming).")
    async def find_tests_for(
        entity: Annotated[str, Field(description="Class or method name, e.g. 'OrderService'.")],
    ) -> dict:
        result = fq.tests_for(kg, entity)
        text = f"Tests for {result['entity']}:\n" + ('\n'.join(
            f"- {t['test']} ({t.get('kind')}, via {t.get('via')})" + (f" [{t['location']}]" if t.get('location') else '')
            for t in result['tests']) or '- none found')
        return {**result, 'text': text}
