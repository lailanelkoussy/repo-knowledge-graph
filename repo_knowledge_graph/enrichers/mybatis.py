"""
MyBatis: mapper statements and the tables they read or write.

Statements come from mapper XML files (`<mapper namespace="pkg.ArticleMapper">` with <select> / <insert> /
<update> / <delete>, `<include refid>` fragments expanded) and from annotations on mapper methods
(@Select, @Insert, @Update, @Delete). Each statement is attached to the mapper method `namespace::id`:

    attributes['mybatis'] = {statement, tables, sql, source}
    method -queries-> table   (attribute `operation`: select / insert / update / delete)

Mapper interfaces (@Mapper from MyBatis) are registered as beans by spring_beans, so the repositories
injecting them are linked, and trace_flow follows repository -> mapper method -> table.
"""
import logging
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

from .base import EnrichmentContext, annotation, annotation_value, as_list
from .spring_data_jpa import table_key

logger = logging.getLogger(__name__)

STATEMENTS = ('select', 'insert', 'update', 'delete')
STATEMENT_ANNOTATIONS = {'Select': 'select', 'Insert': 'insert', 'Update': 'update', 'Delete': 'delete',
                         'SelectProvider': 'select', 'InsertProvider': 'insert', 'UpdateProvider': 'update',
                         'DeleteProvider': 'delete'}
SQL_TABLES = re.compile(r'\b(?:from|join|into|update)\s+([`"\[]?[A-Za-z_][\w.]*[`"\]]?)', re.I)
SQL_KEYWORDS = {'select', 'set', 'where', 'values', 'on', 'as', 'lateral', 'unnest', 'dual'}


def applies(context: EnrichmentContext) -> bool:
    if any(_is_mapper_xml(content) for _, content in context.files('.xml')):
        return True
    for _, entity in context.java_declarations():
        names = {a.get('name') for a in (entity.get('attributes') or {}).get('annotations') or []}
        if names & set(STATEMENT_ANNOTATIONS):
            return True
    return False


def _is_mapper_xml(content: str) -> bool:
    return '<mapper' in content and 'namespace=' in content


def sql_tables(sql: str) -> List[str]:
    """Tables referenced by a SQL statement (FROM / JOIN / INTO / UPDATE), subqueries excluded."""
    tables = []
    for match in SQL_TABLES.finditer(sql or ''):
        name = match.group(1).strip('`"[]')
        if name.lower() in SQL_KEYWORDS or name.startswith('#') or name.startswith('$'):
            continue
        key = table_key(name)
        if key not in tables:
            tables.append(key)
    return tables


def _text_of(element: ET.Element, fragments: Dict[str, ET.Element], depth: int = 0) -> str:
    """SQL text of a statement: dynamic tags flattened, <include refid> expanded."""
    parts = [element.text or '']
    for child in element:
        tag = child.tag if isinstance(child.tag, str) else ''
        if tag == 'include' and depth < 5:
            fragment = fragments.get((child.get('refid') or '').rsplit('.', 1)[-1])
            if fragment is not None:
                parts.append(_text_of(fragment, fragments, depth + 1))
        elif tag not in ('selectKey',):
            parts.append(_text_of(child, fragments, depth + 1))
        parts.append(child.tail or '')
    return ' '.join(p.strip() for p in parts if p and p.strip())


def parse_mapper_xml(content: str) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """(namespace, [{id, statement, sql, line}]) of a MyBatis mapper XML file."""
    cleaned = re.sub(r'<!DOCTYPE[^>]*>', '', content)
    try:
        root = ET.fromstring(cleaned)
    except ET.ParseError as e:
        logger.warning(f'Cannot parse MyBatis mapper XML: {e}')
        return None, []
    if root.tag != 'mapper':
        return None, []
    fragments = {str(f.get('id')): f for f in root.findall('sql') if f.get('id')}
    statements = []
    for kind in STATEMENTS:
        for element in root.findall(kind):
            statement_id = element.get('id')
            if not statement_id:
                continue
            sql = re.sub(r'\s+', ' ', _text_of(element, fragments)).strip()
            line = content.count('\n', 0, max(content.find(f'id="{statement_id}"'), 0)) + 1
            statements.append({'id': statement_id, 'statement': kind, 'sql': sql, 'line': line})
    return root.get('namespace'), statements


def _link(context: EnrichmentContext, method: str, statement: str, sql: str, source: str, line: int = 0) -> None:
    tables = sql_tables(sql)
    context.framework_attributes(method, 'mybatis').update({
        'statement': statement, 'tables': tables, 'sql': sql[:400], 'source': source,
        **({'line': line} if line else {})})
    for table in tables:
        table_id = context.add_resource('table', table, {'name': table, 'sources': []})
        node = context.graph.nodes[table_id]['data']
        if 'mybatis' not in node.attributes.setdefault('sources', []):
            node.attributes['sources'].append('mybatis')
        context.add_edge(method, table_id, 'queries', operation=statement)


def enrich(context: EnrichmentContext) -> None:
    linked = 0
    unmatched: List[str] = []
    for path, content in context.files('.xml'):
        if not _is_mapper_xml(content):
            continue
        namespace, statements = parse_mapper_xml(content)
        if not namespace:
            continue
        for statement in statements:
            method = context.registry.lookup(f"{namespace}::{statement['id']}")
            if not method or not context.declared(method):
                unmatched.append(f"{namespace}.{statement['id']}")
                continue
            _link(context, method, statement['statement'], statement['sql'], path, statement['line'])
            linked += 1
        mapper = context.registry.lookup(namespace)
        if mapper and context.declared(mapper):
            context.set_framework_attribute(mapper, 'mybatis', 'xml', path)
    for path, entity in context.java_declarations():
        attributes = entity.get('attributes') or {}
        for name, kind in STATEMENT_ANNOTATIONS.items():
            item = annotation(attributes, name)
            if not item:
                continue
            method = context.registry.lookup(entity['name'])
            value = annotation_value(item, 'value')
            sql = ' '.join(str(v) for v in as_list(value) if isinstance(v, str))
            if method and sql:
                _link(context, method, kind, sql, path, int(entity.get('start_line') or 0))
                linked += 1
            break
    context.shared['mybatis'] = {'statements': linked, 'unmatched': unmatched}
    if unmatched:
        logger.debug(f'MyBatis statements without a mapper method: {unmatched[:10]}')
