"""
Persistence: JPA entities, Spring Data repositories and SQL migrations.

- @Entity / @Embeddable / @MappedSuperclass (and Mongo @Document): `attributes['jpa']` (table, columns,
  id), `table` node (`table:<name>`, Spring's default physical naming: OrderLine -> order_line) and
  `maps_to` edge; associations (@OneToMany, @ManyToOne...) give `relates_to` edges between entities
  with cardinality, field, mapped_by, fetch and cascade.
- Spring Data repositories: `persists` edge (repository -> domain entity), id type, derived query
  methods (`findByStatusAndCustomerId` -> fields) and @Query (JPQL or native SQL, with the tables they
  read: `queries` edges, method -> table).
- Migrations: Flyway (`db/migration/V1__init.sql`, `schema.sql`) and Liquibase changelogs (XML / YAML)
  give `migrates` edges (file -> table) and the columns created by the scripts. When a table is known
  both from the entities and from the migrations, `drift` lists the columns declared on one side only.
"""
import logging
import posixpath
import re
from typing import Any, Dict, List, Optional

from .base import (
    EnrichmentContext,
    annotation,
    annotation_names,
    annotation_value,
    as_list,
    enum_name,
    strip_class_literal,
)
from .spring_beans import SPRING_DATA_REPOSITORIES

logger = logging.getLogger(__name__)

ENTITY_ANNOTATIONS = ('Entity', 'Embeddable', 'MappedSuperclass', 'Document', 'Table')
ASSOCIATIONS = {'OneToMany': 'one_to_many', 'ManyToOne': 'many_to_one', 'OneToOne': 'one_to_one',
                'ManyToMany': 'many_to_many', 'ElementCollection': 'element_collection'}
DERIVED_QUERY = re.compile(
    r'^(find|read|get|query|search|stream|count|exists|delete|remove)(?:[A-Z]\w*?)??(?:First\d*|Top\d*|Distinct)?By([A-Z]\w*)$')
QUERY_OPERATORS = re.compile(
    r'(IsNotNull|IsNull|NotNull|Null|IsNot|Not|IgnoreCase|AllIgnoreCase|GreaterThanEqual|GreaterThan|'
    r'LessThanEqual|LessThan|Between|NotLike|Like|StartingWith|EndingWith|Containing|NotIn|In|IsTrue|True|'
    r'IsFalse|False|Before|After|Exists|Regex|Near|Within|Is|Equals)$')
SQL_TABLE_REFERENCE = re.compile(r'\b(?:from|join|into|update|table)\s+([`"\[]?[\w.]+[`"\]]?)', re.I)
CREATE_TABLE = re.compile(r'create\s+table\s+(?:if\s+not\s+exists\s+)?([`"\[]?[\w.]+[`"\]]?)\s*\((.*?)\)\s*;',
                          re.I | re.S)
ALTER_ADD = re.compile(r'alter\s+table\s+(?:if\s+exists\s+)?([`"\[]?[\w.]+[`"\]]?)\s+add\s+(?:column\s+)?'
                       r'(?:if\s+not\s+exists\s+)?([`"\[]?\w+[`"\]]?)', re.I)
SQL_CONSTRAINTS = ('primary', 'constraint', 'foreign', 'unique', 'index', 'key', 'check', 'exclude')


def applies(context: EnrichmentContext) -> bool:
    for _, entity in context.java_declarations():
        names = set(annotation_names(entity.get('attributes')))
        if names & set(ENTITY_ANNOTATIONS) or any(b.rsplit('.', 1)[-1] in SPRING_DATA_REPOSITORIES
                                                    for b in entity.get('bases') or []):
            return True
    return any(_is_migration(p) for p in context.analyses)


def snake_case(name: str) -> str:
    """Spring Boot default physical naming (CamelCaseToUnderscoresNamingStrategy)."""
    name = re.sub(r'(?<=[a-z0-9])([A-Z])', r'_\1', name)
    name = re.sub(r'(?<=[A-Z])([A-Z][a-z])', r'_\1', name)
    return name.replace('.', '_').lower()


