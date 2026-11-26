"""
Read-only queries over the framework knowledge added by the enrichers (endpoints, beans, persistence,
configuration, messaging, build, tests). Used by RepoKnowledgeGraph and the MCP server.

Every function takes a RepoKnowledgeGraph and returns plain data (dicts / lists) that tools format.
"""
import logging
import re
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .node import EntityNode, FileNode, ResourceNode

logger = logging.getLogger(__name__)

HTTP_METHODS = ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS', 'ANY', 'GRAPHQL')


class LookupError_(ValueError):
    """Raised when a name cannot be resolved to a node (the message lists suggestions)."""


# ---------------------------------------------------------------------- generic helpers

def _node(kg, node_id: str):
    return kg.graph.nodes[node_id]['data'] if node_id in kg.graph else None


def location(node) -> Optional[str]:
    path = getattr(node, 'file_path', '') or ''
    line = getattr(node, 'start_line', 0) or 0
    if not path:
        return None
    return f'{path}:{line}' if line else path


def _edges(kg, node_id: str, relation: str, direction: str = 'out') -> List[Tuple[str, dict]]:
    graph = kg.graph
    if node_id not in graph:
        return []
    if direction == 'out':
        return [(t, d) for _, t, d in graph.out_edges(node_id, data=True) if d.get('relation') == relation]
    return [(s, d) for s, _, d in graph.in_edges(node_id, data=True) if d.get('relation') == relation]


def resources(kg, node_type: str) -> List[ResourceNode]:
    return [d['data'] for _, d in kg.graph.nodes(data=True)
            if isinstance(d.get('data'), ResourceNode) and d['data'].node_type == node_type]


def entities_with(kg, namespace: str, key: Optional[str] = None) -> List[EntityNode]:
    """Entity nodes having attributes[namespace] (and attributes[namespace][key] when given)."""
    found = []
    for _, data in kg.graph.nodes(data=True):
        node = data.get('data')
        if isinstance(node, EntityNode):
            value = (node.attributes or {}).get(namespace)
            if isinstance(value, dict) and (key is None or value.get(key) not in (None, '', [], {}, False)):
                found.append(node)
    return found


def resolve_entity(kg, name: str) -> str:
    if name in kg.graph and isinstance(_node(kg, name), EntityNode):
        return name
    resolved = kg.resolve_entity(name)
    if resolved and resolved in kg.graph:
        return resolved
    # Last resort: a unique declared entity whose name ends with the given (partial) qualified name
    candidates = [n for n in kg.entities if kg.entities[n].get('declaring_chunk_ids')
                  and (n.endswith('.' + name) or n.endswith('::' + name) or n.endswith('.' + name.replace('.', '::')))]
    if len(candidates) == 1:
        return candidates[0]
    hint = f" Candidates: {', '.join(sorted(candidates)[:8])}" if candidates else ''
    raise LookupError_(f"Entity '{name}' not found in the knowledge graph.{hint}")


def entity_summary(kg, name: str) -> Dict[str, Any]:
    node = _node(kg, name)
    result: Dict[str, Any] = {'id': name}
    if isinstance(node, EntityNode):
        result['type'] = node.entity_type
        loc = location(node)
        if loc:
            result['location'] = loc
    return result


# ---------------------------------------------------------------------- endpoints

def list_endpoints(kg, http_method: Optional[str] = None, path_prefix: Optional[str] = None,
                   include_external: bool = False) -> List[ResourceNode]:
    method = http_method.upper() if http_method else None
    result = []
    for node in resources(kg, 'endpoint'):
        attributes = node.attributes
        if attributes.get('external') and not include_external:
            continue
        if method and attributes.get('http_method') not in (method, 'ANY'):
            continue
        if path_prefix and not str(attributes.get('path', '')).startswith(path_prefix):
            continue
        result.append(node)
    return sorted(result, key=lambda n: (str(n.attributes.get('path')), str(n.attributes.get('http_method'))))


def _path_matches(template: str, concrete: str) -> bool:
    parts = []
    for segment in template.rstrip('/').split('/'):
        parts.append('[^/]+' if re.fullmatch(r'\{[^}]*\}', segment) else re.escape(segment))
    return re.fullmatch('/'.join(parts) + '/?', concrete) is not None


def find_endpoint(kg, spec: str) -> str:
    """Endpoint id from 'endpoint:GET /x', 'GET /x', 'GET /api/orders/42' or a unique path '/x'."""
    spec = (spec or '').strip()
    if spec in kg.graph and isinstance(_node(kg, spec), ResourceNode):
        return spec
    if not spec.startswith('endpoint:') and f'endpoint:{spec}' in kg.graph:
        return f'endpoint:{spec}'
    text = spec[len('endpoint:'):] if spec.startswith('endpoint:') else spec
    parts = text.split(None, 1)
    method, path = (parts[0].upper(), parts[1]) if len(parts) == 2 and parts[0].upper() in HTTP_METHODS else (None, text)
    endpoints = list_endpoints(kg, include_external=True)
    exact = [n for n in endpoints if n.attributes.get('path') == path
             and (method is None or n.attributes.get('http_method') in (method, 'ANY'))]
    candidates = exact or [n for n in endpoints if _path_matches(str(n.attributes.get('path', '')), path)
                           and (method is None or n.attributes.get('http_method') in (method, 'ANY'))]
    if len(candidates) == 1:
        return candidates[0].id
    served = [n for n in candidates if not n.attributes.get('external')]
    if len(served) == 1:
        return served[0].id
    hint = ', '.join(sorted(n.id for n in candidates)[:8]) if candidates else ''
    raise LookupError_(f"Endpoint '{spec}' {'is ambiguous' if candidates else 'not found'}."
                       + (f' Candidates: {hint}' if hint else ' Use list_endpoints to see the endpoints.'))


