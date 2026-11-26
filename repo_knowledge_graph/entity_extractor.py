import ast
import logging
import os
import re
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from .languages import get_language_spec
from .utils.path_utils import generate_entity_aliases, with_extra_aliases

logger = logging.getLogger(__name__)

# Parser backends are imported lazily, so that e.g. libclang is only required
# when a C/C++ file is actually analysed.
cindex = None
esprima = None
BeautifulSoup = None
ts_rust = None
Language = None
Parser = None


def _import_clang():
    global cindex
    if cindex is None:
        from clang import cindex as module
        cindex = module


def _import_esprima():
    global esprima
    if esprima is None:
        import esprima as module
        esprima = module


def _import_bs4():
    global BeautifulSoup
    if BeautifulSoup is None:
        from bs4 import BeautifulSoup as cls
        BeautifulSoup = cls


def _import_tree_sitter():
    global Language, Parser
    if Parser is None:
        from tree_sitter import Language as language_cls
        from tree_sitter import Parser as parser_cls
        Language, Parser = language_cls, parser_cls


def _import_tree_sitter_rust():
    global ts_rust
    _import_tree_sitter()
    if ts_rust is None:
        import tree_sitter_rust as module
        ts_rust = module


class BaseASTEntityExtractor:
    def get_call_sites(self) -> List[Tuple[str, int]]:
        """(called name, line) pairs, when the extractor knows positions (used for chunk mapping)."""
        return []

    def extract_entities(self, code: str, file_path: str = None) -> Tuple[List[Dict[str, Any]], List[str]]:
        """
        Extract entities from source code.

        Args:
            code: Source code as string
            file_path: Optional path to the source file (for better context and include resolution)

        Returns:
            Tuple of (declared_entities, called_entities)
        """
        raise NotImplementedError


    def get_imports(self) -> List[str]:
        """Modules / files imported by the last extracted file (empty when not supported)."""
        return list(dict.fromkeys(getattr(self, 'imports', []) or []))

    # Add a reset contract so extractors can be reused safely
    def reset(self) -> None:
        """
        Reset internal state so the extractor instance can be reused.
        Concrete extractors should override this to clear their buffers.
        """
        raise NotImplementedError

class HTMLEntityExtractor(BaseASTEntityExtractor):
    """
    Hybrid HTML AST-based entity extractor.

    Responsibilities:
      • Parse HTML into a tree
      • Extract declared DOM entities (ids, names, classes)
      • Extract JavaScript calls from inline event handlers
      • Extract JS entities from <script> tags
      • Integrate cleanly with the hybrid AST graph linker
    """

    EVENT_ATTR_PREFIX = "on"  # e.g., onclick, onsubmit, etc.

    def __init__(self):
        _import_bs4()
        self.js_extractor = JavaScriptEntityExtractor()
        self.reset()

    # --------------------------------------
    # Core interface
    # --------------------------------------
    def reset(self):
        self.declared_entities: List[Dict[str, str]] = []
        self.called_entities: List[str] = []

    def extract_entities(self, code: str, file_path: str = None) -> Tuple[List[Dict[str, str]], List[str]]:
        """Main entry point: parse HTML and extract entities."""
        self.reset()
        try:
            soup = BeautifulSoup(code, "html.parser")
        except Exception as e:
            logger.debug(f"HTML extraction: Parsing error: {e}")
            return [], []

        # --- DOM element declarations ---
        for tag in soup.find_all(True):
            self._handle_tag_declaration(tag)
            self._handle_event_attributes(tag)

        # --- <script> tags (inline + external) ---
        for script in soup.find_all("script"):
            self._handle_script(script)

        # --- Deduplication ---
        self.declared_entities = self._deduplicate_dicts(self.declared_entities)
        self.called_entities = self._deduplicate_list(self.called_entities)

        return self.declared_entities, self.called_entities

    # --------------------------------------
    # Tag & attribute handlers
    # --------------------------------------
    def _handle_tag_declaration(self, tag):
        """Extract declared DOM elements (id, name, class)."""
        if tag.has_attr("id"):
            self.declared_entities.append({"name": tag["id"], "type": "element"})

        if tag.has_attr("name"):
            self.declared_entities.append({"name": tag["name"], "type": "element"})

        if tag.has_attr("class"):
            classes = tag["class"]
            if isinstance(classes, list):
                for c in classes:
                    self.declared_entities.append({"name": c, "type": "class"})
            elif isinstance(classes, str):
                self.declared_entities.append({"name": classes, "type": "class"})

    def _handle_event_attributes(self, tag):
        """Extract JS calls from inline event attributes."""
        if not self.js_extractor:
            return
        for attr, value in tag.attrs.items():
            if attr.lower().startswith(self.EVENT_ATTR_PREFIX) and isinstance(value, str):
                try:
                    _, called = self.js_extractor.extract_entities(value)
                    self.called_entities.extend(called)
                except Exception as e:
                    logger.debug(f"HTML extraction: JS parse error in {attr}: {e}")

    def _handle_script(self, script):
        """Extract JS entities from <script> blocks or src attributes."""
        if script.has_attr("src"):
            src = script["src"]
            self.called_entities.append(src)
            return

        if not self.js_extractor:
            return

        js_code = (script.string or "").strip()
        if js_code:
            try:
                declared, called = self.js_extractor.extract_entities(js_code)
                self.declared_entities.extend(declared)
                self.called_entities.extend(called)
            except Exception as e:
                logger.debug(f"HTML extraction: JS parse error in <script>: {e}")

    # --------------------------------------
    # Helpers
    # --------------------------------------
    @staticmethod
    def _deduplicate_dicts(dicts: List[Dict]) -> List[Dict]:
        seen = set()
        result = []
        for d in dicts:
            key = tuple(sorted(d.items()))
            if key not in seen:
                seen.add(key)
                result.append(d)
        return result

    @staticmethod
    def _deduplicate_list(items: List[str]) -> List[str]:
        seen = set()
        result = []
        for i in items:
            if i not in seen:
                seen.add(i)
                result.append(i)
        return result


