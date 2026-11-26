"""
Spring beans and dependency injection.

Beans: classes annotated with a stereotype (@Component, @Service, @Repository, @Controller,
@RestController, @Configuration, @ControllerAdvice, @SpringBootApplication...), with a repository-defined
annotation meta-annotated with one of them, Spring Data repository interfaces, Feign clients, and
@Bean methods. Their entity gets `attributes['spring']`: stereotype, bean_name, scope, profiles,
conditions, primary, qualifier, lazy.

Injection points: the constructor (single or @Autowired; Lombok @RequiredArgsConstructor /
@AllArgsConstructor), @Autowired / @Inject / @Resource fields and setters, and the parameters of
@Bean methods. Each point is resolved to the bean(s) providing its type:
    exact type -> 'exact'; unique implementation of an interface -> 'inferred';
    @Qualifier / bean name -> 'qualifier'; @Primary -> 'primary'; List<T> / Set<T>... -> 'collection';
    otherwise one edge per candidate with confidence 'ambiguous'.
Edges: consumer -injects-> bean (via, field / parameter, qualifier, confidence).
"""
import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from .base import EnrichmentContext, annotation, annotation_names, annotation_value, as_list, enum_name

logger = logging.getLogger(__name__)

STEREOTYPES = {
    'Component': 'component',
    'Service': 'service',
    'Repository': 'repository',
    'Controller': 'controller',
    'RestController': 'rest_controller',
    'Configuration': 'configuration',
    'AutoConfiguration': 'configuration',
    'ControllerAdvice': 'controller_advice',
    'RestControllerAdvice': 'controller_advice',
    'SpringBootApplication': 'application',
    'SpringBootConfiguration': 'configuration',
    'FeignClient': 'feign_client',
    'HttpExchange': 'http_interface',
    'Named': 'component',
    'ManagedBean': 'component',
    'Endpoint': 'actuator_endpoint',
    'ConfigurationProperties': 'configuration_properties',
    'DgsComponent': 'graphql_component',
    'RestControllerEndpoint': 'actuator_endpoint',
}

SPRING_DATA_REPOSITORIES = {
    'Repository', 'CrudRepository', 'ListCrudRepository', 'PagingAndSortingRepository',
    'ListPagingAndSortingRepository', 'JpaRepository', 'JpaSpecificationExecutor', 'MongoRepository',
    'ReactiveCrudRepository', 'ReactiveMongoRepository', 'R2dbcRepository', 'ElasticsearchRepository',
    'Neo4jRepository', 'KeyValueRepository', 'CassandraRepository', 'RevisionRepository',
    'QuerydslPredicateExecutor', 'JdbcRepository', 'CoroutineCrudRepository',
}

INJECT_ANNOTATIONS = ('Autowired', 'Inject', 'Resource')
WRAPPERS = {'List', 'Set', 'Collection', 'Iterable', 'Optional', 'ObjectProvider', 'Provider', 'Stream',
            'ObjectFactory', 'Lazy', 'Supplier', 'Map', 'SortedSet', 'Flux', 'Mono'}
MULTIPLE = {'List', 'Set', 'Collection', 'Iterable', 'Map', 'SortedSet', 'Stream'}


def applies(context: EnrichmentContext) -> bool:
    for _path, entity in context.java_declarations():
        names = set(annotation_names(entity.get('attributes')))
        if names & set(STEREOTYPES) or 'Bean' in names or names & set(INJECT_ANNOTATIONS):
            return True
    return any(imp.startswith('org.springframework')
               for p in context.analyses if p.endswith('.java')
               for imp in context.analyses[p].file_node.imports or [])


def bean_name_for(simple: str) -> str:
    """Default bean name of a class (Introspector.decapitalize rule)."""
    if len(simple) > 1 and simple[0].isupper() and simple[1].isupper():
        return simple
    return simple[:1].lower() + simple[1:]


def _stereotype_annotations(context: EnrichmentContext) -> Dict[str, str]:
    """Stereotype annotations, including repository-defined ones meta-annotated with a stereotype."""
    stereotypes = dict(STEREOTYPES)
    annotation_types = {name.rsplit('.', 1)[-1]: decl for name, decl in context.java_types.items()
                        if decl.get('type') == 'annotation'}
    changed = True
    while changed:
        changed = False
        for simple, decl in annotation_types.items():
            if simple in stereotypes:
                continue
            for meta in annotation_names(decl.get('attributes')):
                if meta in stereotypes:
                    stereotypes[simple] = stereotypes[meta]
                    changed = True
                    break
    return stereotypes