def endpoint_details(kg, endpoint_id: str) -> Dict[str, Any]:
    node = _node(kg, endpoint_id)
    details: Dict[str, Any] = {'id': endpoint_id, **node.attributes}
    details['location'] = location(node)
    details['handlers'] = [entity_summary(kg, t) for t, _ in _edges(kg, endpoint_id, 'handled_by')]
    details['callers'] = [{**entity_summary(kg, s), 'via': d.get('via')}
                          for s, d in _edges(kg, endpoint_id, 'calls_endpoint', 'in')]
    throws = set(details.get('throws') or [])
    handlers = []
    for entity in entities_with(kg, 'spring', 'exception_handler'):
        info = entity.attributes['spring']['exception_handler']
        exceptions = info.get('exceptions') or []
        if info.get('global') or any(e in throws for e in exceptions):
            handlers.append({'handler': entity.id, 'exceptions': exceptions, 'status': info.get('status'),
                             'location': location(entity)})
    details['exception_handlers'] = handlers
    return {k: v for k, v in details.items() if v not in (None, '', [], {})}


# ---------------------------------------------------------------------- execution flow

def _owner(name: str) -> str:
    return name.split('::', 1)[0]


def _implementation_steps(kg, method: str) -> List[Tuple[str, str]]:
    """interface method -> the same method on each implementation (via 'implemented_by')."""
    if '::' not in method:
        return []
    owner, member = method.split('::', 1)
    steps = []
    for impl, _ in _edges(kg, owner, 'implemented_by'):
        candidate = f'{impl}::{member}'
        if candidate in kg.graph:
            steps.append((candidate, 'implemented_by'))
    return steps


def _repository_steps(kg, node: EntityNode) -> List[Tuple[str, str, str]]:
    """Calls to inherited Spring Data methods (save, findById...) are not declared: step to the repository."""
    steps: List[Tuple[str, str, str]] = []
    for name in (node.attributes or {}).get('calls') or []:
        resolved = kg.resolve_entity(name)
        if resolved and kg.entities.get(resolved, {}).get('declaring_chunk_ids'):
            continue
        owner = name.split('::', 1)[0] if '::' in name else name.rsplit('.', 1)[0] if '.' in name else None
        member = name.split('::', 1)[1] if '::' in name else name.rsplit('.', 1)[-1]
        owner_entity = kg.resolve_entity(owner) if owner else None
        owner_node = _node(kg, owner_entity) if owner_entity else None
        if owner_entity and isinstance(owner_node, EntityNode) and \
                (owner_node.attributes.get('jpa') or {}).get('repository'):
            steps.append((owner_entity, 'calls', f'{owner_entity}::{member}'))
    return steps


def _next_steps(kg, node_id: str) -> List[Tuple[str, str, Optional[str]]]:
    node = _node(kg, node_id)
    steps: List[Tuple[str, str, Optional[str]]] = []
    if isinstance(node, ResourceNode):
        if node.node_type == 'endpoint':
            steps += [(t, 'handled_by', None) for t, _ in _edges(kg, node_id, 'handled_by')]
        elif node.node_type == 'channel':
            steps += [(s, 'consumed_by', None) for s, _ in _edges(kg, node_id, 'listens_to', 'in')]
        return steps
    if not isinstance(node, EntityNode):
        return steps
    steps += [(t, 'implemented_by', None) for t, _ in _implementation_steps(kg, node_id)]
    for relation in ('calls', 'queries', 'publishes', 'calls_endpoint', 'persists', 'maps_to'):
        steps += [(t, relation, None) for t, _ in _edges(kg, node_id, relation)]
    steps += [(t, relation, label) for t, relation, label in _repository_steps(kg, node)]
    return steps


def resolve_start(kg, start: str) -> str:
    if start in kg.graph:
        return start
    try:
        return find_endpoint(kg, start)
    except LookupError_:
        pass
    if f'channel:{start}' in kg.graph:
        return f'channel:{start}'
    return resolve_entity(kg, start)


def _trivial(kg, node_id: str) -> bool:
    """Steps hidden by default in a trace: fields / record components and Lombok accessors."""
    node = _node(kg, node_id)
    return isinstance(node, EntityNode) and (node.entity_type in ('variable', 'enum_constant')
                                             or bool((node.attributes or {}).get('synthetic')))


def trace_flow(kg, start: str, max_depth: int = 6, limit: int = 60, include_trivial: bool = False) -> Dict[str, Any]:
    """
    Execution flow from an endpoint, a method or a channel, following
    handled_by -> calls -> implemented_by -> repository (persists) -> entity (maps_to) -> table,
    plus queries (method -> table), publishes (-> channel -> consumed_by listeners) and calls_endpoint.
    """
    root = resolve_start(kg, start)
    steps: List[Dict[str, Any]] = []
    lines = [f'{root} ({_kind(kg, root)})']
    visited = {root}
    truncated = False

    def walk(node_id: str, depth: int) -> None:
        nonlocal truncated
        if depth > max_depth:
            return
        for target, relation, label in _next_steps(kg, node_id):
            if not include_trivial and relation == 'calls' and _trivial(kg, target):
                continue
            if len(steps) >= limit:
                truncated = True
                return
            seen = target in visited
            node = _node(kg, target)
            step = {'depth': depth, 'from': node_id, 'relation': relation, 'node': target,
                    'kind': _kind(kg, target)}
            if label:
                step['call'] = label
            loc = location(node)
            if loc:
                step['location'] = loc
            sites = (kg.graph.edges[node_id, target].get('sites') or []) if kg.graph.has_edge(node_id, target) else []
            if sites:  # where the call happens in the caller, with the argument bindings
                step['call_site'] = f"{sites[0].get('file')}:{sites[0].get('line')}"
                if sites[0].get('args'):
                    step['args'] = sites[0]['args']
            if seen:
                step['seen'] = True
            steps.append(step)
            shown = label or target
            lines.append(f"{'  ' * depth}--{relation}--> {shown} ({step['kind']})"
                         + (f' [{loc}]' if loc else '')
                         + (f" (called at {step['call_site']})" if sites else '') + (' (see above)' if seen else ''))
            if not seen:
                visited.add(target)
                walk(target, depth + 1)

    walk(root, 1)
    return {'start': root, 'steps': steps, 'truncated': truncated, 'text': '\n'.join(lines)}