class JavaScriptEntityExtractor(BaseASTEntityExtractor):
    """
    Extract declared and called entities from JavaScript code using esprima.
    Handles ES6+ syntax including classes, arrow functions, imports/exports.
    Also detects API endpoint calls (fetch, axios, etc.).
    """

    # Common HTTP methods to detect
    HTTP_METHODS = {'get', 'post', 'put', 'patch', 'delete', 'head', 'options'}

    # API call patterns to detect
    API_PATTERNS = {
        'fetch',           # fetch('/api/users')
        'axios',           # axios.get('/api/users')
        '$http',           # Angular $http
        'request',         # request library
        'superagent',      # superagent library
    }

    def __init__(self):
        _import_esprima()
        self.reset()

    def reset(self) -> None:
        self.declared_entities: List[Dict[str, Any]] = []
        self.called_entities: List[str] = []
        self.imports: List[str] = []
        self.scope_stack: List[str] = []
        self.api_calls: List[Dict[str, Any]] = []  # Track API endpoint calls

    def _qualified(self, name: str) -> str:
        """Return fully qualified name using current scope stack."""
        if not name:
            return ""
        scope = ".".join(self.scope_stack)
        return f"{scope}.{name}" if scope else name

    def _get_function_name(self, node) -> Optional[str]:
        """Extract function name from various function node types."""
        if hasattr(node, 'id') and node.id:
            return node.id.name
        return None

    def _walk_node(self, node):
        """Recursively walk the AST and extract entities."""
        if not node or not hasattr(node, 'type'):
            return

        node_type = node.type

        # --- Function Declaration ---
        if node_type == 'FunctionDeclaration':
            func_name = self._get_function_name(node)
            if func_name:
                qualified = self._qualified(func_name)
                self.declared_entities.append({"name": qualified, "type": "function"})

                # Extract parameters
                if hasattr(node, 'params'):
                    for param in node.params:
                        param_name = self._extract_pattern_name(param)
                        if param_name:
                            self.declared_entities.append({
                                "name": f"{qualified}.{param_name}",
                                "type": "variable",
                                "dtype": "unknown"
                            })

                self.scope_stack.append(func_name)
                if hasattr(node, 'body'):
                    self._walk_node(node.body)
                self.scope_stack.pop()

        # --- Arrow Function Expression ---
        elif node_type == 'ArrowFunctionExpression':
            # Arrow functions are typically assigned, handle in VariableDeclarator
            if hasattr(node, 'params'):
                for param in node.params:
                    param_name = self._extract_pattern_name(param)
                    # Note: can't fully qualify without parent context
            if hasattr(node, 'body'):
                self._walk_node(node.body)

        # --- Function Expression ---
        elif node_type == 'FunctionExpression':
            func_name = self._get_function_name(node)
            if func_name:
                qualified = self._qualified(func_name)
                self.declared_entities.append({"name": qualified, "type": "function"})
                self.scope_stack.append(func_name)

            if hasattr(node, 'params'):
                for param in node.params:
                    param_name = self._extract_pattern_name(param)
                    if param_name and func_name:
                        self.declared_entities.append({
                            "name": f"{self._qualified(func_name)}.{param_name}",
                            "type": "variable",
                            "dtype": "unknown"
                        })

            if hasattr(node, 'body'):
                self._walk_node(node.body)

            if func_name:
                self.scope_stack.pop()

        # --- Class Declaration ---
        elif node_type == 'ClassDeclaration':
            class_name = node.id.name if hasattr(node, 'id') and node.id else None
            if class_name:
                qualified = self._qualified(class_name)
                class_entity = {"name": qualified, "type": "class"}
                self.declared_entities.append(class_entity)

                # Handle inheritance
                if hasattr(node, 'superClass') and node.superClass:
                    if hasattr(node.superClass, 'name'):
                        self.called_entities.append(node.superClass.name)
                        class_entity["bases"] = [node.superClass.name]

                self.scope_stack.append(class_name)
                if hasattr(node, 'body') and hasattr(node.body, 'body'):
                    for method in node.body.body:
                        self._walk_node(method)
                self.scope_stack.pop()

        # --- Method Definition ---
        elif node_type == 'MethodDefinition':
            method_name = node.key.name if hasattr(node, 'key') and hasattr(node.key, 'name') else None
            if method_name:
                qualified = self._qualified(method_name)
                self.declared_entities.append({"name": qualified, "type": "method"})

                if hasattr(node, 'value') and hasattr(node.value, 'params'):
                    for param in node.value.params:
                        param_name = self._extract_pattern_name(param)
                        if param_name:
                            self.declared_entities.append({
                                "name": f"{qualified}.{param_name}",
                                "type": "variable",
                                "dtype": "unknown"
                            })

                if hasattr(node, 'value'):
                    self._walk_node(node.value)

        # --- Variable Declaration ---
        elif node_type == 'VariableDeclaration':
            if hasattr(node, 'declarations'):
                for decl in node.declarations:
                    self._walk_node(decl)

        # --- Variable Declarator ---
        elif node_type == 'VariableDeclarator':
            var_name = self._extract_pattern_name(node.id) if hasattr(node, 'id') else None
            if var_name:
                qualified = self._qualified(var_name)

                # Check if it's a function assignment
                if hasattr(node, 'init') and node.init:
                    if node.init.type in ('FunctionExpression', 'ArrowFunctionExpression'):
                        self.declared_entities.append({"name": qualified, "type": "function"})
                        self.scope_stack.append(var_name)
                        self._walk_node(node.init)
                        self.scope_stack.pop()
                    else:
                        self.declared_entities.append({
                            "name": qualified,
                            "type": "variable",
                            "dtype": "unknown"
                        })
                        self._walk_node(node.init)
                else:
                    self.declared_entities.append({
                        "name": qualified,
                        "type": "variable",
                        "dtype": "unknown"
                    })

        # --- Call Expression ---
        elif node_type == 'CallExpression':
            callee_name = self._extract_callee_name(node.callee) if hasattr(node, 'callee') else None
            if callee_name:
                self.called_entities.append(callee_name)

                # Detect API endpoint calls
                self._detect_api_call(node, callee_name)

            # Walk arguments
            if hasattr(node, 'arguments'):
                for arg in node.arguments:
                    self._walk_node(arg)

        # --- Member Expression ---
        elif node_type == 'MemberExpression':
            # Don't record as call, just traverse
            if hasattr(node, 'object'):
                self._walk_node(node.object)
            if hasattr(node, 'property'):
                self._walk_node(node.property)

        # --- Import/Export ---
        elif node_type == 'ImportDeclaration':
            if hasattr(node, 'source') and hasattr(node.source, 'value'):
                self.called_entities.append(node.source.value)
                self.imports.append(node.source.value)

        elif node_type == 'ExportNamedDeclaration':
            if hasattr(node, 'declaration'):
                self._walk_node(node.declaration)

        elif node_type == 'ExportDefaultDeclaration':
            if hasattr(node, 'declaration'):
                self._walk_node(node.declaration)

        # --- Recursive traversal for other nodes ---
        else:
            if hasattr(node, '__dict__'):
                for attr, val in vars(node).items():
                    if isinstance(val, list):
                        for item in val:
                            if hasattr(item, 'type'):
                                self._walk_node(item)
                    elif hasattr(val, 'type'):
                        self._walk_node(val)

    def _extract_pattern_name(self, pattern) -> Optional[str]:
        """Extract name from various pattern types (Identifier, ObjectPattern, etc.)."""
        if not pattern:
            return None
        if hasattr(pattern, 'type'):
            if pattern.type == 'Identifier':
                return pattern.name if hasattr(pattern, 'name') else None
            elif pattern.type == 'RestElement':
                return self._extract_pattern_name(pattern.argument) if hasattr(pattern, 'argument') else None
        return None

    def _extract_callee_name(self, callee) -> Optional[str]:
        """Extract the name of the function being called."""
        if not callee:
            return None

        if hasattr(callee, 'type'):
            if callee.type == 'Identifier':
                return callee.name if hasattr(callee, 'name') else None
            elif callee.type == 'MemberExpression':
                obj = self._extract_callee_name(callee.object) if hasattr(callee, 'object') else ""
                prop = callee.property.name if hasattr(callee, 'property') and hasattr(callee.property, 'name') else ""
                if obj and prop:
                    return f"{obj}.{prop}"
                return prop or obj
        return None

    def _detect_api_call(self, call_node, callee_name: str):
        """
        Detect API endpoint calls in JavaScript code.
        Handles patterns like:
        - fetch('/api/users')
        - axios.get('/api/users')
        - axios.post('/api/users', data)
        - request.get('/api/users')
        """
        if not callee_name or not hasattr(call_node, 'arguments'):
            return

        # Split callee name to check for patterns
        parts = callee_name.split('.')
        base = parts[0]
        method = parts[-1].lower() if len(parts) > 1 else None

        # Check if this is an API call
        is_api_call = False
        http_method = 'unknown'

        # Pattern 1: fetch('/api/...')
        if base == 'fetch':
            is_api_call = True
            http_method = 'GET'  # Default for fetch

        # Pattern 2: axios.get('/api/...'), request.post(...), etc.
        elif base in self.API_PATTERNS and method in self.HTTP_METHODS:
            is_api_call = True
            http_method = method.upper()

        # Pattern 3: axios('/api/...', {method: 'POST'})
        elif base in self.API_PATTERNS and method is None:
            is_api_call = True
            http_method = 'GET'  # Default

        if not is_api_call:
            return

        # Extract the endpoint URL from arguments
        if call_node.arguments:
            first_arg = call_node.arguments[0]
            endpoint = self._extract_string_literal(first_arg)

            if endpoint:
                # Store as a called entity with special type
                self.called_entities.append(f"API:{http_method}:{endpoint}")

                # Also track in api_calls for easier filtering
                self.api_calls.append({
                    "endpoint": endpoint,
                    "method": http_method,
                    "type": "api_call"
                })

    def _extract_string_literal(self, node) -> Optional[str]:
        """Extract string value from a Literal/TemplateLiteral node."""
        if not node or not hasattr(node, 'type'):
            return None

        if node.type == 'Literal' and isinstance(node.value, str):
            return node.value
        elif node.type == 'TemplateLiteral':
            # For template literals, we try to extract the quasi parts
            # e.g., `/api/${version}/users` -> /api/{version}/users
            if hasattr(node, 'quasis'):
                parts = []
                for i, quasi in enumerate(node.quasis):
                    if hasattr(quasi, 'value') and hasattr(quasi.value, 'raw'):
                        parts.append(quasi.value.raw)
                    if i < len(node.quasis) - 1:
                        parts.append('{param}')
                return ''.join(parts)

        return None

    def extract_entities(self, code: str, file_path: str = None) -> Tuple[List[Dict[str, Any]], List[str]]:
        self.reset()

        try:
            tree = esprima.parseScript(code, {'tolerant': True, 'loc': False})
        except Exception as e:
            # Try parsing as module if script fails
            try:
                tree = esprima.parseModule(code, {'tolerant': True, 'loc': False})
            except Exception as e2:
                logger.error(f"Failed to parse JavaScript code: {e2}")
                return [], []

        if hasattr(tree, 'body'):
            for node in tree.body:
                self._walk_node(node)

        # Deduplicate
        seen_decl = set()
        unique_declared = []
        for e in self.declared_entities:
            key = (e.get("name"), e.get("type"), e.get("dtype"))
            if key not in seen_decl:
                unique_declared.append(e)
                seen_decl.add(key)

        unique_called = list(dict.fromkeys(self.called_entities))
        return unique_declared, unique_called


