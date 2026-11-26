"""
Registry of code entities (classes, functions, variables...) and their aliases.

`EntityRegistry.entities` keeps the historical public format used by
`RepoKnowledgeGraph.entities`:

    {canonical_name: {"declaring_chunk_ids": [...], "calling_chunk_ids": [...],
                      "type": [...], "dtype": str | None, "aliases": [...],
                      "attributes": {...}, "file_path": str, "start_line": int, "end_line": int}}

`attributes` keeps everything else an extractor knows about a declaration (annotations, modifiers,
signature, REST endpoint, base classes, framework data added by enrichers...).

Lookups are O(1): the alias map and the simple-name index are maintained
incrementally instead of being rebuilt for every chunk / call.
"""
import copy
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Set

from .utils.path_utils import extract_simple_name

# Keys of an extracted entity dict that are not copied into `attributes`
_RECORD_KEYS = {"name", "type", "dtype", "aliases", "extra_aliases", "exclude_aliases", "attributes",
                "file_path", "start_line", "end_line"}


def new_entity_record() -> dict:
    return {
        "declaring_chunk_ids": [],
        "calling_chunk_ids": [],
        "type": [],
        "dtype": None,
        "aliases": [],
        "attributes": {},
        "file_path": "",
        "start_line": 0,
        "end_line": 0,
    }


def _is_qualified(name: str) -> bool:
    return '.' in name or '::' in name


def merge_attributes(target: Dict[str, Any], source: Dict[str, Any]) -> None:
    """Merge declaration attributes: lists are united, dicts merged, scalars overwritten."""
    for key, value in (source or {}).items():
        current = target.get(key)
        if isinstance(current, list) and isinstance(value, list):
            for item in value:
                if item not in current:
                    current.append(item)
        elif isinstance(current, dict) and isinstance(value, dict):
            merge_attributes(current, value)
        else:
            target[key] = value


def normalize_entity_record(info: dict) -> dict:
    """Fill missing keys of a record coming from an older serialized graph."""
    record = new_entity_record()
    record.update({k: v for k, v in (info or {}).items() if v is not None or k == 'dtype'})
    for key in ("declaring_chunk_ids", "calling_chunk_ids", "type", "aliases"):
        value = record.get(key) or []
        record[key] = list(value) if not isinstance(value, str) else [value]
    record["attributes"] = dict(record.get("attributes") or {})
    record["file_path"] = record.get("file_path") or ""
    for key in ("start_line", "end_line"):
        try:
            record[key] = int(record.get(key) or 0)
        except (TypeError, ValueError):
            record[key] = 0
    return record