def _kind(kg, node_id: str) -> str:
    node = _node(kg, node_id)
    if isinstance(node, EntityNode):
        spring = (node.attributes or {}).get('spring') or {}
        return spring.get('stereotype') or node.entity_type or 'entity'
    return getattr(node, 'node_type', '?')


# ---------------------------------------------------------------------- beans

def list_beans(kg, stereotype: Optional[str] = None, profile: Optional[str] = None) -> List[Dict[str, Any]]:
    beans = []
    for node in entities_with(kg, 'spring', 'stereotype'):
        spring = node.attributes['spring']
        if stereotype and spring.get('stereotype') != stereotype:
            continue
        if profile and profile not in (spring.get('profiles') or []):
            continue
        beans.append({'id': node.id, 'location': location(node),
                      **{k: spring.get(k) for k in ('stereotype', 'bean_name', 'bean_type', 'scope', 'profiles',
                                                     'conditions', 'primary', 'qualifier')
                         if spring.get(k) not in (None, '', [], False)}})
    return sorted(beans, key=lambda b: (b['stereotype'], b['id']))


def bean_dependencies(kg, bean: str, direction: str = 'both', depth: int = 1) -> Dict[str, Any]:
    name = resolve_entity(kg, bean)
    result: Dict[str, Any] = {'bean': name, **{k: v for k, v in entity_summary(kg, name).items() if k != 'id'}}

    def collect(direction_key: str) -> List[Dict[str, Any]]:
        items, frontier, seen = [], [(name, 1)], {name}
        while frontier:
            current, level = frontier.pop(0)
            edges = _edges(kg, current, 'injects', 'out' if direction_key == 'injects' else 'in')
            for other, data in edges:
                items.append({'from': current, 'bean': other, 'depth': level,
                              **{k: data.get(k) for k in ('via', 'point', 'qualifier', 'confidence') if data.get(k)}})
                if other not in seen and level < depth:
                    seen.add(other)
                    frontier.append((other, level + 1))
        return items

    if direction in ('both', 'injects'):
        result['injects'] = collect('injects')
        injections = ((_node(kg, name).attributes or {}).get('spring') or {}).get('injections') or []
        unresolved = [i for i in injections if i.get('unresolved')]
        if unresolved:
            result['unresolved_injections'] = unresolved
    if direction in ('both', 'injected_into'):
        result['injected_into'] = collect('injected_into')
    return result


def find_implementations(kg, type_name: str) -> Dict[str, Any]:
    name = resolve_entity(kg, type_name)
    implementations = [{**entity_summary(kg, t), 'direct': bool(d.get('direct'))}
                       for t, d in _edges(kg, name, 'implemented_by')]
    subclasses, frontier, seen = [], [name], {name}
    while frontier:
        current = frontier.pop()
        for child, _ in _edges(kg, current, 'inherits', 'in'):
            if child not in seen:
                seen.add(child)
                frontier.append(child)
                subclasses.append({**entity_summary(kg, child), 'parent': current})
    return {'type': name, 'implementations': implementations, 'subtypes': subclasses}


def find_by_annotation(kg, annotation: str, entity_type: Optional[str] = None) -> List[Dict[str, Any]]:
    wanted = annotation.lstrip('@')
    simple = wanted.rsplit('.', 1)[-1]
    found = []
    for _, data in kg.graph.nodes(data=True):
        node = data.get('data')
        if not isinstance(node, EntityNode) or (entity_type and node.entity_type != entity_type):
            continue
        for item in (node.attributes or {}).get('annotations') or []:
            if not isinstance(item, dict):
                continue
            if item.get('name') == simple and ('.' not in wanted or item.get('qualified') in (wanted, simple)):
                found.append({'id': node.id, 'type': node.entity_type, 'location': location(node),
                              'args': item.get('args') or {}})
                break
    return sorted(found, key=lambda f: f['id'])


# ---------------------------------------------------------------------- persistence

def data_model(kg, entity: Optional[str] = None) -> Dict[str, Any]:
    nodes = entities_with(kg, 'jpa')
    focus = resolve_entity(kg, entity) if entity else None
    model_entities, repositories = [], []
    for node in nodes:
        jpa = node.attributes['jpa']
        if jpa.get('repository'):
            repositories.append({'id': node.id, 'domain': jpa.get('domain'), 'id_type': jpa.get('id_type'),
                                 'base': jpa.get('base'), 'location': location(node)})
            continue
        if not jpa.get('kind'):
            continue
        model_entities.append({
            'id': node.id, 'kind': jpa.get('kind'), 'table': jpa.get('table'), 'id_field': jpa.get('id'),
            'columns': jpa.get('columns') or [], 'associations': jpa.get('associations') or [],
            'location': location(node)})
    if focus:
        related = {focus}
        for target, _ in _edges(kg, focus, 'relates_to') + _edges(kg, focus, 'relates_to', 'in'):
            related.add(target)
        model_entities = [e for e in model_entities if e['id'] in related]
        repositories = [r for r in repositories if r['domain'] == focus]
    tables = []
    for table in resources(kg, 'table'):
        if focus and table.attributes.get('entity') not in {e['id'] for e in model_entities}:
            continue
        tables.append({'id': table.id, **{k: table.attributes.get(k) for k in
                                          ('name', 'sources', 'entity', 'columns', 'migration_columns', 'drift')
                                          if table.attributes.get(k) not in (None, [], {}, '')},
                       'migrations': [s for s, _ in _edges(kg, table.id, 'migrates', 'in')],
                       'queried_by': [s for s, _ in _edges(kg, table.id, 'queries', 'in')]})
    return {'entities': sorted(model_entities, key=lambda e: e['id']),
            'repositories': sorted(repositories, key=lambda r: r['id']),
            'tables': sorted(tables, key=lambda t: t['id'])}


# ---------------------------------------------------------------------- configuration

