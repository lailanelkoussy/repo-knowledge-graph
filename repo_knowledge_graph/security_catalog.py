"""
Loader of security_catalog.yaml: sinks (dangerous APIs with their CWE), sources (untrusted input) and
sanitizers. The catalog is data: extend the YAML file to support more APIs.
"""
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

CATALOG_PATH = Path(__file__).with_name('security_catalog.yaml')
ALL = 'all'


@dataclass(frozen=True)
class Sink:
    id: str
    category: str
    cwe: int
    severity: str
    receivers: FrozenSet[str]
    members: FrozenSet[str]
    arg: Any = 0                       # int, 'any' or 'none'
    always: bool = False
    dangerous_values: Optional[re.Pattern] = None
    safe_values: Optional[re.Pattern] = None
    mitigated_by: Tuple[Tuple[str, re.Pattern], ...] = ()
    context: Optional[re.Pattern] = None
    context_exclude: Optional[re.Pattern] = None  # method names never meant for secrets


@dataclass(frozen=True)
class Sanitizer:
    members: FrozenSet[str]
    receivers: FrozenSet[str]          # empty: any receiver
    categories: FrozenSet[str]

    def cleans(self, category: str) -> bool:
        return ALL in self.categories or category in self.categories


@dataclass
class Catalog:
    sinks: List[Sink]
    param_annotations: Dict[str, str]
    framework_param_types: FrozenSet[str]
    source_invocations: List[Tuple[str, FrozenSet[str], FrozenSet[str]]]  # (kind, receivers, members)
    sanitizers: List[Sanitizer]
    _sink_index: Dict[Tuple[str, str], List[Sink]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for sink in self.sinks:
            for receiver in sink.receivers:
                for member in sink.members:
                    self._sink_index.setdefault((receiver, member), []).append(sink)

    def sinks_for(self, receiver: Optional[str], member: str) -> List[Sink]:
        return self._sink_index.get((simple(receiver), member), [])

    def source_kind(self, receiver: Optional[str], member: str) -> Optional[str]:
        name = simple(receiver)
        for kind, receivers, members in self.source_invocations:
            if name in receivers and member in members:
                return kind
        return None

    def sanitizer(self, receiver: Optional[str], member: str) -> Optional[Sanitizer]:
        name = simple(receiver)
        for sanitizer in self.sanitizers:
            if member in sanitizer.members and (not sanitizer.receivers or name in sanitizer.receivers):
                return sanitizer
        return None

    @property
    def sanitizer_members(self) -> FrozenSet[str]:
        return frozenset(m for s in self.sanitizers for m in s.members)


def simple(type_name: Optional[str]) -> str:
    return (type_name or '').split('<')[0].rsplit('.', 1)[-1]


def _pattern(value: Optional[str]) -> Optional[re.Pattern]:
    return re.compile(value) if value else None


@lru_cache(maxsize=4)
def load_catalog(path: Optional[str] = None) -> Catalog:
    import yaml
    data = yaml.safe_load(Path(path or CATALOG_PATH).read_text(encoding='utf-8')) or {}
    sinks = []
    for item in data.get('sinks') or []:
        sinks.append(Sink(
            id=item['id'], category=item['category'], cwe=int(item.get('cwe') or 0),
            severity=item.get('severity', 'medium'),
            receivers=frozenset(item.get('receivers') or []), members=frozenset(item.get('members') or []),
            arg=item.get('arg', 0), always=bool(item.get('always')),
            dangerous_values=_pattern(item.get('dangerous_values')), safe_values=_pattern(item.get('safe_values')),
            mitigated_by=tuple((m['member'], re.compile(m.get('pattern') or '.*'))
                               for m in item.get('mitigated_by') or []),
            context=_pattern(item.get('context')), context_exclude=_pattern(item.get('context_exclude'))))
    sources = data.get('sources') or {}
    return Catalog(
        sinks=sinks,
        param_annotations=dict(sources.get('param_annotations') or {}),
        framework_param_types=frozenset(sources.get('framework_param_types') or []),
        source_invocations=[(s['kind'], frozenset(s.get('receivers') or []), frozenset(s.get('members') or []))
                            for s in sources.get('invocations') or []],
        sanitizers=[Sanitizer(frozenset(s.get('members') or []), frozenset(s.get('receivers') or []),
                              frozenset(s.get('categories') or [ALL])) for s in data.get('sanitizers') or []],
    )
