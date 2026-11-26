"""
Java entity extraction with tree-sitter-java (Java 8 to 21: records, sealed types, text blocks,
switch expressions, pattern matching...).

Output contract (shared with the other extractors, see entity_extractor.BaseASTEntityExtractor):
`extract_entities(code) -> (declared_entities, called_entities)` and `get_imports()`.

Naming:
    top-level type   com.acme.OrderService
    nested type      com.acme.OrderService.Status
    member           com.acme.OrderService::find
    parameter        com.acme.OrderService::find.id

Every declared entity carries `start_line` / `end_line` and an `attributes` dict: annotations (with
their arguments), modifiers, javadoc summary, signature, resolved types, and for methods the calls
they make (`calls`), the invocations with their literal arguments (`invocations`) and the types they
reference (`uses_types`). Framework enrichers (enrichers/) build on these attributes.

Data flow (local, flow-insensitive): every invocation records a `site` id, the `shape` of each argument
(`arg_shapes`: literal, constant, param, field, local, concat, format, builder, call, new, expression) and
its `origins` (`param:x`, `field:x`, `call:<site>`, `lambda:x`, `unknown:x`) computed through the local
variables (def-use, `+=`, `StringBuilder.append`). Methods record where their return value comes from and
which fields they write (`attributes['dataflow']`). A chained call keeps a link to the call producing its
receiver (`receiver_site`), resolved after the registry is built (call_resolution.py).

Call names are type-resolved when the receiver type is known (field, parameter, local variable):
`orderService.find(id)` is emitted as `com.acme.OrderService::find` when OrderService is imported,
as `OrderService.find` when it is only known by its simple name (same package / wildcard import),
which both match the aliases of the declared method.
"""
import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from .entity_extractor import BaseASTEntityExtractor

logger = logging.getLogger(__name__)

_LANGUAGE = None

TYPE_DECLARATIONS = {
    'class_declaration': 'class',
    'interface_declaration': 'interface',
    'enum_declaration': 'enum',
    'record_declaration': 'record',
    'annotation_type_declaration': 'annotation',
}

PRIMITIVES = {'int', 'long', 'short', 'byte', 'char', 'boolean', 'float', 'double', 'void', 'var'}

JAVA_LANG = {
    'String', 'Object', 'Integer', 'Long', 'Short', 'Byte', 'Character', 'Boolean', 'Double', 'Float',
    'Number', 'Void', 'Math', 'System', 'Thread', 'StringBuilder', 'StringBuffer', 'CharSequence',
    'Iterable', 'Comparable', 'Runnable', 'Class', 'Enum', 'Record', 'Exception', 'RuntimeException',
    'Error', 'Throwable', 'IllegalArgumentException', 'IllegalStateException', 'NullPointerException',
    'UnsupportedOperationException', 'IndexOutOfBoundsException', 'ArithmeticException',
    'ClassCastException', 'InterruptedException', 'CloneNotSupportedException', 'AutoCloseable',
    'Override', 'Deprecated', 'FunctionalInterface', 'SuppressWarnings', 'SafeVarargs', 'Process',
    'ProcessBuilder', 'Runtime', 'ThreadLocal', 'StackTraceElement', 'Appendable', 'Readable',
}

MAPPING_ANNOTATIONS = {
    'GetMapping': 'GET', 'PostMapping': 'POST', 'PutMapping': 'PUT', 'PatchMapping': 'PATCH',
    'DeleteMapping': 'DELETE', 'RequestMapping': None,
}

MAX_INVOCATIONS = 400
MAX_ARGS = 6
STRING_FORMAT_MEMBERS = {'format', 'formatted', 'join', 'concat', 'valueOf'}
BUILDER_TYPES = {'StringBuilder', 'StringBuffer', 'StringJoiner'}
# Members that keep a constant receiver constant ("key".getBytes(), KEY.toCharArray())
# java.security.MessageDigest, javax.crypto.Cipher: a type written with its package (lowercase segments)
FULLY_QUALIFIED_TYPE = re.compile(r'^[a-z_]\w*(?:\.[a-z_]\w*)+\.[A-Z]\w*$')
CONSTANT_PRESERVING = {'getBytes', 'toCharArray', 'trim', 'strip', 'toUpperCase', 'toLowerCase', 'intern',
                       'substring', 'repeat', 'chars'}
LITERAL_TYPES = {'string_literal', 'decimal_integer_literal', 'hex_integer_literal', 'octal_integer_literal',
                 'binary_integer_literal', 'decimal_floating_point_literal', 'hex_floating_point_literal',
                 'true', 'false', 'null_literal', 'character_literal', 'class_literal'}
LOMBOK_GETTERS = {'Data', 'Getter', 'Value'}
LOMBOK_SETTERS = {'Data', 'Setter'}
_CONST_REF = re.compile(r'\{\{([^}]+)\}\}')


def _parser():
    global _LANGUAGE
    import tree_sitter_java
    from tree_sitter import Language, Parser
    if _LANGUAGE is None:
        _LANGUAGE = Language(tree_sitter_java.language())
    return Parser(_LANGUAGE)


def _text(node) -> str:
    return node.text.decode('utf-8', errors='replace') if node is not None else ''


def _line(node) -> int:
    return node.start_point[0] + 1


def _end_line(node) -> int:
    return node.end_point[0] + 1


def _string_value(node) -> Optional[str]:
    """Content of a string literal or text block, None for other nodes."""
    if node is None or node.type != 'string_literal':
        return None
    raw = _text(node)
    if raw.startswith('"""'):
        body = raw[3:-3]
        lines = body.split('\n')
        if lines and not lines[0].strip():
            lines = lines[1:]
        indent = min((len(ln) - len(ln.lstrip()) for ln in lines if ln.strip()), default=0)
        return '\n'.join(ln[indent:] for ln in lines).rstrip()
    return raw[1:-1] if len(raw) >= 2 else ''


def combine_paths(base: str, path: str) -> str:
    """Join a class-level and a method-level request path ('/api' + 'users' -> '/api/users')."""
    base = (base or '').strip()
    path = (path or '').strip()
    if not base:
        joined = path
    elif not path:
        joined = base
    else:
        joined = base.rstrip('/') + '/' + path.lstrip('/')
    if not joined.startswith('/') and not joined.startswith('{{') and not joined.startswith('${'):
        joined = '/' + joined
    return joined or '/'


def javadoc_summary(comment: str) -> str:
    """First sentence of a Javadoc comment, without tags."""
    body = comment.strip()
    if body.startswith('/**'):
        body = body[3:]
    if body.endswith('*/'):
        body = body[:-2]
    lines = []
    for line in body.split('\n'):
        line = line.strip().lstrip('*').strip()
        if line.startswith('@'):
            break
        lines.append(line)
    text = re.sub(r'<[^>]+>', '', ' '.join(lines)).strip()
    text = re.sub(r'\{@\w+\s+([^}]*)\}', r'\1', text)
    match = re.search(r'(.+?[.!?])(\s|$)', text)
    summary = match.group(1) if match else text
    return summary[:240]