def config_properties(kg, key: Optional[str] = None, prefix: Optional[str] = None,
                      profile: Optional[str] = None) -> List[Dict[str, Any]]:
    result = []
    for node in resources(kg, 'config_property'):
        if key and node.key != key:
            continue
        if prefix and not node.key.startswith(prefix):
            continue
        values = node.attributes.get('values') or {}
        if profile and profile not in values:
            continue
        item = {'key': node.key, 'defined': node.attributes.get('defined', True),
                'values': {profile: values[profile]} if profile else values,
                'files': node.attributes.get('files') or [],
                'readers': [{'entity': s, 'via': d.get('via')} for s, d in _edges(kg, node.id, 'reads_config', 'in')]}
        if node.attributes.get('default_in_code') is not None:
            item['default_in_code'] = node.attributes['default_in_code']
        if node.attributes.get('secret'):
            item['secret'] = True
        result.append(item)
    return sorted(result, key=lambda r: r['key'])


def profiles(kg) -> List[str]:
    return sorted({p for node in resources(kg, 'config_property') for p in (node.attributes.get('values') or {})})


# ---------------------------------------------------------------------- messaging, build, tests

def messaging(kg, channel: Optional[str] = None) -> List[Dict[str, Any]]:
    result = []
    for node in resources(kg, 'channel'):
        if channel and channel not in (node.id, node.key, node.attributes.get('name')):
            continue
        result.append({'id': node.id, 'broker': node.attributes.get('broker'), 'name': node.attributes.get('name'),
                       'producers': [{'entity': s, 'via': d.get('via')} for s, d in _edges(kg, node.id, 'publishes', 'in')],
                       'consumers': [{'entity': s, **({'group': d['group']} if d.get('group') else {})}
                                     for s, d in _edges(kg, node.id, 'listens_to', 'in')]})
    return sorted(result, key=lambda c: c['id'])


# ---------------------------------------------------------------------- security facts

SEVERITY_RANK = {'high': 0, 'medium': 1, 'low': 2}


def _security(node) -> Dict[str, Any]:
    value = (node.attributes or {}).get('security') if isinstance(node, EntityNode) else None
    return value if isinstance(value, dict) else {}


def list_sinks(kg, category: Optional[str] = None, status: Optional[str] = None, cwe: Optional[int] = None,
               include_safe: bool = False, path_prefix: Optional[str] = None) -> List[Dict[str, Any]]:
    """Dangerous API calls with the status of their dangerous argument, most worrying first."""
    from .enrichers.security import STATUS_RANK
    safe = {'safe_by_construction', 'sanitized', 'mitigated'}
    found = []
    for _, data in kg.graph.nodes(data=True):
        node = data.get('data')
        for sink in _security(node).get('sinks') or []:
            if category and sink.get('category') != category:
                continue
            if status and sink.get('status') != status:
                continue
            if cwe and sink.get('cwe') != cwe:
                continue
            if not include_safe and not status and sink.get('status') in safe:
                continue
            if path_prefix and not str(sink.get('file', '')).startswith(path_prefix):
                continue
            found.append({'method': node.id, **sink,
                          'location': f"{sink.get('file')}:{sink.get('line')}" if sink.get('line') else sink.get('file')})
    rank = {s: i for i, s in enumerate(STATUS_RANK)}
    return sorted(found, key=lambda s: (rank.get(str(s.get('status')), 99),
                                        SEVERITY_RANK.get(str(s.get('severity')), 3), str(s.get('location'))))


def list_sources(kg, kind: Optional[str] = None) -> List[Dict[str, Any]]:
    """Untrusted inputs: request-bound parameters, message payloads and calls returning request data."""
    found = []
    for _, data in kg.graph.nodes(data=True):
        node = data.get('data')
        for source in _security(node).get('sources') or []:
            if kind and source.get('kind') != kind:
                continue
            found.append({'method': node.id, 'location': location(node), **source})
    return sorted(found, key=lambda s: (s['method'], str(s.get('param') or s.get('call'))))


def _sink_brief(sink: Dict[str, Any]) -> Dict[str, Any]:
    """A sink without its flows (they are returned by trace_taint): flow count and entry points instead."""
    brief = {k: v for k, v in sink.items() if k != 'flows'}
    flows = sink.get('flows') or []
    if flows:
        brief['flow_count'] = len(flows)
        entries = sorted({str(f['source'].get('entry')) for f in flows if f['source'].get('entry')})
        if entries:
            brief['entries'] = entries[:5]
    return brief


def trace_taint(kg, target: Optional[str] = None, category: Optional[str] = None, cwe: Optional[int] = None,
                min_confidence: str = 'low') -> List[Dict[str, Any]]:
    """Source -> sink paths recorded by the taint propagation, with each call site crossed.

    `target` keeps the flows whose entry endpoint, source method, sink method or any crossed method matches
    (endpoint spec such as 'GET /api/users', method, or class: every method of the class).
    """
    from .enrichers.taint import CONFIDENCE_RANK
    wanted: Optional[set] = None
    endpoint: Optional[str] = None
    tainted = list_sinks(kg, category=category, cwe=cwe, status='tainted')
    entries = {f['source'].get('entry') for s in tainted for f in s.get('flows') or []}
    if target and (target in entries or f'listener of {target}' in entries):  # 'listener of kafka:orders'
        endpoint = target if target in entries else f'listener of {target}'
    elif target:
        try:
            endpoint = find_endpoint(kg, target).removeprefix('endpoint:')
        except LookupError_:
            resolved = resolve_entity(kg, target)
            wanted = {resolved}
            prefix = resolved + '::'
            wanted |= {n for n in kg.graph if str(n).startswith(prefix)}
    limit_rank = CONFIDENCE_RANK.get(min_confidence, 2)
    found = []
    for sink in tainted:
        for flow in sink.get('flows') or []:
            if CONFIDENCE_RANK.get(flow['confidence'], 2) > limit_rank:
                continue
            methods = {flow['source'].get('method'), sink['method']} | {s['to'] for s in flow['steps']}
            if endpoint and flow['source'].get('entry') != endpoint:
                continue
            if wanted is not None and not methods & wanted:
                continue
            found.append({'category': sink['category'], 'cwe': sink['cwe'], 'severity': sink.get('severity'),
                          'confidence': flow['confidence'], 'source': flow['source'],
                          'steps': [{**s, 'location': f"{s.get('file')}:{s.get('line')}"} for s in flow['steps']],
                          'sink': {'method': sink['method'], 'api': sink['api'], 'location': sink['location'],
                                   'local_status': sink.get('local_status', 'tainted')}})
    return sorted(found, key=lambda f: (CONFIDENCE_RANK.get(f['confidence'], 2),
                                        SEVERITY_RANK.get(str(f.get('severity')), 3), len(f['steps'])))


