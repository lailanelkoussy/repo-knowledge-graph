"""
Inter-procedural taint propagation, run at the end of the `security` enricher.

Untrusted data is followed forward from every source (handler parameters, `request.getParameter(...)`...)
through the `calls` edges and their argument bindings, down to the sinks classified by the enricher:

- **inside a method**, the dependencies of a value are read from its shape (computed by `java_extractor`): concatenation parts one by one
  (typed values such as numbers or UUIDs do not carry a payload), local variables, sanitizer calls
  (`via:<member>:...`, which clean the categories listed in the catalog) and calls to methods of the
  repository, replaced by what the callee returns (method summary: which parameters reach its return value);
- **across methods**, an argument depending on tainted data taints the parameter it binds to; a call to an
  interface method continues in every implementation (confidence lowered when there are several);
- **asynchronous flows** are not followed through the broker: a listener parameter is a source of kind
  `message`, and the flow lists the methods publishing on the same channel.

A sink reached by a flow becomes `tainted` (its intra-procedural status is kept in `local_status`) and lists
up to five `flows`: the source (method, kind, parameter or call, entry endpoint) and the call sites crossed
(`file:line`, parameter bound), with a confidence: `high` when every step is resolved, `medium` through an
interface with several implementations or a value only approximated (local defined in several ways); `low` is
reserved for flows through fields (not followed yet). A `depends_on_param` sink that no source reaches gets
`reachable_from_source: false`.

Assumed limits (carried by the confidence, documented in the README): no alias analysis, no reflection,
collections taken as a whole, flow-insensitive def-use, fields shared between requests not followed.
"""
import logging
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

from ..security_catalog import ALL, simple

logger = logging.getLogger(__name__)

MAX_DEPTH = 8
MAX_FLOWS_PER_SINK = 5
CONFIDENCE_RANK = {'high': 0, 'medium': 1, 'low': 2}
RESOLVABLE_STATUSES = ('depends_on_param', 'depends_on_call', 'depends_on_field', 'unknown', 'tainted')

Atom = Tuple[str, FrozenSet[str], str]  # (origin, categories cleaned on the way, confidence)


def _lower(a: str, b: str) -> str:
    return a if CONFIDENCE_RANK[a] >= CONFIDENCE_RANK[b] else b