class CEntityExtractor(BaseASTEntityExtractor):
    """
    Extract declared and called entities from C code using clang.cindex (libclang),
    with filtering to ignore system headers.
    """

    def __init__(self):
        _import_clang()
        self.index = cindex.Index.create()

    def reset(self) -> None:
        """No persistent state to reset, but method provided for interface consistency."""
        pass

    def _walk_cursor(self, cursor, declared, called, source_file):
        """Recursively walk a clang Cursor, restricted to the main file."""
        for c in cursor.get_children():
            # --- Include directives ---
            # Note: INCLUSION_DIRECTIVE nodes are at the root level and need special handling
            if c.kind == cindex.CursorKind.INCLUSION_DIRECTIVE:
                # Get the included file name
                included_file = c.displayname
                if included_file:
                    called.append(included_file)
                continue

            loc = c.location
            if not loc.file or not source_file:
                continue

            # Skip system / external headers for other nodes
            if os.path.abspath(loc.file.name) != os.path.abspath(source_file):
                continue

            # --- Declarations ---
            if c.kind.is_declaration():
                if c.kind in (cindex.CursorKind.FUNCTION_DECL, cindex.CursorKind.FUNCTION_TEMPLATE):
                    name = c.spelling or c.displayname
                    declared.append({"name": name, "type": "function"})
                    for p in c.get_arguments():
                        declared.append({
                            "name": f"{name}.{p.spelling}",
                            "type": "variable",
                            "dtype": p.type.spelling
                        })
                elif c.kind == cindex.CursorKind.VAR_DECL:
                    declared.append({
                        "name": c.spelling,
                        "type": "variable",
                        "dtype": c.type.spelling
                    })

                    # Add the variable's type to called entities
                    # This captures struct references like "struct Point p;"
                    if c.type.spelling:
                        # Extract the base type name (remove const, &, *, struct keyword, etc.)
                        type_name = c.type.spelling.strip()
                        # Remove common qualifiers and keywords
                        type_name = type_name.replace('const', '').replace('&', '').replace('*', '').replace('struct', '').strip()
                        if type_name and type_name not in ['int', 'float', 'double', 'char', 'bool', 'void', 'long', 'short', 'unsigned', 'signed', 'size_t']:
                            called.append(type_name)
                elif c.kind == cindex.CursorKind.STRUCT_DECL:
                    declared.append({"name": c.spelling or c.displayname, "type": "struct"})
                elif c.kind == cindex.CursorKind.TYPEDEF_DECL:
                    declared.append({"name": c.spelling, "type": "typedef"})

            # --- Calls ---
            if c.kind == cindex.CursorKind.CALL_EXPR:
                callee = None
                for child in c.get_children():
                    if child.kind in (cindex.CursorKind.DECL_REF_EXPR, cindex.CursorKind.MEMBER_REF_EXPR):
                        callee = child.spelling
                        break
                if callee:
                    called.append(callee)
                else:
                    if c.displayname or c.spelling:
                        called.append(c.displayname or c.spelling)

            # --- Recurse ---
            self._walk_cursor(c, declared, called, source_file)

    def extract_entities(self, code: str, file_path: str = None) -> Tuple[List[Dict[str, Any]], List[str]]:
        declared, called = [], []

        # If file_path is provided, use it directly for better include resolution
        # Otherwise, create a temporary file
        tf_name = None
        temp_file = False

        if file_path and os.path.exists(file_path):
            tf_name = file_path
            temp_file = False
        else:
            with tempfile.NamedTemporaryFile(suffix=".c", mode="w+", delete=False) as tf:
                tf_name = tf.name
                tf.write(code)
                tf.flush()
            temp_file = True

        # Get the directory containing the file for include paths
        include_dir = os.path.dirname(tf_name) if tf_name else None
        args = ['-std=c11']
        if include_dir:
            args.append(f'-I{include_dir}')
        # Extra include roots (e.g. the repository root for '#include "pkg/header.h"')
        for extra_dir in getattr(self, 'include_dirs', None) or []:
            args.append(f'-I{extra_dir}')

        try:
            tu = self.index.parse(
                tf_name,
                args=args,
                options=cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD
            )
        except Exception as e:
            raise RuntimeError(f"libclang failed to parse translation unit: {e}")

        self._walk_cursor(tu.cursor, declared, called, tf_name)

        # Deduplicate
        seen_decl = set()
        unique_declared = []
        for e in declared:
            key = (e.get("name"), e.get("type"), e.get("dtype", None))
            if key not in seen_decl:
                unique_declared.append(e)
                seen_decl.add(key)

        unique_called = [c for c in dict.fromkeys(called) if c]

        # Only delete if we created a temp file
        if temp_file:
            try:
                os.unlink(tf_name)
            except Exception:
                pass

        return unique_declared, unique_called