class EntityRegistry:
    def __init__(self, entities: Optional[Dict[str, dict]] = None):
        self.entities: Dict[str, dict] = {}
        self._alias_map: Dict[str, str] = {}
        # simple name -> canonical names of *declared* entities having an alias with that simple name
        self._by_simple: Dict[str, Set[str]] = defaultdict(set)
        self._links: Set[tuple] = set()  # (canonical, kind, chunk_id) already recorded
        # aliases claimed by several entities: never resolved directly (the imports / package decide)
        self._ambiguous: Set[str] = set()
        for name, info in (entities or {}).items():
            record = normalize_entity_record(info)
            self.entities[name] = record
            self._index_alias(name, name)
            for alias in record["aliases"]:
                self._index_alias(alias, name)
            for chunk_id in record["declaring_chunk_ids"]:
                self._links.add((name, 'declaring_chunk_ids', chunk_id))
            for chunk_id in record["calling_chunk_ids"]:
                self._links.add((name, 'calling_chunk_ids', chunk_id))
            if record["declaring_chunk_ids"]:
                self._index_declared(name)

    # ------------------------------------------------------------------ indexing

    def _index_alias(self, alias: str, canonical: str) -> None:
        if not alias:
            return
        current = self._alias_map.get(alias)
        if current is None:
            self._alias_map[alias] = canonical
        elif current != canonical and alias != current and alias not in self.entities:
            self._ambiguous.add(alias)

    def _index_declared(self, canonical: str) -> None:
        record = self.entities[canonical]
        for alias in [canonical] + record["aliases"]:
            if alias:
                self._by_simple[extract_simple_name(alias)].add(canonical)

    def _link(self, canonical: str, kind: str, chunk_id: str) -> None:
        key = (canonical, kind, chunk_id)
        if key not in self._links:
            self._links.add(key)
            self.entities[canonical][kind].append(chunk_id)

    @property
    def alias_map(self) -> Dict[str, str]:
        return self._alias_map

    def lookup(self, alias: str) -> Optional[str]:
        return self._alias_map.get(alias)

    # ------------------------------------------------------------------ registration

    def register_declared(self, entity: dict, chunk_id: str) -> Optional[str]:
        """Register an entity declared in `chunk_id`; returns its canonical name."""
        name = entity.get("name")
        if not name:
            return None
        excluded = set(entity.get("exclude_aliases") or [])
        aliases = [a for a in list(entity.get("aliases") or []) + list(entity.get("extra_aliases") or [])
                   if a and a not in excluded]

        canonical = self._alias_map.get(name)
        if canonical is None:
            for alias in aliases:
                # A bare simple name ('find') must not merge two different qualified declarations
                # ('com.a.OrderService::find' and 'com.b.UserService::find')
                if (alias in self._alias_map and _is_qualified(name) and not _is_qualified(alias)
                        and _is_qualified(self._alias_map[alias])):
                    continue
                if alias in self._alias_map:
                    canonical = self._alias_map[alias]
                    break
        if canonical is None:
            canonical = name
            self.entities[canonical] = new_entity_record()
            self._index_alias(canonical, canonical)

        record = self.entities[canonical]
        self._link(canonical, "declaring_chunk_ids", chunk_id)
        entity_type = entity.get("type")
        if entity_type and entity_type not in record["type"]:
            record["type"].append(entity_type)
        if entity.get("dtype"):
            record["dtype"] = entity["dtype"]
        # Deep copies: the entity dicts stay owned by the chunk / file nodes
        attributes = copy.deepcopy({k: v for k, v in entity.items()
                                    if k not in _RECORD_KEYS and v not in (None, [], {})})
        merge_attributes(attributes, copy.deepcopy(entity.get("attributes") or {}))
        merge_attributes(record["attributes"], attributes)
        if entity.get("file_path") and not record["file_path"]:
            record["file_path"] = entity["file_path"]
            record["start_line"] = int(entity.get("start_line") or 0)
            record["end_line"] = int(entity.get("end_line") or 0)
        for alias in [name] + aliases:
            if alias not in record["aliases"]:
                record["aliases"].append(alias)
            self._index_alias(alias, canonical)
        self._index_declared(canonical)
        return canonical

    def register_called(self, called_name: str, chunk_id: str, imports: Iterable[str] = ()) -> Optional[str]:
        """Register a call/usage of `called_name` in `chunk_id`; returns the canonical entity name."""
        if not called_name or not called_name.strip():
            return None
        canonical = self.resolve(called_name, imports)
        if canonical is None:
            canonical = called_name
            if canonical not in self.entities:
                self.entities[canonical] = new_entity_record()
                self._index_alias(canonical, canonical)
        self._link(canonical, "calling_chunk_ids", chunk_id)
        return canonical

    # ------------------------------------------------------------------ resolution

    def resolve(self, called_name: str, imports: Iterable[str] = ()) -> Optional[str]:
        """
        Resolve a called name to a canonical entity.

        Order: exact alias -> name qualified by one of the file imports -> unique declared
        entity with the same simple name (optionally narrowed by the imports).
        Ambiguous matches return None instead of guessing.
        """
        if not called_name or not called_name.strip():
            return None
        if called_name in self._alias_map and called_name not in self._ambiguous:
            return self._alias_map[called_name]

        imports = [i for i in imports if i]
        for import_path in imports:
            for sep in ('.', '::'):
                canonical = self._alias_map.get(f"{import_path}{sep}{called_name}")
                if canonical:
                    return canonical

        candidates = self._by_simple.get(extract_simple_name(called_name), set())
        if '::' in called_name:
            # 'pkg.Type::member' (Java / C++): only a member of a type with the same simple name can match
            owner, member = called_name.rsplit('::', 1)
            owner_simple = owner.replace('::', '.').rsplit('.', 1)[-1]
            suffixes = (f'{owner_simple}::{member}', f'{owner_simple}.{member}')
            candidates = {c for c in candidates
                          if any(a == suffixes[0] or a == suffixes[1] or a.endswith(('.' + suffixes[0], '::' + suffixes[0],
                                                                                       '.' + suffixes[1]))
                                 for a in [c] + self.entities[c]["aliases"])}
        if called_name in self._alias_map and len(candidates) > 1:
            # ambiguous alias: keep the candidates consistent with the imports / package
            narrowed = self._narrow(candidates, called_name, imports)
            return narrowed[0] if len(narrowed) == 1 else None
        if len(candidates) == 1:
            return next(iter(candidates))
        if len(candidates) > 1:
            narrowed = self._narrow(candidates, called_name, imports)
            if len(narrowed) == 1:
                return narrowed[0]
        return None

    def _narrow(self, candidates: Set[str], called_name: str, imports: List[str]) -> List[str]:
        """Keep candidates consistent with the qualified call or with the file imports."""
        if '.' in called_name or '::' in called_name:
            suffix_matches = [
                c for c in candidates
                if any(a == called_name or a.endswith('.' + called_name) or a.endswith('::' + called_name)
                       for a in [c] + self.entities[c]["aliases"])
            ]
            if suffix_matches:
                candidates = set(suffix_matches)
        if imports and len(candidates) > 1:
            imported = [
                c for c in candidates
                if any(a.lstrip('.').startswith(imp + '.') or a.startswith(imp + '::') or a == imp
                       for imp in imports for a in [c] + self.entities[c]["aliases"])
            ]
            if imported:
                candidates = set(imported)
        return sorted(candidates)

    def declared(self) -> List[str]:
        """Canonical names of the entities declared in the repository."""
        return [name for name, record in self.entities.items() if record["declaring_chunk_ids"]]

    def ambiguous_candidates(self, called_name: str) -> List[str]:
        """Declared entities a simple name could refer to (useful for debugging resolution)."""
        return sorted(self._by_simple.get(extract_simple_name(called_name), set()))