def _is_data_repository(context: EnrichmentContext, type_name: str, seen: Optional[Set[str]] = None) -> bool:
    decl = context.java_types.get(type_name)
    if not decl:
        return False
    seen = seen or set()
    seen.add(type_name)
    for parent in decl.get('bases') or []:
        simple = parent.rsplit('.', 1)[-1]
        if simple in SPRING_DATA_REPOSITORIES and (parent.startswith('org.springframework.data') or '.' not in parent
                                                   or not context.declared(context.resolve(parent))):
            return True
        resolved = context.resolve(parent, decl['_path'])
        if resolved and resolved not in seen and _is_data_repository(context, resolved, seen):
            return True
    return False


def _subtypes(context: EnrichmentContext) -> Dict[str, Set[str]]:
    """Type -> every repository type that extends / implements it (transitively), itself included."""
    parents: Dict[str, Set[str]] = {}
    for u, v, data in context.graph.edges(data=True):
        if data.get('relation') == 'inherits':
            parents.setdefault(u, set()).add(v)
    result: Dict[str, Set[str]] = {}
    for child in list(parents) + list(context.java_types):
        stack, seen = [child], set()
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            result.setdefault(current, set()).add(child)
            stack.extend(parents.get(current, ()))
    return result


def unwrap_type(type_text: str) -> Tuple[str, bool]:
    """('OrderService', multiple?) from 'List<OrderService>', 'Optional<X>', 'Map<String, X>'..."""
    text = re.sub(r'@\w+(\([^)]*\))?\s*', '', type_text or '').strip()
    multiple = False
    for _ in range(3):
        match = re.match(r'^([\w.]+)\s*<(.*)>$', text)
        if not match or match.group(1).rsplit('.', 1)[-1] not in WRAPPERS:
            break
        wrapper = match.group(1).rsplit('.', 1)[-1]
        multiple = multiple or wrapper in MULTIPLE
        args = [a.strip() for a in _split_generic_args(match.group(2))]
        text = args[-1] if wrapper == 'Map' else args[0]
    return re.sub(r'<.*', '', text).replace('[]', '').strip(), multiple


def _split_generic_args(text: str) -> List[str]:
    parts, depth, current = [], 0, ''
    for char in text:
        if char == '<':
            depth += 1
        elif char == '>':
            depth -= 1
        if char == ',' and depth == 0:
            parts.append(current)
            current = ''
        else:
            current += char
    parts.append(current)
    return parts


def _qualifier(attributes: Dict[str, Any]) -> Optional[str]:
    for name in ('Qualifier', 'Named'):
        item = annotation(attributes, name)
        if item:
            value = annotation_value(item, 'value')
            if isinstance(value, str):
                return value
    resource = annotation(attributes, 'Resource')
    if resource:
        value = annotation_value(resource, 'name')
        if isinstance(value, str):
            return value
    return None