class CppEntityExtractor(BaseASTEntityExtractor):
    """
    Extract declared and called entities from C++ code using clang.cindex (libclang),
    including classes, namespaces, and methods.
    """

    def __init__(self):
        _import_clang()
        self.index = cindex.Index.create()
        self.reset()

    def reset(self) -> None:
        self.declared_entities = []
        self.called_entities = []
        self.scope_stack = []

    def _qualified(self, name: str) -> str:
        """Return fully qualified name using current scope stack."""
        if not name:
            return ""
        if not self.scope_stack:
            return name
        return "::".join(self.scope_stack + [name])

    def _walk_cursor(self, cursor, source_file: str):
        for c in cursor.get_children():
            # --- Include directives ---
            # Note: INCLUSION_DIRECTIVE nodes are at the root level and need special handling
            if c.kind == cindex.CursorKind.INCLUSION_DIRECTIVE:
                # Get the included file name
                included_file = c.displayname
                if included_file:
                    self.called_entities.append(included_file)
                continue

            kind = c.kind

            # --- Namespace --- (process before location check)
            if kind == cindex.CursorKind.NAMESPACE:
                if c.spelling:  # Only add non-empty namespace names
                    self.scope_stack.append(c.spelling)
                self._walk_cursor(c, source_file)
                if c.spelling:
                    self.scope_stack.pop()
                continue

            # Check location for other node types
            loc = c.location
            # Skip nodes from other files, but allow nodes without location info
            if loc.file and os.path.abspath(loc.file.name) != os.path.abspath(source_file):
                continue

            # --- Class / Struct ---
            if kind in (cindex.CursorKind.CLASS_DECL, cindex.CursorKind.STRUCT_DECL):
                # Only process if it has a name
                if c.spelling:
                    # Check if it's a definition (not a forward declaration)
                    is_def = c.is_definition() if hasattr(c, 'is_definition') else True
                    if is_def:
                        full_name = self._qualified(c.spelling)
                        self.declared_entities.append({"name": full_name, "type": "class"})

                        # Handle base classes (inheritance)
                        for base in c.get_children():
                            if base.kind == cindex.CursorKind.CXX_BASE_SPECIFIER:
                                if base.spelling:
                                    self.called_entities.append(base.spelling)

                        self.scope_stack.append(c.spelling)
                        self._walk_cursor(c, source_file)
                        self.scope_stack.pop()
                continue

            # --- Methods ---
            if kind in (cindex.CursorKind.CXX_METHOD, cindex.CursorKind.CONSTRUCTOR, cindex.CursorKind.DESTRUCTOR):
                if c.spelling:  # Only process if it has a name
                    full_name = self._qualified(c.spelling)
                    self.declared_entities.append({"name": full_name, "type": "method"})

                    for p in c.get_arguments():
                        if p.spelling:  # Only add parameters with names
                            self.declared_entities.append({
                                "name": f"{full_name}.{p.spelling}",
                                "type": "variable",
                                "dtype": p.type.spelling
                            })

                self._walk_cursor(c, source_file)
                continue

            # --- Free functions ---
            if kind == cindex.CursorKind.FUNCTION_DECL:
                if c.spelling:  # Only process if it has a name
                    full_name = self._qualified(c.spelling)
                    self.declared_entities.append({"name": full_name, "type": "function"})
                    for p in c.get_arguments():
                        if p.spelling:  # Only add parameters with names
                            self.declared_entities.append({
                                "name": f"{full_name}.{p.spelling}",
                                "type": "variable",
                                "dtype": p.type.spelling
                            })
                self._walk_cursor(c, source_file)
                continue

            # --- Variables ---
            if kind == cindex.CursorKind.VAR_DECL:
                full_name = self._qualified(c.spelling)
                self.declared_entities.append({
                    "name": full_name,
                    "type": "variable",
                    "dtype": c.type.spelling
                })

                # Look for TYPE_REF children which explicitly reference the type
                # This is more reliable than c.type.spelling when includes aren't resolved
                type_ref_found = False
                for child in c.get_children():
                    if child.kind == cindex.CursorKind.TYPE_REF:
                        # TYPE_REF.spelling gives us the fully qualified type name
                        # It may have 'class ' or 'struct ' prefix, so strip it
                        if child.spelling:
                            type_name = child.spelling.replace('class ', '').replace('struct ', '').strip()
                            if type_name:
                                # TYPE_REF gives us the canonical name from the definition,
                                # which includes namespace qualifiers if present.
                                # We only add this canonical name and rely on alias resolution
                                # to match unqualified usage (e.g., 'Calculator' -> 'math::Calculator')
                                self.called_entities.append(type_name)
                                type_ref_found = True
                                break

                # Fallback: use c.type.spelling if no TYPE_REF found
                # Note: c.type.spelling may give us the name as written in source code,
                # which could be unqualified even if it refers to a namespaced type
                if not type_ref_found and c.type.spelling:
                    # Extract the base type name (remove const, &, *, etc.)
                    type_name = c.type.spelling.strip()
                    # Remove common qualifiers
                    type_name = type_name.replace('const', '').replace('&', '').replace('*', '').strip()
                    if type_name and type_name not in ['int', 'float', 'double', 'char', 'bool', 'void', 'long', 'short', 'unsigned', 'signed']:
                        # Only add if not already added via TYPE_REF
                        # c.type.spelling might give unqualified name even for namespaced types
                        # We'll add it and let alias resolution handle it
                        self.called_entities.append(type_name)

            # --- Calls ---
            if kind == cindex.CursorKind.CALL_EXPR:
                callee = None
                for child in c.get_children():
                    if child.kind in (cindex.CursorKind.DECL_REF_EXPR, cindex.CursorKind.MEMBER_REF_EXPR):
                        callee = child.spelling
                        break
                if callee:
                    self.called_entities.append(callee)
                else:
                    if c.displayname or c.spelling:
                        self.called_entities.append(c.displayname or c.spelling)

            # Recurse
            self._walk_cursor(c, source_file)

    def extract_entities(self, code: str, file_path: str = None) -> Tuple[List[Dict[str, Any]], List[str]]:
        self.reset()

        # If file_path is provided, use it directly for better include resolution
        # Otherwise, create a temporary file
        tf_name = None
        temp_file = False

        if file_path and os.path.exists(file_path):
            tf_name = file_path
            temp_file = False
        else:
            with tempfile.NamedTemporaryFile(suffix=".cpp", mode="w+", delete=False) as tf:
                tf_name = tf.name
                tf.write(code)
                tf.flush()
            temp_file = True

        # Get the directory containing the file for include paths
        include_dir = os.path.dirname(tf_name) if tf_name else None
        args = ['-std=c++17', '-xc++']
        if include_dir:
            args.append(f'-I{include_dir}')
        # Extra include roots (e.g. the repository root for '#include "pkg/header.h"')
        for extra_dir in getattr(self, 'include_dirs', None) or []:
            args.append(f'-I{extra_dir}')

        try:
            tu = self.index.parse(
                tf_name,
                args=args,
                options=cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD
            )
        except Exception as e:
            raise RuntimeError(f"libclang failed to parse C++ translation unit: {e}")

        self._walk_cursor(tu.cursor, tf_name)

        # Deduplicate
        seen_decl = set()
        unique_declared = []
        for e in self.declared_entities:
            key = (e.get("name"), e.get("type"), e.get("dtype", None))
            if key not in seen_decl:
                unique_declared.append(e)
                seen_decl.add(key)

        unique_called = list(dict.fromkeys(self.called_entities))

        # Only delete if we created a temp file
        if temp_file:
            try:
                os.unlink(tf_name)
            except Exception:
                pass

        return unique_declared, unique_called