def table_key(name: str) -> str:
    return name.strip('`"[]').rsplit('.', 1)[-1].lower()


def _is_migration(path: str) -> bool:
    lower = path.lower()
    base = posixpath.basename(lower)
    if lower.endswith('.sql'):
        return '/db/migration' in '/' + lower or re.match(r'^[vr]\d*.*__.*\.sql$', base) is not None \
            or base in ('schema.sql',) or '/migrations/' in '/' + lower
    return ('changelog' in lower or '/db/changelog' in '/' + lower) and lower.endswith(('.xml', '.yaml', '.yml'))


def _split_columns(body: str) -> List[str]:
    parts, depth, current = [], 0, ''
    for char in body:
        if char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
        if char == ',' and depth == 0:
            parts.append(current.strip())
            current = ''
        else:
            current += char
    if current.strip():
        parts.append(current.strip())
    return parts


def parse_sql_migration(content: str) -> Dict[str, List[str]]:
    """Table -> created / added columns of a SQL script."""
    content = re.sub(r'--[^\n]*', '', content)
    content = re.sub(r'/\*.*?\*/', '', content, flags=re.S)
    tables: Dict[str, List[str]] = {}
    for match in CREATE_TABLE.finditer(content):
        columns = tables.setdefault(table_key(match.group(1)), [])
        for definition in _split_columns(match.group(2)):
            first = definition.split()[0].strip('`"[]') if definition.split() else ''
            if first and first.lower() not in SQL_CONSTRAINTS and first.lower() not in columns:
                columns.append(first.lower())
    for match in ALTER_ADD.finditer(content):
        column = match.group(2).strip('`"[]').lower()
        if column in SQL_CONSTRAINTS:
            continue
        columns = tables.setdefault(table_key(match.group(1)), [])
        if column not in columns:
            columns.append(column)
    return tables


def parse_liquibase(content: str) -> Dict[str, List[str]]:
    tables: Dict[str, List[str]] = {}
    for match in re.finditer(r'<createTable[^>]*tableName="([^"]+)"[^>]*>(.*?)</createTable>', content, re.S):
        tables[table_key(match.group(1))] = [c.lower() for c in re.findall(r'<column[^>]*name="([^"]+)"', match.group(2))]
    for block in re.split(r'\n\s*-\s*changeSet\s*:', content):
        for match in re.finditer(r'createTable\s*:\s*\n(.*?)(?=\n\s{0,10}-\s*\w+\s*:|\Z)', block, re.S):
            name = re.search(r'tableName\s*:\s*["\']?([\w.]+)', match.group(1))
            if name:
                tables[table_key(name.group(1))] = [c.lower() for c in
                                                    re.findall(r'name\s*:\s*["\']?(\w+)', match.group(1))]
    return tables