def enrich(context: EnrichmentContext) -> None:
    stereotypes = _stereotype_annotations(context)
    beans: Dict[str, Dict[str, Any]] = {}  # bean entity (class or @Bean method) -> info

    def register(entity_name: str, info: Dict[str, Any]) -> None:
        beans[entity_name] = info
        spring = context.framework_attributes(entity_name, 'spring')
        spring.update({k: v for k, v in info.items() if v not in (None, '', [], False) and k != 'provides'})

    # ---------------------------------------------------------------- bean classes
    for type_name, decl in context.java_types.items():
        attributes = decl.get('attributes') or {}
        names = annotation_names(attributes)
        stereotype = next((stereotypes[n] for n in names if n in stereotypes), None)
        mapper = annotation(attributes, 'Mapper')
        if mapper and not stereotype:  # MyBatis or MapStruct @Mapper: interfaces proxied / generated as beans
            qualified = str(mapper.get('qualified') or '')
            stereotype = 'mapstruct_mapper' if qualified.startswith('org.mapstruct') else 'mybatis_mapper'
        data_repository = decl.get('type') == 'interface' and _is_data_repository(context, type_name)
        if data_repository and not stereotype:
            stereotype = 'repository'
        if not stereotype or decl.get('type') == 'annotation':
            continue
        # Spring only instantiates interfaces it proxies (Spring Data repositories, Feign / HTTP interface
        # clients); a plain domain interface annotated @Repository is implemented by another bean.
        if decl.get('type') == 'interface' and not data_repository and stereotype not in (
                'feign_client', 'http_interface', 'mybatis_mapper', 'mapstruct_mapper'):
            continue
        simple = type_name.rsplit('.', 1)[-1]
        explicit_name = None
        for n in names:
            if n in stereotypes:
                value = annotation_value(annotation(attributes, n), 'value', 'name')
                if isinstance(value, str) and value:
                    explicit_name = value
        scope = annotation_value(annotation(attributes, 'Scope'), 'value', 'scopeName')
        if annotation(attributes, 'RequestScope'):
            scope = 'request'
        elif annotation(attributes, 'SessionScope'):
            scope = 'session'
        register(type_name, {
            'stereotype': stereotype,
            'bean_name': explicit_name or bean_name_for(simple),
            'bean_type': type_name,
            'scope': enum_name(scope).lower().replace('scope_', '') if scope else 'singleton',
            'profiles': [p for p in as_list(annotation_value(annotation(attributes, 'Profile'), 'value')) if p],
            'conditions': [n for n in names if n.startswith('Conditional')],
            'primary': bool(annotation(attributes, 'Primary')),
            'qualifier': _qualifier(attributes),
            'lazy': bool(annotation(attributes, 'Lazy')),
            'data_repository': data_repository,
            'provides': type_name,
        })

    # ---------------------------------------------------------------- @Bean methods
    for path, entity in context.java_declarations():
        attributes = entity.get('attributes') or {}
        bean_annotation = annotation(attributes, 'Bean')
        if not bean_annotation or entity.get('type') not in ('method', 'api_endpoint'):
            continue
        method = context.registry.lookup(entity['name'])
        if not method:
            continue
        names = [n for n in as_list(annotation_value(bean_annotation, 'name', 'value')) if isinstance(n, str)]
        method_name = entity['name'].rsplit('::', 1)[-1]
        provided = context.qualify(attributes.get('return_type_resolved'), path)
        register(method, {
            'stereotype': 'bean_method',
            'bean_name': names[0] if names else method_name,
            'aliases': names[1:],
            'bean_type': provided or attributes.get('return_type_resolved') or attributes.get('return_type'),
            'scope': enum_name(annotation_value(annotation(attributes, 'Scope'), 'value') or 'singleton').lower(),
            'profiles': [p for p in as_list(annotation_value(annotation(attributes, 'Profile'), 'value')) if p],
            'conditions': [n for n in annotation_names(attributes) if n.startswith('Conditional')],
            'primary': bool(annotation(attributes, 'Primary')),
            'qualifier': _qualifier(attributes),
            'provides': provided,
        })
        if provided and context.declared(provided):
            context.add_edge(method, provided, 'provides')

    subtypes = _subtypes(context)
    providers: Dict[str, List[str]] = {}
    for bean, info in beans.items():
        provided = info.get('provides')
        if not provided:
            continue
        for parent, children in subtypes.items():
            if provided in children:
                providers.setdefault(parent, []).append(bean)
        providers.setdefault(provided, [])
        if bean not in providers[provided]:
            providers[provided].append(bean)

    def candidates_for(type_text: str, path: str) -> Tuple[Optional[str], List[str], bool]:
        raw, multiple = unwrap_type(type_text)
        resolved = context.qualify(raw, path) if raw else None
        if not resolved:
            return None, [], multiple
        return resolved, list(dict.fromkeys(providers.get(resolved, []))), multiple

    def choose(resolved: str, candidates: List[str], multiple: bool, qualifier: Optional[str],
               point_name: str) -> List[Tuple[str, str]]:
        if not candidates:
            return []
        if qualifier:
            chosen = [c for c in candidates if qualifier in (beans[c].get('bean_name'), beans[c].get('qualifier'))
                      or qualifier in (beans[c].get('aliases') or [])]
            if chosen:
                return [(c, 'qualifier') for c in chosen]
        if multiple:
            return [(c, 'collection') for c in candidates]
        if len(candidates) == 1:
            only = candidates[0]
            return [(only, 'exact' if beans[only].get('provides') == resolved else 'inferred')]
        primary = [c for c in candidates if beans[c].get('primary')]
        if len(primary) == 1:
            return [(primary[0], 'primary')]
        by_name = [c for c in candidates if beans[c].get('bean_name') == point_name]
        if len(by_name) == 1:
            return [(by_name[0], 'by_name')]
        return [(c, 'ambiguous') for c in candidates]

    def inject(consumer: str, path: str, type_text: str, point_name: str, via: str,
               point_attributes: Dict[str, Any]) -> None:
        if annotation(point_attributes, 'Value'):
            return  # configuration value, not a bean
        resolved, candidates, multiple = candidates_for(type_text, path)
        qualifier = _qualifier(point_attributes)
        targets = choose(resolved, candidates, multiple, qualifier, point_name) if resolved else []
        record: Dict[str, Any] = {'name': point_name, 'type': resolved or type_text, 'via': via,
                                  'targets': [t for t, _ in targets]}
        if qualifier:
            record['qualifier'] = qualifier
        if not targets:
            record['unresolved'] = True
        context.framework_attributes(consumer, 'spring').setdefault('injections', []).append(record)
        for target, confidence in targets:
            if target != consumer:
                context.add_edge(consumer, target, 'injects', via=via, point=point_name,
                                 qualifier=qualifier, confidence=confidence)

    for bean, info in list(beans.items()):
        if info['stereotype'] == 'bean_method':
            method_decl = next((e for _, e in context.java_declarations()
                                if context.registry.lookup(e['name']) == bean), None)
            if method_decl:
                path = (context.registry.entities[bean].get('file_path') or '')
                for param in (method_decl.get('attributes') or {}).get('params') or []:
                    inject(bean, path, param.get('type', ''), param.get('name', ''), 'bean_method',
                           {'annotations': param.get('annotations')})
            continue
        class_decl = context.java_types.get(bean)
        if not class_decl or class_decl.get('type') == 'interface':
            continue
        path = class_decl['_path']
        class_attributes = class_decl.get('attributes') or {}
        members = context.members_of(bean)
        constructors = [m for m in members if m.get('type') == 'constructor']
        explicit = [c for c in constructors if set(annotation_names(c.get('attributes'))) & set(INJECT_ANNOTATIONS)]
        chosen = explicit or (constructors if len(constructors) == 1 else [])
        for constructor in chosen:
            for param in (constructor.get('attributes') or {}).get('params') or []:
                inject(bean, path, param.get('type', ''), param.get('name', ''), 'constructor',
                       {'annotations': param.get('annotations')})
        lombok = set(annotation_names(class_attributes)) & {'RequiredArgsConstructor', 'AllArgsConstructor'}
        fields = [m for m in members if m.get('type') == 'variable']
        for field in fields:
            field_attributes = field.get('attributes') or {}
            modifiers = field_attributes.get('modifiers') or []
            name = field['name'].rsplit('::', 1)[-1]
            if set(annotation_names(field_attributes)) & set(INJECT_ANNOTATIONS):
                inject(bean, path, field.get('dtype', ''), name, 'field', field_attributes)
            elif not constructors and lombok and 'static' not in modifiers and (
                    'AllArgsConstructor' in lombok or 'final' in modifiers or annotation(field_attributes, 'NonNull')):
                if 'calls' not in field_attributes and field_attributes.get('constant_value') is None:
                    inject(bean, path, field.get('dtype', ''), name, 'constructor', field_attributes)
        for member in members:
            method_attributes = member.get('attributes') or {}
            if member.get('type') == 'method' and set(annotation_names(method_attributes)) & set(INJECT_ANNOTATIONS):
                for param in method_attributes.get('params') or []:
                    inject(bean, path, param.get('type', ''), param.get('name', ''), 'setter',
                           {'annotations': param.get('annotations')})

    context.shared['beans'] = {name: {k: v for k, v in info.items() if k != 'provides'} for name, info in beans.items()}