class RustEntityExtractor(BaseASTEntityExtractor):
    """
    Extract declared and called entities from Rust code using tree-sitter.
    Handles structs, enums, traits, functions, methods, and modules.
    Also detects API endpoint definitions (Actix-web, Rocket, Axum, Warp).
    """

    # HTTP method route macros for Rust web frameworks
    ROUTE_MACROS = {
        'get', 'post', 'put', 'patch', 'delete', 'head', 'options',  # Actix-web, Rocket
        'Get', 'Post', 'Put', 'Patch', 'Delete', 'Head', 'Options',  # Alternative casing
    }

    # Route-related macros and functions
    ROUTE_PATTERNS = {
        'route',        # Generic route macro
        'web::get', 'web::post', 'web::put', 'web::delete',  # Actix-web with web::
    }

    def __init__(self):
        _import_tree_sitter_rust()
        self.parser = Parser()
        self.parser.language = Language(ts_rust.language())
        self.reset()

    def reset(self) -> None:
        self.declared_entities = []
        self.called_entities = []
        self.scope_stack = []
        self.api_endpoints: List[Dict[str, Any]] = []  # Track API endpoint definitions

    def _qualified(self, name: str) -> str:
        """Return fully qualified name using current scope stack."""
        if not name:
            return ""
        if not self.scope_stack:
            return name
        return "::".join(self.scope_stack + [name])

    def _get_node_text(self, node, code_bytes: bytes) -> str:
        """Extract text content of a node."""
        return code_bytes[node.start_byte:node.end_byte].decode('utf8')

    def _extract_api_endpoint_from_attributes(self, node, code_bytes: bytes) -> Optional[Dict[str, Any]]:
        """
        Extract API endpoint information from Rust function attributes.
        Handles patterns like:
        - #[get("/users")]                    # Actix-web, Rocket
        - #[post("/users")]                   # Actix-web, Rocket
        - #[route("/users", method="GET")]    # Generic route

        Note: In tree-sitter Rust AST, attributes appear as PREVIOUS SIBLINGS
        of the function_item node, not as children.
        """


        # Get the parent node to access siblings
        parent = node.parent
        if not parent:
            return None

        # Find the index of current node in parent's children
        node_index = None
        for i, child in enumerate(parent.children):
            if child == node:
                node_index = i
                break

        if node_index is None:
            return None

        # Look backwards through previous siblings for attribute_item nodes
        for i in range(node_index - 1, -1, -1):
            sibling = parent.children[i]

            # Stop if we hit a non-attribute node (except comments/whitespace)
            if sibling.type not in ['attribute_item', 'line_comment', 'block_comment']:
                break

            if sibling.type == 'attribute_item':
                attr_text = self._get_node_text(sibling, code_bytes)

                # Match HTTP method macros: #[get("/path")], #[post("/path")], #[post("/path", data = "<var>")], etc.
                # The pattern now allows optional additional parameters after the path
                method_pattern = r'#\[(get|post|put|patch|delete|head|options)\s*\(\s*"([^"]+)"(?:\s*,.*?)?\s*\)\]'
                match = re.search(method_pattern, attr_text, re.IGNORECASE)

                if match:
                    http_method = match.group(1).upper()
                    endpoint_path = match.group(2)
                    return {
                        "endpoint": endpoint_path,
                        "methods": [http_method],
                        "type": "api_endpoint_definition"
                    }

                # Match generic route macro: #[route("/path", method="GET")]
                route_pattern = r'#\[route\s*\(\s*"([^"]+)"(?:.*?method\s*=\s*"([^"]+)")?\s*\)\]'
                match = re.search(route_pattern, attr_text, re.IGNORECASE)

                if match:
                    endpoint_path = match.group(1)
                    http_method = match.group(2).upper() if match.group(2) else "GET"
                    return {
                        "endpoint": endpoint_path,
                        "methods": [http_method],
                        "type": "api_endpoint_definition"
                    }

        return None

    def _walk_tree(self, node, code_bytes: bytes):
        """Recursively walk the tree-sitter AST."""
        node_type = node.type

        # --- Module declarations ---
        if node_type == 'mod_item':
            # mod my_module { ... }
            name_node = node.child_by_field_name('name')
            if name_node:
                mod_name = self._get_node_text(name_node, code_bytes)
                qualified = self._qualified(mod_name)
                self.declared_entities.append({"name": qualified, "type": "module"})

                self.scope_stack.append(mod_name)
                body = node.child_by_field_name('body')
                if body:
                    for child in body.children:
                        self._walk_tree(child, code_bytes)
                self.scope_stack.pop()
                return

        # --- Struct declarations ---
        elif node_type == 'struct_item':
            name_node = node.child_by_field_name('name')
            if name_node:
                struct_name = self._get_node_text(name_node, code_bytes)
                qualified = self._qualified(struct_name)
                self.declared_entities.append({"name": qualified, "type": "struct"})

                # Check for generic parameters
                type_params = node.child_by_field_name('type_parameters')
                if type_params:
                    self._walk_tree(type_params, code_bytes)

                self.scope_stack.append(struct_name)
                # Process fields
                body = node.child_by_field_name('body')
                if body:
                    for child in body.children:
                        if child.type == 'field_declaration':
                            field_name_node = child.child_by_field_name('name')
                            field_type_node = child.child_by_field_name('type')
                            if field_name_node:
                                field_name = self._get_node_text(field_name_node, code_bytes)
                                field_type = self._get_node_text(field_type_node, code_bytes) if field_type_node else "unknown"
                                self.declared_entities.append({
                                    "name": f"{qualified}.{field_name}",
                                    "type": "field",
                                    "dtype": field_type
                                })
                self.scope_stack.pop()
                return

        # --- Enum declarations ---
        elif node_type == 'enum_item':
            name_node = node.child_by_field_name('name')
            if name_node:
                enum_name = self._get_node_text(name_node, code_bytes)
                qualified = self._qualified(enum_name)
                self.declared_entities.append({"name": qualified, "type": "enum"})

                self.scope_stack.append(enum_name)
                body = node.child_by_field_name('body')
                if body:
                    for child in body.children:
                        if child.type == 'enum_variant':
                            variant_name_node = child.child_by_field_name('name')
                            if variant_name_node:
                                variant_name = self._get_node_text(variant_name_node, code_bytes)
                                self.declared_entities.append({
                                    "name": f"{qualified}::{variant_name}",
                                    "type": "enum_variant"
                                })
                self.scope_stack.pop()
                return

        # --- Trait declarations ---
        elif node_type == 'trait_item':
            name_node = node.child_by_field_name('name')
            if name_node:
                trait_name = self._get_node_text(name_node, code_bytes)
                qualified = self._qualified(trait_name)
                self.declared_entities.append({"name": qualified, "type": "trait"})

                self.scope_stack.append(trait_name)
                body = node.child_by_field_name('body')
                if body:
                    for child in body.children:
                        self._walk_tree(child, code_bytes)
                self.scope_stack.pop()
                return

        # --- Implementation blocks ---
        elif node_type == 'impl_item':
            # impl MyStruct { ... } or impl Trait for MyStruct { ... }
            type_node = node.child_by_field_name('type')
            trait_node = node.child_by_field_name('trait')

            impl_name = None
            if type_node:
                impl_name = self._get_node_text(type_node, code_bytes)

            if trait_node:
                trait_name = self._get_node_text(trait_node, code_bytes)
                self.called_entities.append(trait_name)

            if impl_name:
                self.scope_stack.append(impl_name)

            body = node.child_by_field_name('body')
            if body:
                for child in body.children:
                    self._walk_tree(child, code_bytes)

            if impl_name:
                self.scope_stack.pop()
            return

        # --- Function declarations ---
        elif node_type == 'function_item':
            name_node = node.child_by_field_name('name')
            if name_node:
                func_name = self._get_node_text(name_node, code_bytes)
                qualified = self._qualified(func_name)

                # Check for API endpoint attributes (e.g., #[get("/users")])
                api_info = self._extract_api_endpoint_from_attributes(node, code_bytes)

                if api_info:
                    # This is an API endpoint handler
                    self.declared_entities.append({
                        "name": qualified,
                        "type": "api_endpoint",
                        "endpoint": api_info.get("endpoint"),
                        "methods": api_info.get("methods")
                    })
                    self.api_endpoints.append({**api_info, "function": qualified})
                    entity_type = "api_endpoint"
                else:
                    # Determine if this is a method (inside impl block) or free function
                    entity_type = "method" if len(self.scope_stack) > 0 else "function"
                    self.declared_entities.append({"name": qualified, "type": entity_type})

                # Extract parameters
                params = node.child_by_field_name('parameters')
                if params:
                    for child in params.children:
                        if child.type == 'parameter':
                            pattern = child.child_by_field_name('pattern')
                            type_node = child.child_by_field_name('type')
                            if pattern:
                                param_name = self._get_node_text(pattern, code_bytes)
                                param_type = self._get_node_text(type_node, code_bytes) if type_node else "unknown"
                                # Skip 'self' parameters
                                if param_name not in ['self', '&self', '&mut self', 'mut self']:
                                    self.declared_entities.append({
                                        "name": f"{qualified}.{param_name}",
                                        "type": "variable",
                                        "dtype": param_type
                                    })

                # Walk the function body to find calls
                body = node.child_by_field_name('body')
                if body:
                    self._walk_tree(body, code_bytes)
                return

        # --- Type alias ---
        elif node_type == 'type_item':
            name_node = node.child_by_field_name('name')
            if name_node:
                type_name = self._get_node_text(name_node, code_bytes)
                qualified = self._qualified(type_name)
                self.declared_entities.append({"name": qualified, "type": "type_alias"})
                return

        # --- Constant declarations ---
        elif node_type == 'const_item':
            name_node = node.child_by_field_name('name')
            type_node = node.child_by_field_name('type')
            if name_node:
                const_name = self._get_node_text(name_node, code_bytes)
                const_type = self._get_node_text(type_node, code_bytes) if type_node else "unknown"
                qualified = self._qualified(const_name)
                self.declared_entities.append({
                    "name": qualified,
                    "type": "constant",
                    "dtype": const_type
                })

        # --- Static declarations ---
        elif node_type == 'static_item':
            name_node = node.child_by_field_name('name')
            type_node = node.child_by_field_name('type')
            if name_node:
                static_name = self._get_node_text(name_node, code_bytes)
                static_type = self._get_node_text(type_node, code_bytes) if type_node else "unknown"
                qualified = self._qualified(static_name)
                self.declared_entities.append({
                    "name": qualified,
                    "type": "static",
                    "dtype": static_type
                })

        # --- Let bindings (local variables) ---
        elif node_type == 'let_declaration':
            pattern = node.child_by_field_name('pattern')
            type_node = node.child_by_field_name('type')
            if pattern and pattern.type == 'identifier':
                var_name = self._get_node_text(pattern, code_bytes)
                var_type = self._get_node_text(type_node, code_bytes) if type_node else "unknown"
                # Only track top-level or module-level variables, not function-local ones
                # For now, we skip local variables to avoid clutter

        # --- Use declarations (imports) ---
        elif node_type == 'use_declaration':
            # Extract imported items
            use_text = self._get_node_text(node, code_bytes)
            self.called_entities.append(use_text)

        # --- Call expressions ---
        elif node_type == 'call_expression':
            function = node.child_by_field_name('function')
            if function:
                func_text = self._get_node_text(function, code_bytes)
                # Clean up function call to get just the name/path
                # Handle method calls like obj.method() and path calls like std::vec::Vec::new()
                self.called_entities.append(func_text)

        # --- Macro invocations ---
        elif node_type == 'macro_invocation':
            macro_node = node.child_by_field_name('macro')
            if macro_node:
                macro_name = self._get_node_text(macro_node, code_bytes)
                self.called_entities.append(f"{macro_name}!")

        # --- Field expressions (method calls or field access) ---
        elif node_type == 'field_expression':
            field = node.child_by_field_name('field')
            if field:
                field_name = self._get_node_text(field, code_bytes)
                # This could be a field access or method call, record it
                # We don't have full context here, so just record the field name

        # Recursively walk all children
        for child in node.children:
            self._walk_tree(child, code_bytes)

    def extract_entities(self, code: str, file_path: str = None) -> Tuple[List[Dict[str, Any]], List[str]]:
        """Extract entities from Rust code using tree-sitter."""
        self.reset()

        code_bytes = code.encode('utf8')
        tree = self.parser.parse(code_bytes)

        # Walk the AST
        self._walk_tree(tree.root_node, code_bytes)

        # Deduplicate
        seen_decl = set()
        unique_declared = []
        for e in self.declared_entities:
            key = (e.get("name"), e.get("type"), e.get("dtype", None))
            if key not in seen_decl:
                unique_declared.append(e)
                seen_decl.add(key)

        unique_called = list(dict.fromkeys(self.called_entities))

        return unique_declared, unique_called