class TreeSitterJavaEntityExtractor(BaseASTEntityExtractor):
    """Extract declared and called entities from Java code using tree-sitter-java."""

    def __init__(self):
        self.parser = _parser()
        self.reset()

    # ------------------------------------------------------------------ state

    def reset(self) -> None:
        self.declared_entities: List[Dict[str, Any]] = []
        self.called_entities: List[str] = []
        self.imports: List[str] = []
        self.api_endpoints: List[Dict[str, Any]] = []
        self.call_sites: List[Tuple[str, int]] = []  # (called name, line) for line-based chunk mapping
        self.current_package: Optional[str] = None
        self.import_map: Dict[str, str] = {}        # simple name -> fully qualified name
        self.static_imports: Dict[str, str] = {}    # member -> fully qualified class
        self.wildcard_imports: List[str] = []
        self.local_types: Dict[str, str] = {}       # types declared in this file: simple / dotted -> FQN
        self.type_members: Dict[str, Set[str]] = {}  # FQN -> method names declared in the type
        self.type_supers: Dict[str, List[str]] = {}  # FQN -> resolved supertypes
        self.constants: Dict[str, str] = {}         # 'NAME' and 'Type.NAME' -> string value (this file)
        self.syntax_errors = 0

    def get_imports(self) -> List[str]:
        return list(self.imports)

    def get_call_sites(self) -> List[Tuple[str, int]]:
        return list(self.call_sites)

    # ------------------------------------------------------------------ entry point

    def extract_entities(self, code: str, file_path: Optional[str] = None  # type: ignore[override]
                         ) -> Tuple[List[Dict[str, Any]], List[str]]:
        self.reset()
        try:
            tree = self.parser.parse(code.encode('utf-8', errors='replace'))
        except Exception as e:  # pragma: no cover - tree-sitter does not raise on syntax errors
            logger.error(f'Error parsing Java code {file_path or ""}: {e}', exc_info=True)
            return [], []
        root = tree.root_node
        if root.has_error:
            self.syntax_errors = sum(1 for _ in self._iter_errors(root))
            logger.debug(f'{self.syntax_errors} syntax error(s) in {file_path or "Java code"}, partial extraction')

        for child in root.named_children:
            if child.type == 'package_declaration':
                name = next((c for c in child.named_children if c.type in ('scoped_identifier', 'identifier')), None)
                self.current_package = _text(name) if name is not None else None
            elif child.type == 'import_declaration':
                self._visit_import(child)

        type_nodes = [c for c in root.named_children if c.type in TYPE_DECLARATIONS]
        # Pre-pass: names of every type (nested included) and their methods, constants and supertypes
        for node in type_nodes:
            self._collect_types(node, outer=None)
        for node in type_nodes:
            self._collect_type_details(node, outer=None)
        for node in type_nodes:
            self._visit_type(node, outer=None, base_paths=[''])

        unique_declared, seen = [], set()
        for entity in self.declared_entities:
            key = (entity.get('name'), entity.get('type'), entity.get('dtype'))
            if key not in seen:
                seen.add(key)
                unique_declared.append(entity)
        return unique_declared, list(dict.fromkeys(self.called_entities))

    @staticmethod
    def _iter_errors(node):
        stack = [node]
        while stack:
            current = stack.pop()
            if current.type == 'ERROR' or current.is_missing:
                yield current
            stack.extend(current.children)

    # ------------------------------------------------------------------ imports & types

    def _visit_import(self, node) -> None:
        is_static = any(c.type == 'static' for c in node.children)
        name_node = next((c for c in node.named_children if c.type in ('scoped_identifier', 'identifier')), None)
        if name_node is None:
            return
        path = _text(name_node)
        self.call_sites.append((path, _line(node)))
        wildcard = any(c.type == 'asterisk' for c in node.children)
        if wildcard:
            self.imports.append(path + '.*')
            if not is_static:
                self.wildcard_imports.append(path)
            self.called_entities.append(path)
            return
        self.imports.append(path)
        self.called_entities.append(path)
        simple = path.rsplit('.', 1)[-1]
        if is_static:
            self.static_imports[simple] = path.rsplit('.', 1)[0]
        else:
            self.import_map[simple] = path

    def _fqn(self, simple: str, outer: Optional[str]) -> str:
        if outer:
            return f'{outer}.{simple}'
        return f'{self.current_package}.{simple}' if self.current_package else simple

    def _collect_types(self, node, outer: Optional[str]) -> None:
        name = _text(node.child_by_field_name('name'))
        if not name:
            return
        fqn = self._fqn(name, outer)
        self.local_types.setdefault(name, fqn)
        if outer:
            outer_simple = outer.rsplit('.', 1)[-1]
            self.local_types.setdefault(f'{outer_simple}.{name}', fqn)
        self.type_members.setdefault(fqn, set())
        body = node.child_by_field_name('body')
        for member in self._body_members(body):
            if member.type in TYPE_DECLARATIONS:
                self._collect_types(member, outer=fqn)

    def _collect_type_details(self, node, outer: Optional[str]) -> None:
        fqn = self._fqn(_text(node.child_by_field_name('name')), outer)
        self.type_supers[fqn] = [s['name'] for s in self._supertypes(node)]
        body = node.child_by_field_name('body')
        for accessor in self._lombok_accessors(node):
            self.type_members[fqn].add(accessor['name'])
        for member in self._body_members(body):
            if member.type in ('method_declaration', 'annotation_type_element_declaration'):
                self.type_members[fqn].add(_text(member.child_by_field_name('name')))
            elif member.type in ('field_declaration', 'constant_declaration'):
                self._collect_constants(member, fqn)
            elif member.type in TYPE_DECLARATIONS:
                self._collect_type_details(member, outer=fqn)
        if node.type == 'record_declaration':
            for param in self._formal_parameters(node.child_by_field_name('parameters')):
                self.type_members[fqn].add(param['name'])

    def _collect_constants(self, field_node, owner: str) -> None:
        for declarator in self._children_of_type(field_node, 'variable_declarator'):
            value = _string_value(declarator.child_by_field_name('value'))
            if value is None:
                continue
            name = _text(declarator.child_by_field_name('name'))
            owner_simple = owner.rsplit('.', 1)[-1]
            self.constants.setdefault(name, value)
            self.constants[f'{owner_simple}.{name}'] = value
            self.constants[f'{owner}.{name}'] = value

    @staticmethod
    def _children_of_type(node, *types):
        return [c for c in node.named_children if c.type in types] if node is not None else []

    def _body_members(self, body) -> list:
        if body is None:
            return []
        members = []
        for child in body.named_children:
            if child.type == 'enum_body_declarations':
                members.extend(child.named_children)
            else:
                members.append(child)
        return members

    def resolve_type(self, type_text: str) -> Optional[str]:
        """Fully qualified name of a type when known, its simple name otherwise, None for primitives."""
        name = re.sub(r'<.*', '', type_text or '').replace('[]', '').replace('...', '').strip()
        name = re.sub(r'@\w+(\([^)]*\))?\s*', '', name).strip()
        if not name or name in PRIMITIVES or name == '?':
            return None
        if '.' in name:
            head, rest = name.split('.', 1)
            if head in self.local_types:
                return f'{self.local_types[head]}.{rest}'
            if head in self.import_map:
                return f'{self.import_map[head]}.{rest}'
            return name
        if name in self.local_types:
            return self.local_types[name]
        if name in self.import_map:
            return self.import_map[name]
        if name in JAVA_LANG:
            return f'java.lang.{name}'
        return name

    def _type_args(self, type_node) -> List[str]:
        """Resolved type arguments of a generic type node (List<OrderDto> -> ['...OrderDto'])."""
        result: List[str] = []
        if type_node is None:
            return result
        for child in type_node.named_children:
            if child.type == 'type_arguments':
                for arg in child.named_children:
                    resolved = self.resolve_type(_text(arg))
                    if resolved:
                        result.append(resolved)
        return result

    def _referenced_types(self, type_node, type_params: Set[str]) -> List[str]:
        """Every (non JDK, non primitive) type mentioned in a type node, generics included."""
        found: List[str] = []
        if type_node is None:
            return found
        stack = [type_node]
        while stack:
            node = stack.pop()
            if node.type in ('type_identifier', 'scoped_type_identifier'):
                name = _text(node)
                if name not in type_params:
                    resolved = self.resolve_type(name)
                    if resolved and not resolved.startswith('java.') and resolved not in found:
                        found.append(resolved)
                if node.type == 'scoped_type_identifier':
                    continue
            stack.extend(reversed(node.named_children))
        return found

    def _supertypes(self, node) -> List[Dict[str, Any]]:
        supers = []
        for field_name in ('superclass', 'interfaces'):
            child = node.child_by_field_name(field_name)
            if child is None:
                continue
            for type_node in self._type_list(child):
                supers.append({'name': self.resolve_type(_text(type_node)) or _text(type_node),
                               'type_args': self._type_args(type_node),
                               'relation': 'extends' if field_name == 'superclass' else 'implements'})
        if node.type == 'interface_declaration':
            for child in node.named_children:
                if child.type == 'extends_interfaces':
                    for type_node in self._type_list(child):
                        supers.append({'name': self.resolve_type(_text(type_node)) or _text(type_node),
                                       'type_args': self._type_args(type_node), 'relation': 'extends'})
        return supers

    @staticmethod
    def _type_list(node) -> list:
        result = []
        for child in node.named_children:
            if child.type == 'type_list':
                result.extend(child.named_children)
            elif child.type not in ('extends', 'implements'):
                result.append(child)
        return result

    # ------------------------------------------------------------------ annotations & modifiers

    def _modifiers(self, node) -> Tuple[List[Dict[str, Any]], List[str]]:
        modifiers_node = next((c for c in node.children if c.type == 'modifiers'), None)
        annotations: List[Dict[str, Any]] = []
        modifiers: List[str] = []
        if modifiers_node is None:
            return annotations, modifiers
        for child in modifiers_node.children:
            if child.type in ('marker_annotation', 'annotation'):
                annotations.append(self._annotation(child))
            elif child.type not in ('line_comment', 'block_comment'):
                modifiers.append(_text(child))
        return annotations, modifiers

    def _annotation(self, node) -> Dict[str, Any]:
        name = _text(node.child_by_field_name('name'))
        simple = name.rsplit('.', 1)[-1]
        args: Dict[str, Any] = {}
        arguments = node.child_by_field_name('arguments')
        if arguments is not None:
            for child in arguments.named_children:
                if child.type == 'element_value_pair':
                    key = _text(child.child_by_field_name('key'))
                    args[key] = self._value(child.child_by_field_name('value'))
                elif child.type not in ('line_comment', 'block_comment'):
                    args['value'] = self._value(child)
        qualified = name if '.' in name else self.import_map.get(name, name)
        return {'name': simple, 'qualified': qualified, 'args': args}

    def _value(self, node) -> Any:
        """Python representation of an annotation value / argument expression."""
        if node is None:
            return None
        string = _string_value(node)
        if string is not None:
            return string
        if node.type == 'element_value_array_initializer' or node.type == 'array_initializer':
            return [self._value(c) for c in node.named_children if c.type not in ('line_comment', 'block_comment')]
        if node.type in ('marker_annotation', 'annotation'):
            return {'annotation': self._annotation(node)}
        if node.type == 'binary_expression' and _text(node.child_by_field_name('operator')) == '+':
            left, right = self._value(node.child_by_field_name('left')), self._value(node.child_by_field_name('right'))
            if isinstance(left, str) and isinstance(right, str):
                return left + right
        if node.type in ('identifier', 'field_access', 'scoped_identifier'):
            reference = _text(node)
            if reference in self.constants:
                return self.constants[reference]
            return '{{' + reference + '}}'
        if node.type == 'parenthesized_expression' and node.named_children:
            return self._value(node.named_children[0])
        if node.type in ('true', 'false'):
            return node.type == 'true'
        if node.type in ('decimal_integer_literal', 'hex_integer_literal'):
            try:
                return int(_text(node).rstrip('lL'), 0)
            except ValueError:
                return _text(node)
        return _text(node)

    @staticmethod
    def _javadoc(node) -> Optional[str]:
        previous = node.prev_sibling
        while previous is not None and previous.type == 'line_comment':
            previous = previous.prev_sibling
        if previous is not None and previous.type == 'block_comment' and _text(previous).startswith('/**'):
            summary = javadoc_summary(_text(previous))
            return summary or None
        return None

    # ------------------------------------------------------------------ declarations

    def _declare(self, name: str, entity_type: str, node, attributes: Dict[str, Any], **extra) -> Dict[str, Any]:
        entity: Dict[str, Any] = {'name': name, 'type': entity_type, 'start_line': _line(node),
                                  'end_line': _end_line(node)}
        entity.update({k: v for k, v in extra.items() if v is not None})
        entity['attributes'] = {k: v for k, v in attributes.items() if v not in (None, [], {}, '')}
        self.declared_entities.append(entity)
        return entity

    def _visit_type(self, node, outer: Optional[str], base_paths: List[str]) -> None:
        simple = _text(node.child_by_field_name('name'))
        if not simple:
            return
        fqn = self._fqn(simple, outer)
        kind = TYPE_DECLARATIONS[node.type]
        annotations, modifiers = self._modifiers(node)
        supers = self._supertypes(node)
        type_params = self._type_parameters(node)
        class_paths = self._mapping_paths(annotations, 'RequestMapping')
        own_paths = class_paths if class_paths is not None else base_paths if outer else ['']

        permits = []
        permits_node = node.child_by_field_name('permits')
        if permits_node is not None:
            permits = [self.resolve_type(_text(t)) or _text(t) for t in self._type_list(permits_node)]

        extra_aliases = [simple]
        if outer:
            outer_simple = outer.rsplit('.', 1)[-1]
            extra_aliases.append(f'{outer_simple}.{simple}')

        attributes = {
            'kind': kind,
            'annotations': annotations,
            'modifiers': modifiers,
            'javadoc': self._javadoc(node),
            'type_params': sorted(type_params),
            'extends': [s for s in supers if s['relation'] == 'extends'],
            'implements': [s for s in supers if s['relation'] == 'implements'],
            'permits': permits,
            'outer': outer,
            'package': self.current_package,
            'uses_types': self._header_types(node, type_params),
        }
        self._declare(fqn, kind, node, attributes, bases=[s['name'] for s in supers],
                      extra_aliases=extra_aliases or None)
        for s in supers:
            self.called_entities.append(s['name'])
            self.call_sites.append((s['name'], _line(node)))

        fields: Dict[str, str] = {}
        body = node.child_by_field_name('body')
        members = self._body_members(body)
        # Record components are fields (and accessors) of the record
        if node.type == 'record_declaration':
            for param in self._formal_parameters(node.child_by_field_name('parameters')):
                fields[param['name']] = param['type']
                param_node = param.pop('_node')
                self._declare(f'{fqn}::{param["name"]}', 'variable', param_node,
                              {'annotations': param['annotations'], 'record_component': True,
                               'type_resolved': param['type_resolved'], 'type_args': param['type_args']},
                              dtype=param['type'], extra_aliases=self._member_aliases(fqn, param['name']))
        for member in members:
            if member.type in ('field_declaration', 'constant_declaration'):
                type_text = _text(member.child_by_field_name('type'))
                for declarator in self._children_of_type(member, 'variable_declarator'):
                    fields[_text(declarator.child_by_field_name('name'))] = type_text

        explicit = {_text(m.child_by_field_name('name')) for m in members if m.type == 'method_declaration'}
        for accessor in self._lombok_accessors(node):
            if accessor['name'] in explicit:
                continue
            params =[{'name': accessor['field'], 'type': accessor['param_type']}] if accessor.get('param_type') else []
            self._declare(f"{fqn}::{accessor['name']}", 'method', accessor['node'], {
                'synthetic': True, 'lombok': accessor['lombok'], 'return_type': accessor['return_type'],
                'return_type_resolved': self.resolve_type(accessor['return_type']) if accessor['return_type'] else None,
                'params': params, 'modifiers': ['public'] + (['static'] if accessor.get('static') else []),
                'signature': f"{accessor['name']}({', '.join(p['type'] for p in params)})",
            }, extra_aliases=self._member_aliases(fqn, accessor['name']))

        for member in members:
            if member.type in TYPE_DECLARATIONS:
                self._visit_type(member, outer=fqn, base_paths=own_paths)
            elif member.type in ('field_declaration', 'constant_declaration'):
                self._visit_field(member, fqn, type_params)
            elif member.type == 'enum_constant':
                name = _text(member.child_by_field_name('name'))
                annotations_c, _ = self._modifiers(member)
                self._declare(f'{fqn}::{name}', 'enum_constant', member, {'annotations': annotations_c},
                              dtype=simple, extra_aliases=self._member_aliases(fqn, name))
                enum_body = member.child_by_field_name('body')
                if enum_body is not None:
                    self._walk_body(enum_body, self._new_context(fqn, fields, {}))
            elif member.type in ('method_declaration', 'constructor_declaration', 'compact_constructor_declaration'):
                self._visit_method(member, fqn, simple, fields, type_params, own_paths, kind)
            elif member.type == 'annotation_type_element_declaration':
                name = _text(member.child_by_field_name('name'))
                self._declare(f'{fqn}::{name}', 'method', member,
                              {'return_type': _text(member.child_by_field_name('type'))},
                              extra_aliases=self._member_aliases(fqn, name))
            elif member.type in ('static_initializer', 'block'):
                self._walk_body(member, self._new_context(fqn, fields, {}))

    def _lombok_accessors(self, node) -> List[Dict[str, Any]]:
        """Accessors generated by Lombok (@Data, @Getter, @Setter, @Value, @Builder) for a type declaration."""
        if node.type not in ('class_declaration', 'enum_declaration'):
            return []
        class_annotations = {a['name'] for a in self._modifiers(node)[0]}
        accessors: List[Dict[str, Any]] = []
        if 'Builder' in class_annotations or 'SuperBuilder' in class_annotations:
            simple = _text(node.child_by_field_name('name'))
            accessors.append({'name': 'builder', 'lombok': 'Builder', 'node': node,
                              'return_type': f'{simple}.{simple}Builder', 'static': True})
        body = node.child_by_field_name('body')
        for member in self._body_members(body):
            if member.type != 'field_declaration':
                continue
            field_annotations, modifiers = self._modifiers(member)
            if 'static' in modifiers:
                continue
            names = {a['name'] for a in field_annotations}
            type_text = _text(member.child_by_field_name('type'))
            getter = bool((class_annotations | names) & LOMBOK_GETTERS)
            setter = bool((class_annotations | names) & LOMBOK_SETTERS) and 'final' not in modifiers \
                and 'Value' not in class_annotations
            for declarator in self._children_of_type(member, 'variable_declarator'):
                field = _text(declarator.child_by_field_name('name'))
                capitalized = field[:1].upper() + field[1:]
                if getter:
                    prefix = 'is' if type_text == 'boolean' else 'get'
                    accessors.append({'name': prefix + capitalized, 'lombok': 'Getter', 'node': member,
                                      'return_type': type_text, 'field': field})
                if setter:
                    accessors.append({'name': 'set' + capitalized, 'lombok': 'Setter', 'node': member,
                                      'return_type': 'void', 'field': field, 'param_type': type_text})
        return accessors

    def _type_parameters(self, node) -> Set[str]:
        params = node.child_by_field_name('type_parameters')
        names: Set[str] = set()
        if params is None:
            return names
        for param in params.named_children:
            identifier = next((c for c in param.named_children if c.type in ('type_identifier', 'identifier')), None)
            if identifier is not None:
                names.add(_text(identifier))
        return names

    def _header_types(self, node, type_params: Set[str]) -> List[str]:
        types: List[str] = []
        for field_name in ('superclass', 'interfaces'):
            for t in self._referenced_types(node.child_by_field_name(field_name), type_params):
                if t not in types:
                    types.append(t)
        return types

    @staticmethod
    def _member_aliases(owner_fqn: str, member: str) -> List[str]:
        owner_simple = owner_fqn.rsplit('.', 1)[-1]
        aliases = [f'{owner_simple}.{member}', f'{owner_simple}::{member}', f'{owner_fqn}.{member}']
        parts = owner_fqn.split('.')
        # Nested types: Outer.Inner.member
        for i in range(len(parts) - 2, 0, -1):
            if parts[i][:1].isupper():
                aliases.append('.'.join(parts[i:]) + f'.{member}')
            else:
                break
        return list(dict.fromkeys(aliases))

    def _visit_field(self, node, owner: str, type_params: Set[str]) -> None:
        annotations, modifiers = self._modifiers(node)
        type_node = node.child_by_field_name('type')
        type_text = _text(type_node)
        javadoc = self._javadoc(node)
        for declarator in self._children_of_type(node, 'variable_declarator'):
            name = _text(declarator.child_by_field_name('name'))
            value_node = declarator.child_by_field_name('value')
            attributes = {
                'annotations': annotations,
                'modifiers': modifiers,
                'javadoc': javadoc,
                'type_resolved': self.resolve_type(type_text),
                'type_args': self._type_args(type_node),
                'uses_types': self._referenced_types(type_node, type_params),
                'constant_value': _string_value(value_node),
            }
            if value_node is not None:
                context = self._new_context(owner, {}, {})
                self._walk_body(value_node, context)
                if context['calls']:
                    attributes['calls'] = context['calls']
                if context['invocations']:
                    attributes['invocations'] = context['invocations']
            self._declare(f'{owner}::{name}', 'variable', declarator, attributes, dtype=type_text,
                          extra_aliases=self._member_aliases(owner, name))

    def _formal_parameters(self, params_node) -> List[Dict[str, Any]]:
        params: List[Dict[str, Any]] = []
        if params_node is None:
            return params
        for param in params_node.named_children:
            if param.type not in ('formal_parameter', 'spread_parameter', 'receiver_parameter'):
                continue
            annotations, _ = self._modifiers(param)
            type_node = param.child_by_field_name('type')
            if type_node is None:
                type_node = next((c for c in param.named_children
                                  if c.type not in ('modifiers', 'variable_declarator', 'identifier')), None)
            name_node = param.child_by_field_name('name')
            if name_node is None:
                declarator = next((c for c in param.named_children if c.type == 'variable_declarator'), None)
                name_node = declarator.child_by_field_name('name') if declarator is not None else None
            type_text = _text(type_node)
            if param.type == 'spread_parameter':
                type_text += '...'
            params.append({
                'name': _text(name_node),
                'type': type_text,
                'type_resolved': self.resolve_type(type_text),
                'type_args': self._type_args(type_node),
                'annotations': annotations,
                '_node': param,
                '_type_node': type_node,
            })
        return params

    def _visit_method(self, node, owner: str, owner_simple: str, fields: Dict[str, str],
                      class_type_params: Set[str], class_paths: List[str], owner_kind: str) -> None:
        is_constructor = node.type in ('constructor_declaration', 'compact_constructor_declaration')
        name = owner_simple if is_constructor else _text(node.child_by_field_name('name'))
        qualified = f'{owner}::{name}'
        annotations, modifiers = self._modifiers(node)
        type_params = class_type_params | self._type_parameters(node)
        params = self._formal_parameters(node.child_by_field_name('parameters'))
        return_node = node.child_by_field_name('type')
        return_type = _text(return_node) if return_node is not None else None

        uses_types: List[str] = []
        for t in self._referenced_types(return_node, type_params):
            uses_types.append(t)
        for param in params:
            for t in self._referenced_types(param['_type_node'], type_params):
                if t not in uses_types:
                    uses_types.append(t)

        throws = []
        for child in node.named_children:
            if child.type == 'throws':
                throws = [self.resolve_type(_text(t)) or _text(t) for t in child.named_children]

        variables = {p['name']: p['type'] for p in params}
        context = self._new_context(owner, fields, variables, type_params)
        context['params'] = {p['name'] for p in params}
        body = node.child_by_field_name('body')
        if body is not None:
            self._walk_body(body, context)
        dataflow: Dict[str, Any] = {}
        if context['returns']:
            dataflow['returns'] = sorted(context['returns'])
        if context['field_writes']:
            dataflow['field_writes'] = {k: sorted(v) for k, v in sorted(context['field_writes'].items())}
        if [s for s in context['return_shapes'] if not s.get('constant')]:
            dataflow['return_shapes'] = context['return_shapes']
        for t in context['types']:
            if t not in uses_types:
                uses_types.append(t)

        endpoint = self._endpoint(annotations, class_paths)
        entity_type = 'constructor' if is_constructor else ('api_endpoint' if endpoint else 'method')
        signature = f"{name}({', '.join(p['type'] for p in params)})"
        attributes = {
            'annotations': annotations,
            'modifiers': modifiers,
            'javadoc': self._javadoc(node),
            'signature': signature,
            'return_type': return_type,
            'return_type_resolved': self.resolve_type(return_type) if return_type else None,
            'return_type_args': self._type_args(return_node),
            'params': [{k: v for k, v in p.items() if not k.startswith('_')} for p in params],
            'throws': throws,
            'type_params': sorted(self._type_parameters(node)),
            'abstract': body is None and owner_kind != 'record',
            'calls': context['calls'],
            'invocations': context['invocations'],
            'uses_types': uses_types,
            'dataflow': dataflow,
        }
        extra = {}
        if endpoint:
            attributes['endpoints'] = endpoint['endpoints']
            extra = {'endpoint': endpoint['endpoint'], 'methods': endpoint['methods']}
            self.api_endpoints.append({'endpoint': endpoint['endpoint'], 'methods': endpoint['methods'],
                                       'type': 'api_endpoint_definition', 'function': qualified})
        if is_constructor:
            extra['exclude_aliases'] = [name]  # the simple name belongs to the class
        self._declare(qualified, entity_type, node, attributes,
                      extra_aliases=self._member_aliases(owner, name), **extra)
        for param in params:
            self._declare(f'{qualified}.{param["name"]}', 'variable', param['_node'],
                          {'annotations': param['annotations'], 'parameter': True,
                           'type_resolved': param['type_resolved']}, dtype=param['type'])

    # ------------------------------------------------------------------ REST mappings

    def _mapping_paths(self, annotations: List[Dict[str, Any]], only: Optional[str] = None) -> Optional[List[str]]:
        for annotation in annotations:
            if annotation['name'] not in MAPPING_ANNOTATIONS or (only and annotation['name'] != only):
                continue
            args = annotation['args']
            value = args.get('value', args.get('path'))
            if value is None:
                return ['']
            values = value if isinstance(value, list) else [value]
            return [v if isinstance(v, str) else str(v) for v in values] or ['']
        return None

    def _endpoint(self, annotations: List[Dict[str, Any]], class_paths: List[str]) -> Optional[Dict[str, Any]]:
        for annotation in annotations:
            if annotation['name'] not in MAPPING_ANNOTATIONS:
                continue
            http_method = MAPPING_ANNOTATIONS[annotation['name']]
            if http_method:
                methods = [http_method]
            else:
                declared = annotation['args'].get('method')
                values = declared if isinstance(declared, list) else [declared] if declared else []
                methods = [str(v).strip('{}').rsplit('.', 1)[-1].upper() for v in values] or ['ANY']
            paths = self._mapping_paths([annotation]) or ['']
            full_paths = [combine_paths(base, path) for base in (class_paths or ['']) for path in paths]
            return {'endpoint': full_paths[0], 'methods': methods,
                    'endpoints': [{'path': p, 'methods': methods} for p in full_paths]}
        return None

    # ------------------------------------------------------------------ method bodies

    def _new_context(self, owner: str, fields: Dict[str, str], variables: Dict[str, str],
                     type_params: Optional[Set[str]] = None) -> Dict[str, Any]:
        return {'owner': owner, 'fields': fields, 'vars': dict(variables), 'calls': [],
                'invocations': [], 'types': [], 'type_params': type_params or set(),
                'params': set(), 'defs': {}, 'const_vars': set(), 'var_site': {}, 'var_shape': {},
                'returns': set(), 'field_writes': {}, 'return_shapes': []}

    # ------------------------------------------------------------------ data flow

    @staticmethod
    def site_of(node) -> str:
        """Stable id of a call site: position of the invoked member name ('line:column')."""
        anchor = node.child_by_field_name('name') or node
        return f'{anchor.start_point[0] + 1}:{anchor.start_point[1] + 1}'

    def origins(self, node, context: Dict[str, Any]) -> Set[str]:
        """Where the value of an expression comes from: parameters, fields, call results, lambda parameters."""
        if node is None or node.type in LITERAL_TYPES or node.type == 'lambda_expression':
            return set()
        kind = node.type
        if kind == 'identifier':
            name = _text(node)
            if name in context['defs']:
                return set(context['defs'][name])
            if name in context['params']:
                return {f'param:{name}'}
            if name in context['fields']:
                return {f'field:{name}'}
            if name in self.constants or name[:1].isupper():
                return set()
            return {f'unknown:{name}'}
        if kind == 'field_access':
            obj = node.child_by_field_name('object')
            field = _text(node.child_by_field_name('field'))
            if obj is not None and obj.type == 'this':
                return {f'field:{field}'}
            if obj is not None and obj.type == 'identifier' and _text(obj)[:1].isupper():
                return set()  # Type.CONSTANT
            return self.origins(obj, context)
        if kind == 'method_invocation':
            found = {f'call:{self.site_of(node)}'}
            inner = self.origins(node.child_by_field_name('object'), context)
            arguments = node.child_by_field_name('arguments')
            for arg in (arguments.named_children if arguments is not None else []):
                inner |= self.origins(arg, context)
            member = _text(node.child_by_field_name('name'))
            if self._is_sanitizer(node, member, context):
                # Long.parseLong(id), HtmlUtils.htmlEscape(s): the value went through a sanitizer
                return found | {f'via:{member}:{o}' for o in inner if not o.startswith(('call:', 'via:'))} | \
                    {o for o in inner if o.startswith('via:')}
            return found | inner
        if kind == 'object_creation_expression':
            arguments = node.child_by_field_name('arguments')
            collected_args: Set[str] = set()
            for arg in (arguments.named_children if arguments is not None else []):
                collected_args |= self.origins(arg, context)
            return collected_args
        collected: Set[str] = set()
        for child in node.named_children:
            collected |= self.origins(child, context)
        return collected

    def _is_sanitizer(self, node, member: str, context: Dict[str, Any]) -> bool:
        from .security_catalog import load_catalog
        catalog = load_catalog()
        if member not in catalog.sanitizer_members:
            return False
        obj = node.child_by_field_name('object')
        receiver = None
        if obj is not None and obj.type == 'identifier':
            name = _text(obj)
            receiver = self._variable_type(name, context) or name
        elif obj is not None and obj.type == 'method_invocation':
            # Paths.get(name).getFileName(): the receiver type comes from the library return table
            from .call_resolution import FLUENT, LIBRARY_RETURNS, simple
            inner_type, _ = self._receiver_type(obj.child_by_field_name('object'), context)
            rule = LIBRARY_RETURNS.get((simple(inner_type), _text(obj.child_by_field_name('name'))))
            receiver = inner_type if rule == FLUENT else rule if isinstance(rule, str) else _text(obj)
        elif obj is not None:
            receiver = _text(obj)
        return catalog.sanitizer(receiver, member) is not None

    def _concat_parts(self, node) -> list:
        if node.type == 'binary_expression' and _text(node.child_by_field_name('operator')) == '+':
            return self._concat_parts(node.child_by_field_name('left')) + \
                self._concat_parts(node.child_by_field_name('right'))
        if node.type == 'parenthesized_expression' and node.named_children:
            return self._concat_parts(node.named_children[0])
        return [node]

    def shape(self, node, context: Dict[str, Any], depth: int = 0) -> Dict[str, Any]:
        """Structured description of an argument expression (see module docstring)."""
        origins = sorted(self.origins(node, context))
        result: Dict[str, Any] = {'origins': origins}
        kind = node.type
        string = _string_value(node)
        if string is not None:
            result.update(shape='literal', constant=True, value=string[:200])
        elif kind in LITERAL_TYPES:
            result.update(shape='literal', constant=True, value=_text(node)[:60])
        elif kind == 'identifier':
            name = _text(node)
            if name in context['defs']:
                constant = name in context['const_vars'] and not origins
                result.update(shape='literal' if constant else 'local', constant=constant, name=name)
                if not constant:
                    declared = context['vars'].get(name)
                    if declared and declared != 'var':
                        result['type'] = declared
                    if name in context['var_site']:
                        result['site'] = context['var_site'][name]  # the call that initialised it
                if context['var_shape'].get(name) and not constant:
                    result['built_by'] = context['var_shape'][name]  # e.g. a concatenation assigned earlier
            elif name in context['params']:
                result.update(shape='param', constant=False, name=name)
            elif name in context['fields']:
                result.update(shape='field', constant=False, name=name)
            elif name in self.constants or name[:1].isupper():
                result.update(shape='constant', constant=True, name=name)
                if name in self.constants:
                    result['value'] = self.constants[name][:200]
            else:
                result.update(shape='expression', constant=False, name=name)
        elif kind in ('field_access', 'scoped_identifier'):
            text = _text(node)
            obj = node.child_by_field_name('object')
            if obj is not None and obj.type == 'this':
                result.update(shape='field', constant=False, name=_text(node.child_by_field_name('field')))
            elif text in self.constants or text.split('.')[0][:1].isupper():
                result.update(shape='constant', constant=True, name=text)
                if text in self.constants:
                    result['value'] = self.constants[text][:200]
            else:
                result.update(shape='expression', constant=not origins, text=text[:160])
        elif kind == 'binary_expression' and _text(node.child_by_field_name('operator')) == '+':
            parts = self._concat_parts(node)
            if any(_string_value(p) is not None for p in parts):
                described = [self.shape(p, context, depth + 1) if depth < 2 else {'shape': 'expression'}
                             for p in parts[:8]]
                result.update(shape='concat', constant=all(d.get('constant') for d in described),
                              parts=[{k: v for k, v in d.items() if k in ('shape', 'name', 'value', 'origins', 'site', 'member', 'type')}
                                     for d in described])
            else:
                result.update(shape='expression', constant=not origins, text=_text(node)[:160])
        elif kind == 'method_invocation':
            member = _text(node.child_by_field_name('name'))
            obj = node.child_by_field_name('object')
            receiver = _text(obj) if obj is not None else ''
            site = self.site_of(node)
            arguments = node.child_by_field_name('arguments')
            args = arguments.named_children if arguments is not None else []
            obj_shape = self.shape(obj, context, depth + 1) if obj is not None and depth < 3 else {}
            if member in CONSTANT_PRESERVING and obj_shape.get('constant') and \
                    all(self.shape(a, context, depth + 1).get('constant') for a in args):
                # "secret".getBytes(), KEY.toCharArray(): still a constant
                result.update(shape='literal', constant=True, member=member, site=site)
                if obj_shape.get('value') is not None:
                    result['value'] = obj_shape['value']
            elif member in STRING_FORMAT_MEMBERS and (receiver in ('String', 'java.lang.String')
                                                    or member == 'formatted' or _string_value(obj) is not None):
                constant = all(self.shape(a, context, depth + 1).get('constant') for a in args) and \
                    (obj is None or receiver in ('String', 'java.lang.String') or _string_value(obj) is not None)
                result.update(shape='format', constant=constant, member=member, site=site)
            elif member == 'toString' and obj is not None and obj.type == 'identifier' and \
                    (context['vars'].get(receiver) or '').split('<')[0] in BUILDER_TYPES:
                result.update(shape='builder', constant=not (origins and any(not o.startswith('call:')
                                                                          for o in origins)), name=receiver, site=site)
            else:
                result.update(shape='call', constant=False, member=member, site=site)
        elif kind == 'object_creation_expression':
            result.update(shape='new', constant=not [o for o in origins if not o.startswith('call:')],
                          type=self.resolve_type(_text(node.child_by_field_name('type'))))
        elif kind == 'parenthesized_expression' and node.named_children:
            return self.shape(node.named_children[0], context, depth)
        elif kind in ('lambda_expression', 'method_reference'):
            result.update(shape='lambda', constant=False, text=_text(node)[:160])
        else:
            result.update(shape='expression', constant=not origins, text=_text(node)[:160])
        if not result['origins']:
            del result['origins']
        return result

    def _define(self, name: str, value, context: Dict[str, Any], append: bool = False) -> None:
        """Record a definition of a local variable (flow-insensitive: definitions are united).

        `append` is kept for readability at call sites (`+=`, `sb.append(x)`): every definition is a union.
        """
        found = self.origins(value, context) if value is not None else set()
        previous = context['defs'].get(name)
        context['defs'][name] = (previous or set()) | found
        constant = value is not None and not found and bool(self.shape(value, context).get('constant'))
        if constant and (previous is None or name in context['const_vars']):
            context['const_vars'].add(name)
        else:
            context['const_vars'].discard(name)
        if value is not None and value.type == 'method_invocation':
            context['var_site'][name] = self.site_of(value)
        if value is not None:
            built = self.shape(value, context).get('shape')
            if built in ('concat', 'format', 'builder'):
                context['var_shape'][name] = built
            elif built == 'local' and context['var_shape'].get(_text(value)):
                context['var_shape'][name] = context['var_shape'][_text(value)]

    def _record_call(self, name: Optional[str], node, context: Dict[str, Any]) -> None:
        if not name:
            return
        if name not in context['calls']:
            context['calls'].append(name)
        self.called_entities.append(name)
        self.call_sites.append((name, _line(node)))

    def _record_type(self, type_text: str, context: Dict[str, Any]) -> Optional[str]:
        resolved = self.resolve_type(type_text)
        if resolved and not resolved.startswith('java.') and type_text not in context['type_params'] \
                and resolved not in context['types']:
            context['types'].append(resolved)
        return resolved

    def _variable_type(self, name: str, context: Dict[str, Any]) -> Optional[str]:
        if name in context['vars']:
            return context['vars'][name]
        if name in context['fields']:
            return context['fields'][name]
        return None

    def member_call_name(self, type_name: Optional[str], member: str) -> Optional[str]:
        """Name emitted for a call of `member` on an object of type `type_name`."""
        if not type_name or type_name.startswith('java.'):
            return None
        if type_name in self.type_members or '.' in type_name and type_name.split('.')[0].islower():
            return f'{type_name}::{member}'
        return f'{type_name}.{member}'

    def _receiver_type(self, obj, context: Dict[str, Any]) -> Tuple[Optional[str], bool]:
        """(resolved type, known) of a method-call receiver expression."""
        if obj is None or obj.type == 'this':
            return context['owner'], True
        if obj.type == 'super':
            supers = self.type_supers.get(context['owner']) or []
            return (supers[0] if supers else None), True
        if obj.type == 'identifier':
            name = _text(obj)
            declared = self._variable_type(name, context)
            if declared is not None:
                if declared == 'var':
                    return None, False
                return self.resolve_type(declared), True
            if name[:1].isupper():  # static call: Type.method()
                return self.resolve_type(name), True
            return None, False
        if obj.type == 'object_creation_expression':
            return self.resolve_type(_text(obj.child_by_field_name('type'))), True
        if obj.type == 'parenthesized_expression' and obj.named_children:
            return self._receiver_type(obj.named_children[0], context)
        if obj.type == 'field_access':
            inner = obj.child_by_field_name('object')
            field_name = _text(obj.child_by_field_name('field'))
            if inner is not None and inner.type == 'this' and field_name in context['fields']:
                return self.resolve_type(context['fields'][field_name]), True
            text = _text(obj)
            if text[:1].isupper() or text.split('.')[0] in self.import_map:
                return self.resolve_type(text), True  # nested type or qualified static call
            if FULLY_QUALIFIED_TYPE.match(text) and text.split('.')[0] not in context['fields'] \
                    and self._variable_type(text.split('.')[0], context) is None:
                return text, True  # javax.crypto.Cipher.getInstance(...): static call on a fully qualified type
        return None, False

    def _invocation_args(self, arguments, context: Dict[str, Any]) -> List[Any]:
        args: List[Any] = []
        if arguments is None:
            return args
        for arg in arguments.named_children[:4]:
            string = _string_value(arg)
            if string is not None:
                args.append(string)
            elif arg.type == 'object_creation_expression':
                args.append({'new': self.resolve_type(_text(arg.child_by_field_name('type')))})
            elif arg.type == 'identifier':
                declared = self._variable_type(_text(arg), context)
                if declared:
                    args.append({'var': _text(arg), 'type': self.resolve_type(declared)})
                else:
                    args.append({'ref': self.constants.get(_text(arg), '{{' + _text(arg) + '}}')})
            elif arg.type in ('field_access', 'scoped_identifier'):
                ref = _text(arg)
                args.append({'ref': self.constants.get(ref, '{{' + ref + '}}')})
            elif arg.type == 'class_literal':
                args.append({'class': self.resolve_type(_text(arg)[:-len('.class')])})
            elif arg.type == 'binary_expression':
                value = self._value(arg)
                args.append(value if isinstance(value, str) else {'expr': _text(arg)[:240]})
            else:
                args.append({'expr': _text(arg)[:240]})
        return args

    def _visit_invocation(self, node, context: Dict[str, Any]) -> None:
        member = _text(node.child_by_field_name('name'))
        obj = node.child_by_field_name('object')
        receiver, known = self._receiver_type(obj, context)
        name: Optional[str]
        if obj is None or obj.type == 'this':
            name = self._unqualified_call(member, context['owner'], this=obj is not None)
            receiver = name.split('::', 1)[0] if '::' in name else receiver
        elif known:
            name = self.member_call_name(receiver, member)
        elif obj.type == 'identifier':
            name = f'{_text(obj)}.{member}'  # unknown variable: keep the raw form
        else:
            name = None  # chained call on an expression of unknown type
        self._record_call(name, node, context)
        arguments = node.child_by_field_name('arguments')
        # StringBuilder sb; sb.append(x): the builder's content now depends on x
        if member in ('append', 'insert', 'add') and obj is not None and obj.type == 'identifier' \
                and (context['vars'].get(_text(obj)) or '').split('<')[0] in BUILDER_TYPES and arguments is not None:
            for arg in arguments.named_children:
                self._define(_text(obj), arg, context, append=True)
        if len(context['invocations']) < MAX_INVOCATIONS:
            anchor = node.child_by_field_name('name') or node
            invocation = {'member': member, 'line': anchor.start_point[0] + 1, 'column': anchor.start_point[1] + 1,
                          'site': self.site_of(node)}
            if name:
                invocation['name'] = name
            if receiver:
                invocation['receiver_type'] = receiver
            if obj is not None and obj.type in ('identifier', 'field_access'):
                invocation['receiver'] = _text(obj)
            if obj is not None and obj.type == 'method_invocation':
                invocation['receiver_site'] = self.site_of(obj)
            elif obj is not None and obj.type == 'identifier' and not known and _text(obj) in context['var_site']:
                invocation['receiver_site'] = context['var_site'][_text(obj)]
            receiver_origins = sorted(self.origins(obj, context)) if obj is not None and obj.type != 'this' else []
            if receiver_origins:
                invocation['receiver_origins'] = receiver_origins
            args = self._invocation_args(arguments, context)
            if args:
                invocation['args'] = args
            if arguments is not None and arguments.named_children:
                invocation['arg_shapes'] = [self.shape(a, context) for a in arguments.named_children[:MAX_ARGS]]
                invocation['arg_count'] = len(arguments.named_children)
            context['invocations'].append(invocation)

    def _record_constructor(self, node, type_name: str, context: Dict[str, Any]) -> None:
        """`new X(args)` as an invocation of member '<init>' (constructor sinks: new File(path), new URL(u)...)."""
        if len(context['invocations']) >= MAX_INVOCATIONS:
            return
        type_node = node.child_by_field_name('type')
        anchor = type_node if type_node is not None else node
        invocation: Dict[str, Any] = {'member': '<init>', 'line': anchor.start_point[0] + 1,
                                      'column': anchor.start_point[1] + 1,
                                      'site': f'{anchor.start_point[0] + 1}:{anchor.start_point[1] + 1}',
                                      'receiver_type': type_name.split('<')[0]}
        arguments = node.child_by_field_name('arguments')
        if arguments is not None and arguments.named_children:
            invocation['arg_shapes'] = [self.shape(a, context) for a in arguments.named_children[:MAX_ARGS]]
            invocation['arg_count'] = len(arguments.named_children)
        context['invocations'].append(invocation)

    def _unqualified_call(self, member: str, owner: str, this: bool = False) -> str:
        """Name emitted for `member(...)` / `this.member(...)` called inside `owner`."""
        current: Optional[str] = owner
        while current:
            if member in self.type_members.get(current, set()):
                return f'{current}::{member}'
            if this:
                break
            parent = current.rsplit('.', 1)[0] if '.' in current else None
            current = parent if parent in self.type_members else None  # enclosing type of a nested type
        if not this and member in self.static_imports:
            return f'{self.static_imports[member]}::{member}'
        supers = [s for s in self.type_supers.get(owner) or [] if not s.startswith('java.')]
        if supers:
            return self.member_call_name(supers[0], member) or member
        static_wildcards = [i[:-2] for i in self.imports if i.endswith('.*') and i[:-2] not in self.wildcard_imports]
        if not this and len(static_wildcards) == 1:
            return f'{static_wildcards[0]}::{member}'
        return member

    def _visit_method_reference(self, node, context: Dict[str, Any]) -> None:
        children = node.named_children
        if not children:
            return
        target = children[0]
        member = _text(children[-1]) if len(children) > 1 else 'new'
        if target.type in ('type_identifier', 'scoped_type_identifier', 'generic_type'):
            receiver, known = self.resolve_type(_text(target)), True
        else:
            receiver, known = self._receiver_type(target, context)
        if member == 'new':
            if receiver:
                self._record_call(receiver, node, context)
            return
        if known:
            self._record_call(self.member_call_name(receiver, member), node, context)

    def _walk_body(self, root, context: Dict[str, Any]) -> None:
        stack = [root]
        while stack:
            node = stack.pop()
            node_type = node.type
            if node_type == 'local_variable_declaration':
                type_node = node.child_by_field_name('type')
                type_text = _text(type_node)
                for declarator in self._children_of_type(node, 'variable_declarator'):
                    var_name = _text(declarator.child_by_field_name('name'))
                    value = declarator.child_by_field_name('value')
                    if type_text == 'var' and value is not None and value.type == 'object_creation_expression':
                        context['vars'][var_name] = _text(value.child_by_field_name('type'))
                    else:
                        context['vars'][var_name] = type_text
                    self._define(var_name, value, context)
                if type_text != 'var':
                    for t in self._referenced_types(type_node, context['type_params']):
                        if t not in context['types']:
                            context['types'].append(t)
            elif node_type in ('enhanced_for_statement', 'catch_formal_parameter'):
                type_node = node.child_by_field_name('type')
                if type_node is None and node_type == 'catch_formal_parameter':
                    type_node = next((c for c in node.named_children if c.type == 'catch_type'), None)
                name_node = node.child_by_field_name('name')
                if type_node is not None and name_node is not None:
                    context['vars'][_text(name_node)] = _text(type_node).split('|')[0].strip()
                    if node_type == 'enhanced_for_statement':
                        self._define(_text(name_node), node.child_by_field_name('value'), context)
            elif node_type == 'formal_parameter':  # typed lambda parameters
                type_node = node.child_by_field_name('type')
                name_node = node.child_by_field_name('name')
                if type_node is not None and name_node is not None:
                    context['vars'][_text(name_node)] = _text(type_node)
                    context['defs'].setdefault(_text(name_node), {f'lambda:{_text(name_node)}'})
            elif node_type == 'lambda_expression':
                parameters = node.child_by_field_name('parameters')
                names = []
                if parameters is not None and parameters.type == 'identifier':
                    names = [_text(parameters)]
                elif parameters is not None and parameters.type == 'inferred_parameters':
                    names = [_text(c) for c in parameters.named_children if c.type == 'identifier']
                for lambda_param in names:
                    context['defs'].setdefault(lambda_param, {f'lambda:{lambda_param}'})
            elif node_type == 'assignment_expression':
                left = node.child_by_field_name('left')
                right = node.child_by_field_name('right')
                append = _text(node.child_by_field_name('operator')) != '='
                if left is not None and left.type == 'identifier' and _text(left) in context['fields'] \
                        and _text(left) not in context['defs'] and _text(left) not in context['params']:
                    context['field_writes'].setdefault(_text(left), set()).update(self.origins(right, context))
                elif left is not None and left.type == 'identifier':
                    self._define(_text(left), right, context, append=append)
                elif left is not None and left.type == 'field_access':
                    target = left.child_by_field_name('object')
                    if target is not None and target.type == 'this':
                        field = _text(left.child_by_field_name('field'))
                        context['field_writes'].setdefault(field, set()).update(self.origins(right, context))
            elif node_type == 'return_statement':
                if node.named_children:
                    context['returns'].update(self.origins(node.named_children[0], context))
                    if len(context['return_shapes']) < 5:
                        context['return_shapes'].append(dict(self.shape(node.named_children[0], context),
                                                             line=_line(node)))
            elif node_type == 'type_pattern':  # obj instanceof Foo foo
                children = node.named_children
                if len(children) >= 2:
                    context['vars'][_text(children[-1])] = _text(children[0])
                    self._record_type(_text(children[0]), context)
            elif node_type == 'method_invocation':
                self._visit_invocation(node, context)
            elif node_type == 'method_reference':
                self._visit_method_reference(node, context)
            elif node_type == 'object_creation_expression':
                type_text = _text(node.child_by_field_name('type'))
                resolved = self._record_type(type_text, context)
                if resolved and not resolved.startswith('java.'):
                    self._record_call(resolved, node, context)
                self._record_constructor(node, resolved or type_text, context)
            elif node_type in ('class_literal',):
                self._record_type(_text(node)[:-len('.class')], context)
            elif node_type == 'cast_expression':
                self._record_type(_text(node.child_by_field_name('type')), context)
            elif node_type in TYPE_DECLARATIONS:
                continue  # local / anonymous type declarations are not walked as part of the method
            stack.extend(reversed(node.named_children))


def resolve_constant_refs(value: str, constants: Dict[str, str]) -> str:
    """Replace '{{Ref}}' placeholders (unresolved constant references) with known constant values."""
    def replace(match):
        ref = match.group(1)
        if ref in constants:
            return constants[ref]
        simple = ref.rsplit('.', 2)
        for i in range(1, len(simple)):
            key = '.'.join(simple[i:])
            if key in constants:
                return constants[key]
        return match.group(0)
    return _CONST_REF.sub(replace, value)