def find_callers(kg, entity: str, max_depth: int = 4, limit: int = 100) -> Dict[str, Any]:
    """Transitive callers of a method (calls edges, interface methods included) and the endpoints reaching it."""
    start = resolve_entity(kg, entity)
    targets = [start]
    if '::' in start:  # a call to the interface method reaches the implementation
        owner, member = start.split('::', 1)
        for interface, _ in _edges(kg, owner, 'implemented_by', 'in'):
            if f'{interface}::{member}' in kg.graph:
                targets.append(f'{interface}::{member}')
    callers: List[Dict[str, Any]] = []
    seen = set(targets)
    frontier = [(t, 0) for t in targets]
    endpoints: Dict[str, int] = {}
    while frontier and len(callers) < limit:
        node_id, depth = frontier.pop(0)
        for endpoint, _ in _edges(kg, node_id, 'handled_by', 'in'):
            endpoints.setdefault(str(endpoint).removeprefix('endpoint:'), depth)
        for channel, _ in _edges(kg, node_id, 'listens_to'):
            endpoints.setdefault(f'listener of {channel}', depth)
        if depth >= max_depth:
            continue
        for caller, _, data in kg.graph.in_edges(node_id, data=True):
            if data.get('relation') != 'calls' and not data.get('sites'):
                continue
            if caller in seen or not isinstance(_node(kg, caller), EntityNode):
                continue
            seen.add(caller)
            sites = [f"{s.get('file')}:{s.get('line')}" for s in data.get('sites') or []][:3]
            callers.append({'caller': caller, 'callee': node_id, 'depth': depth + 1, 'sites': sites})
            frontier.append((caller, depth + 1))
            for interface_method in _interface_methods(kg, caller):
                if interface_method not in seen:
                    seen.add(interface_method)
                    frontier.append((interface_method, depth + 1))
    return {'entity': start, 'callers': callers[:limit],
            'entry_points': [{'entry': e, 'depth': d} for e, d in sorted(endpoints.items(), key=lambda x: (x[1], x[0]))],
            'truncated': len(callers) >= limit}


def _interface_methods(kg, method: str) -> List[str]:
    if '::' not in method:
        return []
    owner, member = method.split('::', 1)
    return [f'{i}::{member}' for i, _ in _edges(kg, owner, 'implemented_by', 'in') if f'{i}::{member}' in kg.graph]


# ---------------------------------------------------------------------- security posture

def security_posture(kg, severity: Optional[str] = None) -> Dict[str, Any]:
    """Spring Security configuration, method security, CORS, risky properties and the candidate findings."""
    node = _node(kg, 'security_posture:application')
    if not isinstance(node, ResourceNode):
        return {'spring_security': False, 'filter_chains': [], 'method_security': [], 'ignored_paths': [], 'cors': [],
                'access_summary': {}, 'findings': [], 'findings_by_id': {}}
    posture = dict(node.attributes)
    limit = SEVERITY_RANK.get(severity or 'low', 2)
    findings = [f for f in posture.get('findings') or [] if SEVERITY_RANK.get(str(f.get('severity')), 2) <= limit]
    posture['findings'] = sorted(findings, key=lambda f: (SEVERITY_RANK.get(str(f.get('severity')), 3), str(f.get('id'))))
    posture['findings_by_id'] = dict(Counter(str(f.get('id')) for f in findings).most_common())
    return posture


ACCESS_ORDER = {'public': 0, 'anonymous': 0, 'unknown': 1, 'authenticated': 2, 'expression': 3, 'authority': 3,
                'role': 3, 'denied': 4, 'broker': 5}


def attack_surface(kg, access: Optional[str] = None, http_method: Optional[str] = None,
                   path_prefix: Optional[str] = None, with_findings: bool = False) -> List[Dict[str, Any]]:
    """Entry points (HTTP, GraphQL, message listeners) with their effective authorization, untrusted inputs,
    taint flows starting there and candidate findings; least protected first."""
    flows: Dict[str, List[str]] = {}
    for sink in list_sinks(kg, status='tainted'):
        for flow in sink.get('flows') or []:
            entry = flow['source'].get('entry')
            if entry:
                flows.setdefault(str(entry), []).append(f"{sink['category']}@{sink['location']}")
    rows = []
    for node in list_endpoints(kg):
        attributes = node.attributes
        entry = node.id.removeprefix('endpoint:')
        info = attributes.get('access') or {}
        handler = attributes.get('handler')
        sources = _security(_node(kg, handler)).get('sources') or [] if handler else []
        decided: Dict[str, Any] = next((v for v in info.get('via') or [] if v.get('source') != 'annotation'), {})
        annotation = next((v for v in info.get('via') or [] if v.get('source') == 'annotation'), None)
        row: Dict[str, Any] = {
            'entry': entry, 'kind': 'graphql' if attributes.get('http_method') == 'GRAPHQL' else 'http',
            'access': info.get('level', 'unknown'), 'roles': info.get('roles') or [],
            'decided_by': {k: decided.get(k) for k in ('source', 'rule', 'patterns', 'file', 'line', 'chain')
                           if decided.get(k) is not None},
            'handler': handler, 'location': location(node),
            'inputs': [s.get('param') or s.get('call') for s in sources][:8],
            'taint_flows': sorted(set(flows.get(entry, [])))[:5],
            'findings': info.get('findings') or []}
        if annotation:
            row['annotation'] = {'name': annotation.get('annotation'), 'value': annotation.get('value'),
                                 'effective': annotation.get('effective')}
        if info.get('alternatives'):
            row['alternatives'] = [{'chain': a['chain'], 'status': a['status'], 'access': a['level']}
                                   for a in info['alternatives']]
        rows.append(row)
    for channel in resources(kg, 'channel'):
        for listener, _ in _edges(kg, channel.id, 'listens_to', 'in'):
            entry = f"listener of {channel.id.removeprefix('channel:')}"
            sources = _security(_node(kg, listener)).get('sources') or []
            rows.append({'entry': entry, 'kind': 'message', 'access': 'broker', 'roles': [], 'decided_by': {},
                         'handler': listener, 'location': location(_node(kg, listener)),
                         'inputs': [s.get('param') or s.get('call') for s in sources][:8],
                         'taint_flows': sorted(set(flows.get(entry, [])))[:5], 'findings': []})
    method = http_method.upper() if http_method else None
    rows = [r for r in rows
            if (not access or r['access'] == access)
            and (not method or r['entry'].split(' ', 1)[0] in (method, 'ANY'))
            and (not path_prefix or r['entry'].split(' ', 1)[-1].startswith(path_prefix))
            and (not with_findings or r['findings'] or r['taint_flows'])]
    return sorted(rows, key=lambda r: (ACCESS_ORDER.get(r['access'], 1), -len(r['taint_flows']), -len(r['findings']),
                                       r['entry']))