class PythonASTEntityExtractor(ast.NodeVisitor, BaseASTEntityExtractor):
    """
    AST-based entity extractor for Python code.
    Also detects API endpoint definitions (FastAPI, Flask, Django REST Framework).
    """

    # Common HTTP decorators/patterns for Python web frameworks
    API_DECORATORS = {
        'route',           # Flask @app.route
        'get', 'post', 'put', 'patch', 'delete', 'head', 'options',  # FastAPI/Flask methods
        'api_view',        # DRF @api_view
    }

    def __init__(self):
        self.declared_entities: List[Dict[str, Any]] = []
        self.called_entities: List[str] = []
        self.current_class: Optional[str] = None
        self.current_function: Optional[str] = None
        self.api_endpoints: List[Dict[str, Any]] = []  # Track API endpoint definitions
        self.imports: List[str] = []

    def reset(self) -> None:
        """Clear previous extraction state including context"""
        self.declared_entities = []
        self.called_entities = []
        self.imports = []
        self.current_class = None
        self.current_function = None
        self.api_endpoints = []

    def _get_type_annotation(self, node: ast.AST) -> str:
        """Extract type annotation from AST node"""
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Constant):
            return type(node.value).__name__
        elif isinstance(node, ast.Attribute):
            return f"{self._get_type_annotation(node.value)}.{node.attr}"
        elif isinstance(node, ast.Subscript):
            # Handle generic types like List[str], Dict[str, int]
            base = self._get_type_annotation(node.value)
            if isinstance(node.slice, ast.Tuple):
                args = [self._get_type_annotation(elt) for elt in node.slice.elts]
                return f"{base}[{', '.join(args)}]"
            else:
                arg = self._get_type_annotation(node.slice)
                return f"{base}[{arg}]"
        return "unknown"

    def _infer_type_from_value(self, node: ast.AST) -> str:
        """Infer type from assigned value"""
        if isinstance(node, ast.Constant):
            return type(node.value).__name__
        elif isinstance(node, ast.List):
            return "list"
        elif isinstance(node, ast.Dict):
            return "dict"
        elif isinstance(node, ast.Set):
            return "set"
        elif isinstance(node, ast.Tuple):
            return "tuple"
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                return node.func.id  # Constructor call
            elif isinstance(node.func, ast.Attribute):
                return "unknown"
        elif isinstance(node, ast.Name):
            return "unknown"  # Reference to another variable
        return "unknown"

    def visit_ClassDef(self, node: ast.ClassDef):
        """Visit class definitions"""
        old_class = self.current_class
        self.current_class = node.name

        # Record base classes as called entities (and on the class, for 'inherits' edges)
        bases = []
        for base in node.bases:
            if isinstance(base, ast.Name):
                bases.append(base.id)
            elif isinstance(base, ast.Attribute):
                bases.append(self._get_type_annotation(base))
        self.called_entities.extend(bases)

        # Add class to declared entities
        class_entity = {"name": node.name, "type": "class"}
        if bases:
            class_entity["bases"] = bases
        self.declared_entities.append(class_entity)

        # Continue visiting child nodes
        self.generic_visit(node)
        self.current_class = old_class

    def visit_FunctionDef(self, node: ast.FunctionDef):
        """Visit function/method definitions and detect API endpoints"""
        old_function = self.current_function

        if self.current_class:
            # This is a method
            full_name = f"{self.current_class}.{node.name}"
            entity_type = "method"
        else:
            # This is a function
            full_name = node.name
            entity_type = "function"

        self.current_function = full_name

        # Check for API endpoint decorators
        api_info = self._extract_api_endpoint_from_decorators(node.decorator_list, full_name)
        if api_info:
            # Mark this as an API endpoint
            self.declared_entities.append({
                "name": full_name,
                "type": "api_endpoint",
                "endpoint": api_info.get("endpoint"),
                "methods": api_info.get("methods")
            })
            self.api_endpoints.append(api_info)
        else:
            self.declared_entities.append({
                "name": full_name,
                "type": entity_type
            })

        # Process parameters
        for arg in node.args.args:
            if arg.arg == 'self' and self.current_class:
                continue  # Skip self parameter

            dtype = "unknown"
            if arg.annotation:
                dtype = self._get_type_annotation(arg.annotation)

            param_name = f"{full_name}.{arg.arg}" if entity_type == "method" else arg.arg
            self.declared_entities.append({
                "name": param_name,
                "type": "variable",
                "dtype": dtype
            })

        # Continue visiting child nodes
        self.generic_visit(node)
        self.current_function = old_function

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        """Visit async function/method definitions"""
        # Treat async functions the same as regular functions
        self.visit_FunctionDef(node)

    def visit_Assign(self, node: ast.Assign):
        """Visit assignment statements"""
        # Infer type from the assigned value
        dtype = self._infer_type_from_value(node.value)

        for target in node.targets:
            if isinstance(target, ast.Name):
                # Simple variable assignment
                var_name = target.id
                if self.current_class and self.current_function and self.current_function.startswith(self.current_class):
                    # Local variable in method
                    pass  # Could add local variables if needed
                else:
                    # Module-level variable
                    self.declared_entities.append({
                        "name": var_name,
                        "type": "variable",
                        "dtype": dtype
                    })

            elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                # Attribute assignment like self.name = value
                if target.value.id == 'self' and self.current_class:
                    attr_name = f"{self.current_class}.{target.attr}"
                    self.declared_entities.append({
                        "name": attr_name,
                        "type": "variable",
                        "dtype": dtype
                    })

        # Continue visiting to catch function calls in the assignment
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign):
        """Visit annotated assignment statements (PEP 526)"""
        if isinstance(node.target, ast.Name):
            dtype = self._get_type_annotation(node.annotation)
            var_name = node.target.id

            self.declared_entities.append({
                "name": var_name,
                "type": "variable",
                "dtype": dtype
            })

        elif isinstance(node.target, ast.Attribute) and isinstance(node.target.value, ast.Name):
            if node.target.value.id == 'self' and self.current_class:
                dtype = self._get_type_annotation(node.annotation)
                attr_name = f"{self.current_class}.{node.target.attr}"
                self.declared_entities.append({
                    "name": attr_name,
                    "type": "variable",
                    "dtype": dtype
                })

        # Continue visiting
        if node.value:
            self.generic_visit(node)

    def visit_Import(self, node: ast.Import):
        """Visit import statements"""
        for alias in node.names:
            # Record the imported module/package
            self.called_entities.append(alias.name)
            self.imports.append(alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        """Visit from...import statements"""
        # Relative imports keep their leading dots ('..pkg.mod') so they can be resolved per file
        self.imports.append('.' * (node.level or 0) + (node.module or ''))
        if node.module:
            # Record the module being imported from
            self.called_entities.append(node.module)
            # Optionally, also record specific imports as module.name
            for alias in node.names:
                if alias.name != '*':
                    self.called_entities.append(f"{node.module}.{alias.name}")
        else:
            # Relative imports without module (from . import x)
            for alias in node.names:
                if alias.name != '*':
                    self.called_entities.append(alias.name)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call):
        """Visit function/method calls"""
        if isinstance(node.func, ast.Name):
            # Simple function call
            self.called_entities.append(node.func.id)

        elif isinstance(node.func, ast.Attribute):
            # Method call or attribute access
            if isinstance(node.func.value, ast.Name):
                # obj.method() - we need to infer the class of obj
                # For now, just record the method name
                method_name = node.func.attr
                # Try to find the variable type from our declared entities
                obj_name = node.func.value.id
                obj_class = self._find_variable_type(obj_name)
                if obj_class and obj_class != "unknown":
                    self.called_entities.append(f"{obj_class}.{method_name}")
                else:
                    # Fallback: just record the method call
                    self.called_entities.append(method_name)

            elif isinstance(node.func.value, ast.Attribute):
                # Nested attribute access like module.Class.method()
                full_name = self._get_type_annotation(node.func)
                self.called_entities.append(full_name)

        # Continue visiting child nodes
        self.generic_visit(node)

    def _find_variable_type(self, var_name: str) -> str:
        """Find the type of a variable from declared entities"""
        for entity in self.declared_entities:
            if entity["name"] == var_name and entity["type"] == "variable":
                return entity.get("dtype", "unknown")
        return "unknown"

    def _extract_api_endpoint_from_decorators(self, decorators: List[ast.expr], function_name: str) -> Optional[Dict[str, Any]]:
        """
        Extract API endpoint information from function decorators.
        Handles patterns like:
        - @app.route("/api/users", methods=["GET", "POST"])  # Flask
        - @app.get("/api/users")                              # FastAPI
        - @router.post("/api/users")                          # FastAPI with router
        - @api_view(['GET', 'POST'])                          # Django REST Framework
        """
        for decorator in decorators:
            # Handle @app.route(...) or @app.get(...)
            if isinstance(decorator, ast.Call):
                if isinstance(decorator.func, ast.Attribute):
                    # e.g., app.route, app.get, router.post
                    method_name = decorator.func.attr.lower()

                    if method_name in self.API_DECORATORS:
                        endpoint = None
                        http_methods = []

                        # Extract endpoint from first positional argument
                        if decorator.args and isinstance(decorator.args[0], ast.Constant):
                            endpoint = decorator.args[0].value

                        # For FastAPI-style decorators (@app.get, @app.post)
                        if method_name in {'get', 'post', 'put', 'patch', 'delete', 'head', 'options'}:
                            http_methods = [method_name.upper()]

                        # For Flask-style @app.route with methods kwarg
                        elif method_name == 'route':
                            for keyword in decorator.keywords:
                                if keyword.arg == 'methods':
                                    if isinstance(keyword.value, ast.List):
                                        http_methods = [
                                            elt.value for elt in keyword.value.elts
                                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                                        ]
                            if not http_methods:
                                http_methods = ['GET']  # Flask default

                        # For DRF @api_view(['GET', 'POST'])
                        elif method_name == 'api_view':
                            if decorator.args and isinstance(decorator.args[0], ast.List):
                                http_methods = [
                                    elt.value for elt in decorator.args[0].elts
                                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                                ]

                        if endpoint:
                            return {
                                "function": function_name,
                                "endpoint": endpoint,
                                "methods": http_methods,
                                "type": "api_endpoint_definition"
                            }

        return None

    def extract_entities(self, code: str, file_path: str = None) -> Tuple[List[Dict[str, Any]], List[str]]:
        """
        Extract entities from Python code using AST parsing

        Args:
            code: Python source code as string
            file_path: Optional path to the source file (for context)

        Returns:
            Tuple of (declared_entities, called_entities)
        """
        # Ensure fresh state on each extraction
        self.reset()

        try:
            tree = ast.parse(code)
            self.visit(tree)

            # Remove duplicates while preserving order
            seen_declared = set()
            unique_declared = []
            for entity in self.declared_entities:
                key = (entity["name"], entity["type"], entity.get("dtype"))
                if key not in seen_declared:
                    unique_declared.append(entity)
                    seen_declared.add(key)

            unique_called = list(dict.fromkeys(self.called_entities))  # Remove duplicates

            return unique_declared, unique_called

        except SyntaxError as e:
            logger.error(f"Syntax error in Python code: {e}")
            return [], []
        except Exception as e:
            logger.error(f"Error parsing Python code: {e}", exc_info=True)
            return [], []