class TaintPropagator:
    def __init__(self, enricher: Any, methods: Dict[str, Dict[str, Any]]):
        self.enricher = enricher
        self.context = enricher.context
        self.catalog = enricher.catalog
        self.methods = methods  # canonical -> {path, sources, source_sites, sinks} for methods with facts
        self.callees: Dict[str, Dict[str, List[Tuple[str, bool]]]] = {}
        self.entries = self._entries()
        self.channels = self._channels()
        self._returns: Dict[str, Set[Atom]] = {}
        self._computing: Set[str] = set()
        self._sites: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._arg_atoms: Dict[Tuple[str, str, int], Set[Atom]] = {}
        self._build_callees()

    # ------------------------------------------------------------------ graph helpers

    def _entries(self) -> Dict[str, List[str]]:
        """Handler method -> endpoint ids (GET /api/users...)."""
        found: Dict[str, List[str]] = {}
        for source, target, data in self.context.graph.edges(data=True):
            if data.get('relation') == 'handled_by':
                found.setdefault(target, []).append(str(source).removeprefix('endpoint:'))
        return found

    def _channels(self) -> Dict[str, List[Dict[str, Any]]]:
        """Listener method -> channels it listens to, with the methods publishing there."""
        publishers: Dict[str, List[str]] = {}
        listeners: Dict[str, List[str]] = {}
        for source, target, data in self.context.graph.edges(data=True):
            if data.get('relation') == 'publishes':
                publishers.setdefault(target, []).append(source)
            elif data.get('relation') == 'listens_to':
                listeners.setdefault(source, []).append(target)
        return {method: [{'channel': str(c).removeprefix('channel:'), 'publishers': sorted(publishers.get(c, []))[:5]}
                         for c in channels] for method, channels in listeners.items()}

    def _build_callees(self) -> None:
        """method -> call site -> [(callee, reached through interface dispatch)] from the `sites` of call edges."""
        graph = self.context.graph
        implementations: Dict[str, List[str]] = {}
        for source, target, data in graph.edges(data=True):
            if data.get('relation') == 'implemented_by':
                implementations.setdefault(source, []).append(target)
        for source, target, data in graph.edges(data=True):
            for site in data.get('sites') or []:
                if not site.get('site'):
                    continue
                targets = [(target, False)]
                owner, _, member = str(target).partition('::')
                impls = [f'{impl}::{member}' for impl in implementations.get(owner, [])]
                impls = [m for m in impls if self.context.declared(self.context.registry.lookup(m) or m)]
                if member and impls:
                    targets += [(self.context.registry.lookup(m) or m, len(impls) > 1) for m in impls]
                bucket = self.callees.setdefault(source, {}).setdefault(site['site'], [])
                bucket.extend(t for t in targets if t not in bucket)

    def _params(self, method: str) -> List[Dict[str, Any]]:
        return self.context.entity_attributes(method).get('params') or []

    def _by_site(self, method: str) -> Dict[str, Dict[str, Any]]:
        if method not in self._sites:
            invocations = self.context.entity_attributes(method).get('invocations') or []
            self._sites[method] = {inv['site']: inv for inv in invocations if inv.get('site')}
        return self._sites[method]

    # ------------------------------------------------------------------ dependencies of a value

    def _cleaned(self, member: str) -> FrozenSet[str]:
        sanitizer = self.catalog.sanitizer(None, member) or next(
            (s for s in self.catalog.sanitizers if member in s.members), None)
        if sanitizer is None or not sanitizer.categories or ALL in sanitizer.categories:
            return frozenset({'*'})
        return frozenset(sanitizer.categories)

    def _origin_atoms(self, method: str, origin: str, cleaned: FrozenSet[str], confidence: str,
                      depth: int) -> Set[Atom]:
        if origin.startswith('via:'):
            _, member, rest = origin.split(':', 2)
            return self._origin_atoms(method, rest, cleaned | self._cleaned(member), confidence, depth)
        if origin.startswith('call:'):
            site = origin[len('call:'):]
            kind = (self.methods.get(method) or {}).get('source_sites', {}).get(site)
            if kind:
                return {(f'source:{kind}@{method}@{site}', cleaned, confidence)}
            if site in self.callees.get(method, {}):
                return {(o, c | cleaned, _lower(k, confidence))
                        for o, c, k in self._call_result(method, site, depth)}
            return set()  # library call: its arguments are already among the origins
        if origin.startswith(('param:', 'field:')):
            return {(origin, cleaned, confidence)}
        return set()

    def atoms(self, method: str, shape: Dict[str, Any], depth: int = 0) -> Set[Atom]:
        """What a value depends on: parameters, fields and sources of `method`, with sanitizers met."""
        if depth > MAX_DEPTH:
            return set()
        enricher = self.enricher
        enricher.by_site = self._by_site(method)
        enricher.param_types = {p.get('name'): p.get('type') for p in self._params(method)}
        if shape.get('shape') == 'concat' and shape.get('parts'):
            found: Set[Atom] = set()
            for part in shape['parts']:
                enricher.by_site = self._by_site(method)
                enricher.param_types = {p.get('name'): p.get('type') for p in self._params(method)}
                if part.get('shape') in ('literal', 'constant') or enricher._typed_value(part):
                    continue
                found |= self.atoms(method, part, depth + 1)
            return found
        if shape.get('constant') or enricher._typed_value(shape):
            return set()
        site = shape.get('site')
        if shape.get('shape') in ('call', 'local') and site and site in self.callees.get(method, {}):
            invocation = self._by_site(method).get(site) or {}
            own = {f'call:{site}'}
            for arg in invocation.get('arg_shapes') or []:
                own.update(arg.get('origins') or [])
            precise = shape.get('shape') == 'call' or set(shape.get('origins') or []) <= own | {
                o for o in shape.get('origins') or [] if o.startswith('field:')}
            result = self._call_result(method, site, depth)
            if precise:
                return result
            extra = {o for o in shape.get('origins') or [] if o not in own}
            found = {(o, c, _lower(k, 'medium')) for o, c, k in result}
            for origin in extra:
                found |= self._origin_atoms(method, origin, frozenset(), 'medium', depth + 1)
            return found
        found = set()
        for origin in shape.get('origins') or []:
            found |= self._origin_atoms(method, origin, frozenset(), 'high', depth + 1)
        return found

    def _call_result(self, method: str, site: str, depth: int) -> Set[Atom]:
        """Dependencies of the value returned by the call at `site`, mapped back to the caller's values."""
        invocation = self._by_site(method).get(site) or {}
        args = invocation.get('arg_shapes') or []
        found: Set[Atom] = set()
        for callee, dispatched in self.callees.get(method, {}).get(site, []):
            names = [p.get('name') for p in self._params(callee)]
            for origin, cleaned, confidence in self.returns(callee):
                if dispatched:
                    confidence = _lower(confidence, 'medium')
                if origin.startswith('param:'):
                    name = origin[len('param:'):]
                    if name in names and names.index(name) < len(args):
                        for o, c, k in self.atoms(method, args[names.index(name)], depth + 1):
                            found.add((o, c | cleaned, _lower(k, confidence)))
                elif origin.startswith('source:'):
                    found.add((origin, cleaned, confidence))
        return found

    def returns(self, method: str) -> Set[Atom]:
        """Method summary: parameters and sources reaching the return value (fields of the callee dropped)."""
        if method in self._returns:
            return self._returns[method]
        if method in self._computing:
            return set()  # recursion: the fixed point is approximated by cutting the cycle
        self._computing.add(method)
        dataflow = self.context.entity_attributes(method).get('dataflow') or {}
        found: Set[Atom] = set()
        for shape in dataflow.get('return_shapes') or []:
            found |= {a for a in self.atoms(method, shape) if not a[0].startswith('field:')}
        self._computing.discard(method)
        self._returns[method] = found
        return found

    # ------------------------------------------------------------------ propagation

    def _sink_atoms(self, method: str, sink: Dict[str, Any]) -> Set[Atom]:
        shape = sink.get('_shape')
        if shape is not None:
            return self.atoms(method, shape)
        return {(o, frozenset(), 'high') for o in sink.get('origins') or [] if o.startswith(('param:', 'field:'))}

    def _source_info(self, method: str, origin: str) -> Dict[str, Any]:
        info: Dict[str, Any]
        if origin.startswith('source:'):
            kind, source_method, site = origin[len('source:'):].split('@', 2)
            facts = self.methods.get(source_method) or {}
            call: Dict[str, Any] = next((s for s in facts.get('sources', []) if s.get('site') == site), {})
            info = {'kind': kind, 'method': source_method, 'call': call.get('call'),
                    'file': facts.get('path'), 'line': call.get('line')}
            method = source_method
        else:
            name = origin[len('param:'):]
            facts = self.methods.get(method) or {}
            param: Dict[str, Any] = next((s for s in facts.get('sources', []) if s.get('param') == name), {})
            info = {'kind': param.get('kind'), 'method': method, 'param': name, 'file': facts.get('path')}
            if param.get('validated'):
                info['validated'] = True
        if self.entries.get(method):
            info['entry'] = self.entries[method][0]
        if self.channels.get(method) and info.get('kind') == 'message':
            info['channels'] = self.channels[method]
            info.setdefault('entry', f"listener of {self.channels[method][0]['channel']}")
        return info

    def run(self) -> Dict[str, int]:
        sink_atoms: Dict[str, List[Tuple[Dict[str, Any], Set[Atom]]]] = {}
        for method, facts in self.methods.items():
            for sink in facts.get('sinks', []):
                if sink.get('status') in RESOLVABLE_STATUSES:
                    sink_atoms.setdefault(method, []).append((sink, self._sink_atoms(method, sink)))
        flows_found = 0
        for method in sorted(set(self.methods) | set(self.callees)):
            facts = self.methods.get(method) or {}
            starts = {f"param:{s['param']}" for s in facts.get('sources', []) if s.get('param')}
            # sources returned by a call (request.getParameter, or a helper wrapping it) are always tainted
            self._walk(method, starts, frozenset(), 'high', [], set(), sink_atoms, origin_method=method)
        for entries in sink_atoms.values():
            for sink, _ in entries:
                flows = sink.get('flows')
                if flows:
                    flows_found += len(flows)
                    if sink['status'] != 'tainted':
                        sink['local_status'] = sink['status']
                        sink['status'] = 'tainted'
                    sink['confidence'] = min((f['confidence'] for f in flows), key=CONFIDENCE_RANK.__getitem__)
                    sink['flows'] = sorted(flows, key=lambda f: (CONFIDENCE_RANK[f['confidence']], len(f['steps'])))
                elif sink.get('status') == 'depends_on_param':
                    sink['reachable_from_source'] = False
        return {'flows': flows_found}

    def _walk(self, method: str, tainted: Set[str], cleaned: FrozenSet[str], confidence: str,
              steps: List[Dict[str, Any]], visited: Set[Tuple[str, str, FrozenSet[str]]],
              sink_atoms: Dict[str, List[Tuple[Dict[str, Any], Set[Atom]]]], origin_method: str,
              origin: Optional[str] = None) -> None:
        def matches(atom_origin: str) -> bool:
            return atom_origin in tainted or (not steps and atom_origin.startswith('source:'))

        for sink, atoms in sink_atoms.get(method, []):
            for atom_origin, atom_cleaned, atom_confidence in sorted(atoms):
                all_cleaned = cleaned | atom_cleaned
                if not matches(atom_origin) or sink['category'] in all_cleaned or '*' in all_cleaned:
                    continue
                if atom_origin.startswith('field:'):
                    continue
                self._record(sink, origin or atom_origin, origin_method, steps, _lower(confidence, atom_confidence))
                break
        if len(steps) >= MAX_DEPTH:
            return
        by_site = self._by_site(method)
        for site, callees in self.callees.get(method, {}).items():
            args = (by_site.get(site) or {}).get('arg_shapes') or []
            if not args:
                continue
            for index, arg in enumerate(args):
                key_atoms = (method, site, index)
                if key_atoms not in self._arg_atoms:
                    self._arg_atoms[key_atoms] = self.atoms(method, arg)
                arg_atoms = [a for a in self._arg_atoms[key_atoms] if matches(a[0])]
                if not arg_atoms:
                    continue
                atom_origin, atom_cleaned, atom_confidence = min(
                    arg_atoms, key=lambda a: (len(a[1]), CONFIDENCE_RANK[a[2]]))
                for callee, dispatched in callees:
                    params = self._params(callee)
                    if index >= len(params):
                        continue
                    param = params[index]
                    if simple(param.get('type')) in self.enricher.typed_values:
                        continue  # bound to a number / UUID / date parameter: no payload goes through
                    key = (callee, str(param.get('name')), cleaned | atom_cleaned)
                    if key in visited:
                        continue
                    step = {'from': method, 'to': callee, 'file': (self.methods.get(method) or {}).get('path')
                            or self._path(method), 'line': (by_site.get(site) or {}).get('line'),
                            'param': param.get('name')}
                    if dispatched:
                        step['dispatch'] = 'interface'
                    next_confidence = _lower(_lower(confidence, atom_confidence), 'medium' if dispatched else 'high')
                    self._walk(callee, {f"param:{param.get('name')}"}, cleaned | atom_cleaned, next_confidence,
                               steps + [step], visited | {key}, sink_atoms, origin_method,
                               origin or atom_origin)

    def _path(self, method: str) -> Optional[str]:
        record = self.context.registry.entities.get(method) or {}
        return record.get('file_path') or (record.get('declaring_files') or [None])[0]

    def _record(self, sink: Dict[str, Any], origin: str, origin_method: str, steps: List[Dict[str, Any]],
                confidence: str) -> None:
        flows = sink.setdefault('flows', [])
        flow = {'source': self._source_info(origin_method, origin), 'steps': steps, 'confidence': confidence}
        if any(f['source'] == flow['source'] and f['steps'] == steps for f in flows):
            return
        if len(flows) < MAX_FLOWS_PER_SINK:
            flows.append(flow)


def propagate(enricher: Any, methods: Dict[str, Dict[str, Any]]) -> Dict[str, int]:
    return TaintPropagator(enricher, methods).run()


def strip_private(records: Iterable[Dict[str, Any]]) -> None:
    for record in records:
        record.pop('_shape', None)


__all__ = ['propagate', 'strip_private', 'TaintPropagator', 'MAX_DEPTH']