# ---------------------------------------------------------------------- supply chain

def resolve_dependency(kg, name: str) -> str:
    """'dep:g:a', 'g:a' or a unique artifact id -> dependency node id."""
    for candidate in (name, f'dep:{name}'):
        if isinstance(_node(kg, candidate), ResourceNode):
            return candidate
    matches = [n.id for n in resources(kg, 'dependency') if n.attributes.get('artifact_id') == name]
    if len(matches) == 1:
        return matches[0]
    hint = f" Candidates: {', '.join(sorted(matches)[:8])}" if matches else ''
    raise LookupError_(f"Dependency '{name}' not found. Use list_dependencies.{hint}")


def dependency_usage(kg, dependency: str, include_tests: bool = False, max_depth: int = 5) -> Dict[str, Any]:
    """Files importing a library, the methods they declare and the entry points that reach those methods."""
    dep_id = resolve_dependency(kg, dependency)
    node = _node(kg, dep_id)
    files = [{'file': s, 'imports': d.get('imports') or [], 'test': bool(d.get('test'))}
             for s, d in _edges(kg, dep_id, 'uses_dependency', 'in') if include_tests or not d.get('test')]
    file_set = {f['file'] for f in files}
    methods = [n for n, d in kg.graph.nodes(data=True) if isinstance(d.get('data'), EntityNode)
               and getattr(d['data'], 'file_path', None) in file_set
               and d['data'].entity_type in ('method', 'api_endpoint', 'constructor')]
    entries: Dict[str, Dict[str, Any]] = {}
    seen = set(methods)
    frontier = [(m, 0, m) for m in methods]
    while frontier:
        current, depth, origin = frontier.pop(0)
        for endpoint, _ in _edges(kg, current, 'handled_by', 'in'):
            key = str(endpoint).removeprefix('endpoint:')
            entries.setdefault(key, {'entry': key, 'depth': depth, 'through': origin})
        for channel, _ in _edges(kg, current, 'listens_to'):
            key = f"listener of {str(channel).removeprefix('channel:')}"
            entries.setdefault(key, {'entry': key, 'depth': depth, 'through': origin})
        if depth >= max_depth:
            continue
        callers = [s for s, _, d in kg.graph.in_edges(current, data=True) if d.get('relation') == 'calls' or d.get('sites')]
        callers += _interface_methods(kg, current)
        for caller in callers:
            if caller not in seen and isinstance(_node(kg, caller), EntityNode):
                seen.add(caller)
                frontier.append((caller, depth + 1, origin))
    attributes = node.attributes if isinstance(node, ResourceNode) else {}
    return {'dependency': dep_id, 'resolved_version': attributes.get('resolved_version'),
            'version_source': attributes.get('version_source'), 'packages': attributes.get('packages') or [],
            'files': sorted(files, key=lambda f: f['file']), 'methods': len(methods),
            'entry_points': sorted(entries.values(), key=lambda e: (e['depth'], e['entry']))}


def check_dependencies(kg, client, include_tests: bool = False, refresh: bool = False) -> Dict[str, Any]:
    """Known vulnerabilities (OSV, opt-in) of the dependencies with a resolved version, and their reachability."""
    test_scopes = {'test', 'testImplementation', 'testRuntimeOnly', 'testCompileOnly', 'testCompile'}
    checked, skipped = [], []
    for node in resources(kg, 'dependency'):
        attributes = node.attributes
        scopes = set(attributes.get('scopes') or [])
        if scopes and scopes <= test_scopes and not include_tests:
            continue
        version = attributes.get('resolved_version')
        if not version:
            skipped.append({'dependency': node.id, 'reason': attributes.get('managed_by') or 'version unknown'})
            continue
        checked.append((node, f"{attributes.get('group_id')}:{attributes.get('artifact_id')}", str(version)))
    answers = client.lookup([(name, version) for _, name, version in checked], refresh=refresh)
    vulnerable: List[Dict[str, Any]] = []
    sources: Counter = Counter()
    for node, name, version in checked:
        answer = answers.get(f'{name}@{version}') or {'vulns': [], 'source': 'not_checked'}
        sources[answer['source']] += 1
        if answer['source'] == 'not_checked':
            continue
        node.attributes['vulnerabilities'] = answer['vulns']
        if answer['vulns']:
            usage = dependency_usage(kg, node.id, include_tests)
            node.attributes['reachability'] = {'files': [f['file'] for f in usage['files']][:20],
                                               'entry_points': usage['entry_points'][:10]}
            vulnerable.append({'dependency': node.id, 'version': version, 'vulnerabilities': answer['vulns'],
                               'files': len(usage['files']), 'entry_points': usage['entry_points'][:10]})
    vulnerable.sort(key=lambda v: (-len(v['entry_points']), min(SEVERITY_RANK.get(x['severity'], 2)
                                                                 for x in v['vulnerabilities'])))
    return {'enabled': bool(client.enabled), 'checked': len(checked), 'sources': dict(sources),
            'vulnerable': vulnerable, 'not_checked': skipped}