class JpaEnricher:
    def __init__(self, context: EnrichmentContext):
        self.context = context
        self.entities: Dict[str, Dict[str, Any]] = {}
        self.foreign_keys: List[tuple] = []  # (target entity, column, owner entity) of unidirectional @OneToMany

    def run(self) -> None:
        for type_name, decl in self.context.java_types.items():
            names = set(annotation_names(decl.get('attributes')))
            if names & set(ENTITY_ANNOTATIONS):
                self.entities[type_name] = decl
        for type_name in self.entities:
            self.add_entity(type_name)
        self.add_foreign_keys()
        for type_name, decl in self.context.java_types.items():
            if decl.get('type') in ('interface', 'class'):
                self.add_repository(type_name, decl)
        self.add_migrations()
        self.compute_drift()

    # ------------------------------------------------------------------ entities

    def _fields(self, type_name: str, seen=None) -> List[dict]:
        """Persistent fields of an entity, inherited @MappedSuperclass / @Entity fields first."""
        seen = seen or set()
        seen.add(type_name)
        decl = self.context.java_types.get(type_name) or {}
        fields: List[dict] = []
        for base in decl.get('bases') or []:
            parent = self.context.resolve(base, decl.get('_path'))
            if parent in self.entities and parent not in seen:
                fields.extend(self._fields(parent, seen))
        for member in self.context.members_of(type_name):
            attributes = member.get('attributes') or {}
            modifiers = attributes.get('modifiers') or []
            if member.get('type') != 'variable' or 'static' in modifiers or 'transient' in modifiers \
                    or annotation(attributes, 'Transient'):
                continue
            fields.append(member)
        return fields

    def add_entity(self, type_name: str) -> None:
        context = self.context
        decl = self.entities[type_name]
        attributes = decl.get('attributes') or {}
        simple = type_name.rsplit('.', 1)[-1]
        names = annotation_names(attributes)
        kind = next((n for n in ('Entity', 'Embeddable', 'MappedSuperclass', 'Document') if n in names), 'Entity')
        table = None
        if kind == 'Entity':
            table = annotation_value(annotation(attributes, 'Table'), 'name', 'value') or snake_case(
                annotation_value(annotation(attributes, 'Entity'), 'name') or simple)
        elif kind == 'Document':
            table = annotation_value(annotation(attributes, 'Document'), 'collection', 'value') or (
                simple[:1].lower() + simple[1:])

        columns, id_field, id_type = [], None, None
        for field in self._fields(type_name):
            field_attributes = field.get('attributes') or {}
            field_name = field['name'].rsplit('::', 1)[-1]
            column_annotation = annotation(field_attributes, 'Column')
            association = next((n for n in annotation_names(field_attributes) if n in ASSOCIATIONS), None)
            is_id = bool(annotation(field_attributes, 'Id', 'EmbeddedId'))
            if is_id:
                id_field, id_type = field_name, field.get('dtype')
            if association:
                self.add_association(type_name, decl, field, field_name, association)
                join = annotation(field_attributes, 'JoinColumn')
                if association in ('ManyToOne', 'OneToOne') and not annotation_value(annotation(
                        field_attributes, association), 'mappedBy'):
                    columns.append({'field': field_name, 'column': (annotation_value(join, 'name') or
                                                                    f'{snake_case(field_name)}_id').lower(),
                                    'type': field.get('dtype'), 'foreign_key': True})
                continue
            if annotation(field_attributes, 'Embedded'):
                embedded = context.resolve(field_attributes.get('type_resolved') or field.get('dtype'), decl['_path'])
                for sub in self._fields(embedded) if embedded in self.entities else []:
                    sub_name = sub['name'].rsplit('::', 1)[-1]
                    columns.append({'field': f'{field_name}.{sub_name}', 'column': snake_case(sub_name),
                                    'type': sub.get('dtype')})
                continue
            column = {'field': field_name,
                      'column': str(annotation_value(column_annotation, 'name') or snake_case(field_name)).lower(),
                      'type': field.get('dtype')}
            for key in ('nullable', 'unique', 'length'):
                value = annotation_value(column_annotation, key)
                if value is not None:
                    column[key] = value
            if is_id:
                column['id'] = True
                generated = annotation(field_attributes, 'GeneratedValue')
                if generated:
                    column['generated'] = enum_name(annotation_value(generated, 'strategy') or 'AUTO')
            if annotation(field_attributes, 'Enumerated'):
                column['enumerated'] = enum_name(annotation_value(annotation(field_attributes, 'Enumerated'), 'value')
                                                 or 'ORDINAL')
            if annotation(field_attributes, 'Version'):
                column['version'] = True
            columns.append(column)

        jpa = context.framework_attributes(type_name, 'jpa')
        jpa.update({'kind': kind.lower() if kind != 'Document' else 'document', 'columns': columns})
        if table:
            jpa['table'] = table
        if id_field:
            jpa['id'] = {'field': id_field, 'type': id_type}
        if table and kind in ('Entity', 'Document'):
            table_id = context.add_resource('table', table_key(table), {
                'name': table, 'sources': ['jpa'] if kind == 'Entity' else ['mongodb'], 'entity': type_name,
                'columns': [c['column'] for c in columns]}, file_path=decl['_path'],
                start_line=int(decl.get('start_line') or 0))
            node = context.graph.nodes[table_id]['data']
            if 'jpa' not in node.attributes.setdefault('sources', []) and kind == 'Entity':
                node.attributes['sources'].append('jpa')
            node.attributes['entity'] = type_name
            node.attributes['columns'] = [c['column'] for c in columns]
            context.add_edge(type_name, table_id, 'maps_to')

    def add_foreign_keys(self) -> None:
        for target, column, owner in self.foreign_keys:
            jpa = self.context.framework_attributes(target, 'jpa')
            columns = jpa.setdefault('columns', [])
            if any(c.get('column') == column for c in columns):
                continue
            columns.append({'column': column, 'foreign_key': True, 'owner': owner})
            table = jpa.get('table')
            table_id = f'table:{table_key(table)}' if table else None
            if table_id and table_id in self.context.graph:
                self.context.graph.nodes[table_id]['data'].attributes.setdefault('columns', []).append(column)

    def add_association(self, owner: str, decl: dict, field: dict, field_name: str, association: str) -> None:
        context = self.context
        attributes = field.get('attributes') or {}
        item = annotation(attributes, association)
        target_name = strip_class_literal(annotation_value(item, 'targetEntity'))
        if not target_name:
            type_args = attributes.get('type_args') or []
            target_name = type_args[-1] if type_args else attributes.get('type_resolved') or field.get('dtype')
        target = context.resolve(target_name, decl['_path'])
        info = {
            'cardinality': ASSOCIATIONS[association],
            'field': field_name,
            'mapped_by': annotation_value(item, 'mappedBy'),
            'fetch': enum_name(annotation_value(item, 'fetch') or '') or None,
            'cascade': [enum_name(c) for c in as_list(annotation_value(item, 'cascade'))] or None,
            'orphan_removal': annotation_value(item, 'orphanRemoval'),
            'join_column': annotation_value(annotation(attributes, 'JoinColumn'), 'name'),
            'join_table': annotation_value(annotation(attributes, 'JoinTable'), 'name'),
            'target': target or target_name,
        }
        info = {k: v for k, v in info.items() if v not in (None, [], '')}
        context.framework_attributes(owner, 'jpa').setdefault('associations', []).append(info)
        if association == 'OneToMany' and info.get('join_column') and not info.get('mapped_by') and target:
            # Unidirectional @OneToMany @JoinColumn: the foreign key lives in the target's table
            self.foreign_keys.append((target, str(info['join_column']).lower(), owner))
        if target and context.declared(target):
            context.add_edge(owner, target, 'relates_to', **{k: v for k, v in info.items() if k != 'target'})

    # ------------------------------------------------------------------ repositories

    def _repository_base(self, type_name: str, seen=None) -> Optional[Dict[str, Any]]:
        decl = self.context.java_types.get(type_name) or {}
        seen = seen or set()
        seen.add(type_name)
        attributes = decl.get('attributes') or {}
        for parent in (attributes.get('extends') or []) + (attributes.get('implements') or []):
            simple = parent.get('name', '').rsplit('.', 1)[-1]
            if simple in SPRING_DATA_REPOSITORIES and parent.get('type_args'):
                return {'base': simple, 'type_args': parent['type_args']}
            resolved = self.context.resolve(parent.get('name'), decl.get('_path'))
            if resolved and resolved not in seen and resolved in self.context.java_types:
                found = self._repository_base(resolved, seen)
                if found:
                    return found
        return None

    def add_repository(self, type_name: str, decl: dict) -> None:
        context = self.context
        base = self._repository_base(type_name)
        if not base:
            return
        domain_name, id_type = base['type_args'][0], base['type_args'][1] if len(base['type_args']) > 1 else None
        domain = context.resolve(domain_name, decl['_path'])
        context.framework_attributes(type_name, 'jpa').update({
            'repository': True, 'base': base['base'], 'domain': domain or domain_name, 'id_type': id_type})
        if domain and context.declared(domain):
            context.add_edge(type_name, domain, 'persists', base=base['base'])
        domain_table = ((context.registry.entities.get(domain) or {}).get('attributes') or {}).get('jpa', {}).get('table') \
            if domain else None
        for member in context.members_of(type_name):
            if member.get('type') not in ('method', 'api_endpoint'):
                continue
            method = context.registry.lookup(member['name'])
            if not method:
                continue
            attributes = member.get('attributes') or {}
            method_name = member['name'].rsplit('::', 1)[-1]
            query: Dict[str, Any] = {}
            query_annotation = annotation(attributes, 'Query')
            if query_annotation:
                text = annotation_value(query_annotation, 'value', 'nativeQuery')
                native = annotation_value(query_annotation, 'nativeQuery') is True
                query = {'kind': 'native' if native else 'jpql', 'text': text if isinstance(text, str) else None}
                if annotation(attributes, 'Modifying'):
                    query['modifying'] = True
                tables = self.query_tables(query.get('text') or '', native, decl['_path'])
                if tables:
                    query['tables'] = tables
                    for table in tables:
                        table_id = context.add_resource('table', table_key(table), {'name': table, 'sources': []})
                        context.add_edge(method, table_id, 'queries')
            else:
                match = DERIVED_QUERY.match(method_name)
                if match:
                    criteria = re.split(r'(?<=[a-z0-9])OrderBy(?=[A-Z])', match.group(2), maxsplit=1)
                    fields = []
                    for part in re.split(r'(?<=[a-z0-9])(?:And|Or)(?=[A-Z])', criteria[0]):
                        part = QUERY_OPERATORS.sub('', part) if part else part
                        if part:
                            fields.append(part[:1].lower() + part[1:])
                    query = {'kind': 'derived', 'operation': match.group(1), 'fields': fields}
                    if len(criteria) > 1:
                        query['order_by'] = criteria[1]
                    if domain_table:
                        query['tables'] = [domain_table]
            if query:
                context.framework_attributes(method, 'jpa')['query'] = query

    def query_tables(self, text: str, native: bool, path: str) -> List[str]:
        tables = []
        for match in SQL_TABLE_REFERENCE.finditer(text or ''):
            name = match.group(1).strip('`"[]')
            if native:
                tables.append(name)
                continue
            entity = self.context.resolve(name, path)
            jpa = ((self.context.registry.entities.get(entity) or {}).get('attributes') or {}).get('jpa', {}) \
                if entity else {}
            if jpa.get('table'):
                tables.append(jpa['table'])
        return list(dict.fromkeys(tables))

    # ------------------------------------------------------------------ migrations

    def add_migrations(self) -> None:
        context = self.context
        for path, content in context.files():
            if not _is_migration(path):
                continue
            tables = parse_sql_migration(content) if path.lower().endswith('.sql') else parse_liquibase(content)
            version = re.match(r'^[Vv]([\d._]+)__', posixpath.basename(path))
            for table, columns in tables.items():
                table_id = context.add_resource('table', table, {'name': table, 'sources': []},
                                                file_path=path, start_line=1)
                node = context.graph.nodes[table_id]['data']
                if 'migration' not in node.attributes.setdefault('sources', []):
                    node.attributes['sources'].append('migration')
                migration_columns = node.attributes.setdefault('migration_columns', [])
                for column in columns:
                    if column not in migration_columns:
                        migration_columns.append(column)
                context.add_edge(path, table_id, 'migrates', version=version.group(1) if version else None)

    def compute_drift(self) -> None:
        for _node_id, data in self.context.graph.nodes(data=True):
            node = data.get('data')
            if getattr(node, 'node_type', '') != 'table':
                continue
            entity_columns = {c.lower() for c in node.attributes.get('columns') or []}
            migration_columns = {c.lower() for c in node.attributes.get('migration_columns') or []}
            if entity_columns and migration_columns:
                drift = {'only_in_entity': sorted(entity_columns - migration_columns),
                         'only_in_migrations': sorted(migration_columns - entity_columns)}
                if drift['only_in_entity'] or drift['only_in_migrations']:
                    node.attributes['drift'] = drift


def enrich(context: EnrichmentContext) -> None:
    JpaEnricher(context).run()