class TypeScriptEntityExtractor(BaseASTEntityExtractor):
    """
    Extract declared and called entities from TypeScript / TSX using tree-sitter
    (esprima cannot parse type annotations). Output format matches the JavaScript extractor,
    including `API:<METHOD>:<endpoint>` called entities for fetch/axios calls.
    """

    HTTP_METHODS = JavaScriptEntityExtractor.HTTP_METHODS
    API_PATTERNS = JavaScriptEntityExtractor.API_PATTERNS

    def __init__(self, tsx: bool = False):
        _import_tree_sitter()
        import tree_sitter_typescript as ts_typescript
        language = ts_typescript.language_tsx() if tsx else ts_typescript.language_typescript()
        self.parser = Parser()
        self.parser.language = Language(language)
        self.reset()

    def reset(self) -> None:
        self.declared_entities: List[Dict[str, Any]] = []
        self.called_entities: List[str] = []
        self.imports: List[str] = []
        self.scope_stack: List[str] = []
        self.api_calls: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ helpers

    def _text(self, node) -> str:
        return node.text.decode('utf8') if node is not None and node.text is not None else ''

    def _qualified(self, name: str) -> str:
        scope = ".".join(self.scope_stack)
        return f"{scope}.{name}" if scope else name

    def _string_value(self, node) -> Optional[str]:
        if node is None:
            return None
        if node.type == 'string':
            return self._text(node)[1:-1]
        if node.type == 'template_string':
            return self._text(node).strip('`')
        return None

    def _declare(self, name: str, entity_type: str, **extra) -> Dict[str, Any]:
        entity = {"name": self._qualified(name), "type": entity_type, **extra}
        self.declared_entities.append(entity)
        return entity

    def _heritage(self, class_node) -> List[str]:
        bases = []
        for child in class_node.children:
            if child.type in ('class_heritage', 'extends_type_clause'):
                for clause in ([child] if child.type == 'extends_type_clause' else child.children):
                    for item in clause.named_children:
                        if item.type in ('identifier', 'type_identifier', 'member_expression',
                                         'nested_type_identifier', 'generic_type'):
                            name = self._text(item.child_by_field_name('name') if item.type == 'generic_type' else item)
                            if name:
                                bases.append(name.split('<')[0])
        return bases

    def _record_call(self, call_node) -> None:
        function = call_node.child_by_field_name('function')
        if function is None:
            return
        callee = self._text(function)
        if not callee:
            return
        self.called_entities.append(callee)

        parts = callee.split('.')
        base, method = parts[0], (parts[-1].lower() if len(parts) > 1 else None)
        http_method = None
        if base == 'fetch':
            http_method = 'GET'
        elif base in self.API_PATTERNS and method in self.HTTP_METHODS:
            http_method = method.upper()
        elif base in self.API_PATTERNS and method is None:
            http_method = 'GET'
        if http_method:
            arguments = call_node.child_by_field_name('arguments')
            first = arguments.named_children[0] if arguments is not None and arguments.named_children else None
            endpoint = self._string_value(first)
            if endpoint:
                self.called_entities.append(f"API:{http_method}:{endpoint}")
                self.api_calls.append({"endpoint": endpoint, "method": http_method, "type": "api_call"})

    # ------------------------------------------------------------------ walk

    def _walk(self, node) -> None:
        t = node.type
        name_node = node.child_by_field_name('name')

        if t in ('class_declaration', 'abstract_class_declaration', 'class') and name_node is not None:
            name = self._text(name_node)
            bases = self._heritage(node)
            entity = self._declare(name, 'class')
            if bases:
                entity["bases"] = bases
                self.called_entities.extend(bases)
            self.scope_stack.append(name)
            body = node.child_by_field_name('body')
            if body is not None:
                for child in body.named_children:
                    self._walk(child)
            self.scope_stack.pop()
            return

        if t == 'interface_declaration' and name_node is not None:
            entity = self._declare(self._text(name_node), 'interface')
            bases = self._heritage(node)
            if bases:
                entity["bases"] = bases
            return

        if t in ('type_alias_declaration', 'enum_declaration') and name_node is not None:
            self._declare(self._text(name_node), 'type' if t == 'type_alias_declaration' else 'enum')
            return

        if t in ('function_declaration', 'generator_function_declaration', 'function_signature') and name_node is not None:
            name = self._text(name_node)
            self._declare(name, 'function')
            self.scope_stack.append(name)
            for child in node.named_children:
                self._walk(child)
            self.scope_stack.pop()
            return

        if t in ('method_definition', 'method_signature', 'abstract_method_signature') and name_node is not None:
            name = self._text(name_node)
            self._declare(name, 'method')
            self.scope_stack.append(name)
            for child in node.named_children:
                self._walk(child)
            self.scope_stack.pop()
            return

        if t in ('public_field_definition', 'property_signature') and name_node is not None and self.scope_stack:
            self._declare(self._text(name_node), 'variable')

        if t == 'variable_declarator' and name_node is not None and name_node.type == 'identifier':
            name = self._text(name_node)
            value = node.child_by_field_name('value')
            is_function = value is not None and value.type in ('arrow_function', 'function_expression', 'function')
            type_node = node.child_by_field_name('type')
            extra = {"dtype": self._text(type_node).lstrip(':').strip()} if type_node is not None else {}
            if is_function or not self.scope_stack:
                self._declare(name, 'function' if is_function else 'variable', **extra)
            if is_function:
                self.scope_stack.append(name)
                self._walk(value)
                self.scope_stack.pop()
                return

        if t == 'import_statement':
            source = self._string_value(node.child_by_field_name('source'))
            if source:
                self.called_entities.append(source)
                self.imports.append(source)
            return

        if t == 'call_expression':
            self._record_call(node)
        elif t == 'new_expression':
            constructor = node.child_by_field_name('constructor')
            if constructor is not None:
                self.called_entities.append(self._text(constructor))

        for child in node.named_children:
            self._walk(child)

    def extract_entities(self, code: str, file_path: str = None) -> Tuple[List[Dict[str, Any]], List[str]]:
        self.reset()
        try:
            tree = self.parser.parse(code.encode('utf8'))
            self._walk(tree.root_node)
        except Exception as e:
            logger.error(f"Error parsing TypeScript code: {e}", exc_info=True)
            return [], []

        seen, unique_declared = set(), []
        for e in self.declared_entities:
            key = (e.get("name"), e.get("type"), e.get("dtype"))
            if key not in seen:
                unique_declared.append(e)
                seen.add(key)
        unique_called = [c for c in dict.fromkeys(self.called_entities) if c]
        return unique_declared, unique_called