# ---------------------------------------------------------------------- code access

MAX_CODE_LINES = 200


def resolve_file(kg, file_path: str) -> str:
    normalized = file_path.replace('\\', '/').lstrip('./')
    if isinstance(_node(kg, normalized), FileNode):
        return normalized
    matches = [n for n, d in kg.graph.nodes(data=True) if type(d.get('data')) is FileNode
               and (str(n).endswith('/' + normalized) or str(n) == normalized)]
    if len(matches) == 1:
        return matches[0]
    hint = f" Candidates: {', '.join(sorted(matches)[:8])}" if matches else ''
    raise LookupError_(f"File '{file_path}' not found.{hint}")


def get_code(kg, file_path: str, start_line: int = 1, end_line: Optional[int] = None,
             context: int = 0) -> Dict[str, Any]:
    """Exact lines of a file (secrets masked), at most MAX_CODE_LINES."""
    path = resolve_file(kg, file_path)
    lines = (_node(kg, path).content or '').splitlines()
    start = max(1, int(start_line) - max(0, context))
    end = min(len(lines), int(end_line or start_line) + max(0, context))
    if end < start:
        raise ValueError(f'start_line {start_line} is after the end of {path} ({len(lines)} lines)')
    truncated = end - start + 1 > MAX_CODE_LINES
    end = min(end, start + MAX_CODE_LINES - 1)
    width = len(str(end))
    code = '\n'.join(f'{n:>{width}}  {lines[n - 1]}' for n in range(start, end + 1))
    return {'file': path, 'start_line': start, 'end_line': end, 'total_lines': len(lines), 'code': code,
            'truncated': truncated}


def grep_code(kg, pattern: str, path_prefix: Optional[str] = None, case_insensitive: bool = False,
              max_results: int = 50) -> Dict[str, Any]:
    """Regular-expression search in the file contents (secrets masked), one match per line."""
    if len(pattern) > 300:
        raise ValueError('pattern too long (300 characters at most)')
    try:
        regex = re.compile(pattern, re.IGNORECASE if case_insensitive else 0)
    except re.error as e:
        raise ValueError(f'invalid regular expression: {e}') from e
    matches: List[Dict[str, Any]] = []
    files = 0
    for node_id, data in sorted(kg.graph.nodes(data=True), key=lambda x: str(x[0])):
        node = data.get('data')
        if type(node) is not FileNode or (path_prefix and not str(node_id).startswith(path_prefix)):
            continue
        hit = False
        for number, line in enumerate((node.content or '').splitlines(), 1):
            if len(line) <= 2000 and regex.search(line):
                hit = True
                if len(matches) < max_results:
                    matches.append({'file': node_id, 'line': number, 'code': line.strip()[:300]})
        files += hit
    return {'pattern': pattern, 'matches': matches, 'files_with_matches': files,
            'truncated': len(matches) >= max_results}


def security_summary(kg) -> Dict[str, Any]:
    statuses: Counter = Counter()
    categories: Counter = Counter()
    sources: Counter = Counter()
    flows = crossing = 0
    for _, data in kg.graph.nodes(data=True):
        facts = _security(data.get('data'))
        for sink in facts.get('sinks') or []:
            statuses[sink.get('status')] += 1
            categories[sink.get('category')] += 1
            flows += len(sink.get('flows') or [])
            crossing += sum(1 for f in sink.get('flows') or [] if f.get('steps'))
        for source in facts.get('sources') or []:
            sources[source.get('kind')] += 1
    summary: Dict[str, Any] = {'sinks_by_status': dict(statuses.most_common()), 'sinks_by_category': dict(categories.most_common()),
               'sources_by_kind': dict(sources.most_common()),
               'taint_flows': {'total': flows, 'inter_procedural': crossing}}
    posture = _node(kg, 'security_posture:application')
    if isinstance(posture, ResourceNode):
        summary['access'] = posture.attributes.get('access_summary') or {}
        summary['posture_findings'] = len(posture.attributes.get('findings') or [])
    return summary


def scheduled_jobs(kg) -> List[Dict[str, Any]]:
    return [{'id': n.id, 'schedule': n.attributes['spring']['schedule'], 'location': location(n)}
            for n in entities_with(kg, 'spring', 'schedule')]


def dependencies(kg, module: Optional[str] = None, name_query: Optional[str] = None) -> List[Dict[str, Any]]:
    result = []
    modules = [n for n in resources(kg, 'module') if not module or module in (n.id, n.key)]
    for mod in modules:
        for target, data in _edges(kg, mod.id, 'depends_on'):
            if name_query and name_query.lower() not in target.lower():
                continue
            dep = _node(kg, target)
            attributes = dep.attributes if isinstance(dep, ResourceNode) else {}
            item = {'module': mod.id, 'dependency': target, 'scope': data.get('scope'), 'version': data.get('version')}
            for key in ('resolved_version', 'version_source', 'managed_by', 'used_by_files'):
                if attributes.get(key) not in (None, ''):
                    item[key] = attributes[key]
            result.append(item)
    return sorted(result, key=lambda d: (d['module'], d['dependency']))


def tests_for(kg, entity: str) -> Dict[str, Any]:
    name = resolve_entity(kg, entity)
    tests = []
    for source, data in _edges(kg, name, 'tests', 'in'):
        test_node = _node(kg, source)
        info = ((test_node.attributes if isinstance(test_node, EntityNode) else {}) or {}).get('test') or {}
        tests.append({'test': source, 'via': data.get('via'), 'kind': info.get('kind'),
                      'test_methods': info.get('test_methods'), 'location': location(test_node)})
    # Tests of the owner class when asked about a method
    if '::' in name and not tests:
        owner = tests_for(kg, _owner(name)) if _owner(name) in kg.graph else {'tests': []}
        tests = owner['tests']
    return {'entity': name, 'tests': tests}


