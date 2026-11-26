"""
Tests: which class each test class exercises.

A Java type is a test class when it lives under a `test` source directory or has @Test /
@ParameterizedTest / @RepeatedTest / @TestFactory methods. `attributes['test']` holds its kind
(unit, web_slice, data_slice, slice, integration), its Spring test annotations, mocks and number of tests.
`tests` edges (test class -> class under test) come from:
    @WebMvcTest(X.class) / @WebFluxTest(controllers = X.class) / @RestClientTest(X.class)   via 'annotation'
    @InjectMocks fields and @Autowired / constructor-injected fields of Spring tests          via 'subject'
    the naming convention FooTest / FooTests / FooIT / FooIntegrationTest / TestFoo -> Foo    via 'naming'
"""
import logging
import posixpath
import re
from typing import Any, Dict, List, Optional

from .base import EnrichmentContext, annotation, annotation_names, annotation_value, as_list, strip_class_literal

logger = logging.getLogger(__name__)

TEST_METHOD_ANNOTATIONS = {'Test', 'ParameterizedTest', 'RepeatedTest', 'TestFactory', 'TestTemplate'}
KINDS = [
    ('SpringBootTest', 'integration'),
    ('WebMvcTest', 'web_slice'), ('WebFluxTest', 'web_slice'),
    ('DataJpaTest', 'data_slice'), ('DataJdbcTest', 'data_slice'), ('JdbcTest', 'data_slice'),
    ('DataMongoTest', 'data_slice'), ('DataR2dbcTest', 'data_slice'), ('DataRedisTest', 'data_slice'),
    ('JsonTest', 'slice'), ('RestClientTest', 'slice'), ('JooqTest', 'slice'), ('GraphQlTest', 'slice'),
]
MOCK_ANNOTATIONS = ('MockBean', 'MockitoBean', 'SpyBean', 'MockitoSpyBean', 'Mock', 'Spy')
SUBJECT_ANNOTATIONS = ('InjectMocks', 'Autowired')
_NAMING = re.compile(r'^(?:Test(?=[A-Z]))?(\w+?)(?:Tests?|IT|ITCase|IntegrationTests?|UnitTests?|Spec)?$')


def _is_test_path(path: str) -> bool:
    parts = path.lower().split('/')
    return 'test' in parts or 'tests' in parts or 'it' in parts or 'integrationtest' in parts


def applies(context: EnrichmentContext) -> bool:
    return any(_is_test_path(p) for p in context.analyses if p.endswith('.java')) or any(
        set(annotation_names(e.get('attributes'))) & TEST_METHOD_ANNOTATIONS for _, e in context.java_declarations())


class _TargetCollector:
    """Records the classes under test of one test class (first `via` wins)."""

    def __init__(self, context: EnrichmentContext, path: str, test_class: str, targets: Dict[str, str]):
        self.context, self.path, self.test_class, self.targets = context, path, test_class, targets

    def __call__(self, name: Optional[str], via: str) -> None:
        resolved = self.context.resolve(name, self.path) if name else None
        if resolved and resolved != self.test_class and self.context.declared(resolved)                 and resolved not in self.targets:
            self.targets[resolved] = via


def enrich(context: EnrichmentContext) -> None:
    test_classes: Dict[str, Dict[str, Any]] = {}
    for type_name, decl in context.java_types.items():
        if decl.get('type') not in ('class', 'record') or (decl.get('attributes') or {}).get('outer'):
            continue
        members = context.members_of(type_name)
        test_methods = [m for m in members if set(annotation_names(m.get('attributes'))) & TEST_METHOD_ANNOTATIONS]
        if not test_methods and not (_is_test_path(decl['_path']) and type_name.endswith(('Test', 'Tests', 'IT'))):
            continue
        test_classes[type_name] = {'decl': decl, 'members': members, 'test_methods': test_methods}

    non_test_types = {name: decl for name, decl in context.java_types.items()
                      if name not in test_classes and not _is_test_path(decl['_path'])}
    by_simple: Dict[str, List[str]] = {}
    for name in non_test_types:
        by_simple.setdefault(name.rsplit('.', 1)[-1], []).append(name)

    for type_name, info in test_classes.items():
        decl, members = info['decl'], info['members']
        attributes = decl.get('attributes') or {}
        names = annotation_names(attributes)
        kind = next((k for a, k in KINDS if a in names), 'unit')
        targets: Dict[str, str] = {}
        add_target = _TargetCollector(context, decl['_path'], type_name, targets)

        for annotation_name in ('WebMvcTest', 'WebFluxTest', 'RestClientTest'):
            item = annotation(attributes, annotation_name)
            for value in as_list(annotation_value(item, 'value', 'controllers', 'components')):
                add_target(strip_class_literal(value), 'annotation')

        mocks = []
        for member in members:
            member_attributes = member.get('attributes') or {}
            if member.get('type') != 'variable':
                continue
            type_text = member_attributes.get('type_resolved') or member.get('dtype')
            if annotation(member_attributes, *MOCK_ANNOTATIONS):
                mocks.append(type_text)
            elif annotation(member_attributes, *SUBJECT_ANNOTATIONS):
                add_target(type_text, 'subject')
        constructors = [m for m in members if m.get('type') == 'constructor']
        if kind != 'unit':
            for constructor in constructors:
                for param in (constructor.get('attributes') or {}).get('params') or []:
                    add_target(param.get('type_resolved') or param.get('type'), 'subject')

        match = _NAMING.match(type_name.rsplit('.', 1)[-1])
        if match and match.group(1) != type_name.rsplit('.', 1)[-1]:
            candidates = by_simple.get(match.group(1), [])
            package = (attributes.get('package') or '')
            same_package = [c for c in candidates if c.rsplit('.', 1)[0] == package]
            chosen = same_package if len(same_package) == 1 else candidates if len(candidates) == 1 else []
            for candidate in chosen:
                add_target(candidate, 'naming')

        test_class = context.registry.lookup(decl['name']) or type_name
        test_info = context.framework_attributes(test_class, 'test')
        test_info.update({
            'kind': kind,
            'annotations': [n for n in names if n in dict(KINDS) or n.startswith(('AutoConfigure', 'Testcontainers',
                                                                                   'ExtendWith', 'Import'))],
            'test_methods': len(info['test_methods']),
            'mocks': [m for m in mocks if m],
            'targets': list(targets),
            'location': posixpath.dirname(decl['_path']),
        })
        for target, via in targets.items():
            context.add_edge(test_class, target, 'tests', via=via)
            context.framework_attributes(target, 'test').setdefault('tested_by', []).append(test_class)
    context.shared['test_classes'] = list(test_classes)