class JavaEntityExtractor:
    """Java extractor (tree-sitter, Java 8 to 21), imported lazily to avoid a circular import."""

    def __new__(cls, *args, **kwargs):
        from .java_extractor import TreeSitterJavaEntityExtractor
        return TreeSitterJavaEntityExtractor(*args, **kwargs)


_EXTRACTOR_FACTORIES = {
    'python': PythonASTEntityExtractor,
    'c': CEntityExtractor,
    'cpp': CppEntityExtractor,
    'java': JavaEntityExtractor,
    'javascript': JavaScriptEntityExtractor,
    'typescript': lambda: TypeScriptEntityExtractor(tsx=False),
    'tsx': lambda: TypeScriptEntityExtractor(tsx=True),
    'rust': RustEntityExtractor,
    'html': HTMLEntityExtractor,
}


class HybridEntityExtractor:
    """
    Dispatch entity extraction to the AST extractor matching the file language
    (see languages.py). Extractors, and their parser libraries, are created lazily.
    Files without an extractor yield no entities.
    """

    def __init__(self):
        self._extractors: Dict[str, BaseASTEntityExtractor] = {}
        self.last_call_sites: List[Tuple[str, int]] = []  # call sites of the last extracted file

    def _extractor_key(self, file_name: str) -> Optional[str]:
        spec = get_language_spec(file_name)
        if not spec or not spec.extractor:
            return None
        if spec.extractor == 'typescript' and file_name.lower().endswith('.tsx'):
            return 'tsx'
        return spec.extractor

    def _get_extractor(self, key: str) -> Optional[BaseASTEntityExtractor]:
        if key not in self._extractors:
            try:
                self._extractors[key] = _EXTRACTOR_FACTORIES[key]()
            except ImportError as e:
                logger.warning(f"Entity extraction for '{key}' files disabled, missing dependency: {e}")
                self._extractors[key] = None
        return self._extractors[key]

    @property
    def extractors(self) -> Dict[str, BaseASTEntityExtractor]:
        """Extractors instantiated so far (kept for backward compatibility)."""
        return {k: v for k, v in self._extractors.items() if v is not None}

    def supports(self, file_name: str) -> bool:
        return self._extractor_key(file_name) is not None

    def extract_file_info(self, code: str, file_name: str, include_dirs: Optional[List[str]] = None
                          ) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
        """Return (declared_entities, called_entities, imports) for a file.

        include_dirs: extra include roots for C/C++ (typically the repository root).
        """
        self.last_call_sites = []
        key = self._extractor_key(file_name)
        extractor = self._get_extractor(key) if key else None
        if extractor is None:
            logger.debug(f"No entity extractor for {file_name}")
            return [], [], []
        extractor.include_dirs = list(include_dirs or [])

        try:
            extractor.reset()
        except Exception:
            pass

        logger.debug(f"Using {key} AST extraction for {file_name}")
        try:
            try:
                declared_entities, called_entities = extractor.extract_entities(code, file_path=file_name)
            except TypeError:
                declared_entities, called_entities = extractor.extract_entities(code)
            imports = extractor.get_imports()
            self.last_call_sites = extractor.get_call_sites() if hasattr(extractor, 'get_call_sites') else []
        except Exception as e:
            logger.error(f"Error during AST extraction for file {file_name}: {e}", exc_info=True)
            return [], [], []

        # Add aliases to each declared entity based on file path
        for entity in declared_entities:
            entity_name = entity.get('name', '')
            if entity_name:
                entity['aliases'] = with_extra_aliases(generate_entity_aliases(entity_name, file_name),
                                                       entity.get('extra_aliases'), entity.get('exclude_aliases'))
        return declared_entities, called_entities, imports

    def extract_entities(self, code: str, file_name: str):
        """Return (declared_entities, called_entities) for a file."""
        declared, called, _ = self.extract_file_info(code, file_name)
        return declared, called