# ---------------------------------------------------------------------- overview

def project_overview(kg) -> Dict[str, Any]:
    languages: Counter = Counter()
    for _, data in kg.graph.nodes(data=True):
        node = data.get('data')
        if isinstance(node, FileNode) and node.node_type == 'file':
            languages[node.language or 'other'] += 1
    modules = [{'id': n.id, **{k: n.attributes.get(k) for k in ('build_tool', 'group_id', 'version', 'packaging',
                                                                   'java_version', 'spring_boot_version')
                                if n.attributes.get(k)}} for n in resources(kg, 'module')]
    stereotypes: Counter = Counter(n.attributes['spring']['stereotype'] for n in entities_with(kg, 'spring', 'stereotype'))
    endpoints = resources(kg, 'endpoint')
    served = [e for e in endpoints if not e.attributes.get('external')]
    jpa = entities_with(kg, 'jpa')
    configs = resources(kg, 'config_property')
    overview = {
        'enrichers': list(kg.metadata.get('enrichers') or kg.graph.graph.get('enrichers') or []),
        'languages': dict(languages.most_common()),
        'modules': modules,
        'java_version': next((m.get('java_version') for m in modules if m.get('java_version')), None),
        'spring_boot_version': next((m.get('spring_boot_version') for m in modules if m.get('spring_boot_version')), None),
        'beans_by_stereotype': dict(stereotypes.most_common()),
        'endpoints': {'served': len(served), 'external': len(endpoints) - len(served),
                      'by_method': dict(Counter(e.attributes.get('http_method') for e in served).most_common())},
        'jpa_entities': sum(1 for n in jpa if n.attributes['jpa'].get('kind') and not n.attributes['jpa'].get('repository')),
        'repositories': sum(1 for n in jpa if n.attributes['jpa'].get('repository')),
        'tables': len(resources(kg, 'table')),
        'channels': len(resources(kg, 'channel')),
        'config_properties': {'defined': sum(1 for c in configs if c.attributes.get('defined', True)),
                              'undefined_but_read': sum(1 for c in configs if not c.attributes.get('defined', True))},
        'profiles': profiles(kg),
        'scheduled_jobs': len(entities_with(kg, 'spring', 'schedule')),
        'test_classes': len(entities_with(kg, 'test', 'kind')),
        'graphql_operations': sum(1 for e in served if e.attributes.get('protocol') == 'graphql'
                                  and e.attributes.get('implemented') is not False),
        'mybatis_statements': len(entities_with(kg, 'mybatis', 'statement')),
        'security': security_summary(kg),
        'dependencies': len(resources(kg, 'dependency')),
    }
    return {k: v for k, v in overview.items() if v not in (None, [], {}, 0) or k in ('endpoints',)}


def format_overview(overview: Dict[str, Any]) -> str:
    lines = ['Project overview']
    if overview.get('languages'):
        lines.append('Files by language: ' + ', '.join(f'{k}={v}' for k, v in overview['languages'].items()))
    for key, label in (('java_version', 'Java'), ('spring_boot_version', 'Spring Boot')):
        if overview.get(key):
            lines.append(f'{label}: {overview[key]}')
    for module in overview.get('modules') or []:
        details = ', '.join(f'{k}={v}' for k, v in module.items() if k != 'id')
        lines.append(f"Module {module['id']}: {details}")
    if overview.get('beans_by_stereotype'):
        lines.append('Beans: ' + ', '.join(f'{k}={v}' for k, v in overview['beans_by_stereotype'].items()))
    endpoints = overview.get('endpoints') or {}
    lines.append(f"Endpoints: {endpoints.get('served', 0)} served"
                 + (f" ({', '.join(f'{k}={v}' for k, v in endpoints.get('by_method', {}).items())})"
                    if endpoints.get('by_method') else '')
                 + (f", {endpoints['external']} outgoing" if endpoints.get('external') else ''))
    for key, label in (('graphql_operations', 'GraphQL operations'), ('mybatis_statements', 'MyBatis statements'),
                       ('jpa_entities', 'JPA entities'), ('repositories', 'Repositories'), ('tables', 'Tables'),
                       ('channels', 'Messaging channels'), ('scheduled_jobs', 'Scheduled jobs'),
                       ('test_classes', 'Test classes'), ('dependencies', 'Library dependencies')):
        if overview.get(key):
            lines.append(f'{label}: {overview[key]}')
    if overview.get('config_properties'):
        cp = overview['config_properties']
        lines.append(f"Configuration properties: {cp['defined']} defined, {cp['undefined_but_read']} read but undefined")
    if overview.get('profiles'):
        lines.append('Profiles: ' + ', '.join(overview['profiles']))
    security = overview.get('security') or {}
    if security.get('sinks_by_status'):
        lines.append('Dangerous API calls by status: '
                     + ', '.join(f'{k}={v}' for k, v in security['sinks_by_status'].items()))
    if security.get('sources_by_kind'):
        lines.append('Untrusted inputs: ' + ', '.join(f'{k}={v}' for k, v in security['sources_by_kind'].items()))
    taint = security.get('taint_flows') or {}
    if taint.get('total'):
        lines.append(f"Taint flows (source -> sink): {taint['total']}, {taint['inter_procedural']} across methods "
                     "(trace_taint)")
    if security.get('access'):
        lines.append('Endpoint access: ' + ', '.join(f'{k}={v}' for k, v in security['access'].items())
                     + f" ({security.get('posture_findings', 0)} posture finding(s): get_security_posture)")
    if overview.get('enrichers'):
        lines.append('Enrichers applied: ' + ', '.join(overview['enrichers']))
    lines.append('Next: list_endpoints, then trace_flow on an endpoint; list_beans / get_data_model / get_config.')
    return '\n'.join(lines)


def iter_ids(items: Iterable[Dict[str, Any]], key: str = 'id') -> List[str]:
    return [i[key] for i in items]
