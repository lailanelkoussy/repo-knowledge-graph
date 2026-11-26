"""
Java extraction with tree-sitter: modern syntax, attributes, positions, type-resolved calls, Lombok,
REST mappings, and the registry fixes that come with it.
"""
import pytest

from repo_knowledge_graph.entity_extractor import HybridEntityExtractor, JavaEntityExtractor
from repo_knowledge_graph.entity_registry import EntityRegistry
from repo_knowledge_graph.java_extractor import TreeSitterJavaEntityExtractor, combine_paths, javadoc_summary
from repo_knowledge_graph.utils.path_utils import generate_entity_aliases


def _extract(code, path='src/main/java/com/acme/Foo.java'):
    declared, called = HybridEntityExtractor().extract_entities(code, path)
    return {e['name']: e for e in declared}, called


MODERN = '''package com.acme;

import java.util.List;

/** Shapes. Second sentence. */
public sealed interface Shape permits Circle, Square {
    double area();
}

record Circle(double radius) implements Shape {
    public double area() { return Math.PI * radius * radius; }
}

final class Square implements Shape {
    private final double side;
    Square(double side) { this.side = side; }
    public double area() { return side * side; }

    String describe(Object o) {
        String text = """
            A square
            """;
        if (o instanceof Square s && s.side > 0) {
            return text;
        }
        return switch ((int) side) {
            case 1 -> "unit";
            default -> "other";
        };
    }
}

enum Color {
    RED, GREEN;
    Color next() { return GREEN; }
}
'''


def test_default_extractor_is_tree_sitter():
    assert isinstance(JavaEntityExtractor(), TreeSitterJavaEntityExtractor)


def test_modern_java_syntax_is_parsed():
    entities, _ = _extract(MODERN)
    assert entities['com.acme.Shape']['type'] == 'interface'
    assert entities['com.acme.Shape']['attributes']['permits'] == ['com.acme.Circle', 'com.acme.Square']
    assert entities['com.acme.Shape']['attributes']['javadoc'] == 'Shapes.'
    assert entities['com.acme.Circle']['type'] == 'record'
    assert entities['com.acme.Circle::radius']['attributes']['record_component'] is True
    assert entities['com.acme.Circle']['bases'] == ['com.acme.Shape']
    assert entities['com.acme.Square::describe']['type'] == 'method'
    assert entities['com.acme.Color']['type'] == 'enum'
    assert entities['com.acme.Color::RED']['type'] == 'enum_constant'
    assert entities['com.acme.Color::next']['type'] == 'method'  # enum bodies are visited


def test_positions_and_signature():
    entities, _ = _extract(MODERN)
    describe = entities['com.acme.Square::describe']
    assert describe['start_line'] == 19 and describe['end_line'] == 30
    assert describe['attributes']['signature'] == 'describe(Object)'
    assert describe['attributes']['return_type'] == 'String'


def test_interface_bases_are_recorded():
    entities, _ = _extract('package a; interface A extends B, java.io.Serializable {}')
    assert entities['a.A']['bases'] == ['B', 'java.io.Serializable']


def test_syntax_error_keeps_the_rest_of_the_file():
    entities, _ = _extract('package a;\nclass Ok { void f() {} }\nclass Broken { void g( { }\n')
    assert 'a.Ok::f' in entities


TYPED = '''package com.acme.web;

import com.acme.service.OrderService;
import static com.acme.util.Strings.trim;

public class OrderController {
    private final OrderService service;
    private Helper helper;

    public OrderController(OrderService service) { this.service = service; }

    public void handle(Long id, Mapper mapper) {
        service.find(id);
        this.service.save(id);
        var local = new Helper();
        local.help();
        mapper.map(id);
        trim("x");
        render();
        unknown.call();
        String.valueOf(id);
    }

    private void render() {}
}
'''


def test_calls_are_resolved_with_the_receiver_type():
    entities, called = _extract(TYPED, 'src/main/java/com/acme/web/OrderController.java')
    calls = entities['com.acme.web.OrderController::handle']['attributes']['calls']
    assert 'com.acme.service.OrderService::find' in calls      # field, imported type
    assert 'com.acme.service.OrderService::save' in calls      # this.field
    assert 'Helper.help' in calls                               # var local = new Helper()
    assert 'Mapper.map' in calls                                # parameter, same package
    assert 'com.acme.util.Strings::trim' in calls               # static import
    assert 'com.acme.web.OrderController::render' in calls      # method of the class
    assert 'unknown.call' in calls                              # unknown receiver: raw form
    assert not any(c.startswith('java.lang') for c in calls)    # JDK calls are not recorded
    assert 'com.acme.service.OrderService::find' in called


def test_invocations_keep_literal_arguments():
    code = '''package a;
class P { void f(Sender s) { s.send("topic-a", new Event()); s.send(Topics.B, x); } }'''
    entities, _ = _extract(code)
    invocations = [i for i in entities['a.P::f']['attributes']['invocations'] if i['member'] == 'send']
    assert invocations[0]['args'][:2] == ['topic-a', {'new': 'Event'}]
    assert invocations[1]['args'][0] == {'ref': '{{Topics.B}}'}
    constructor = next(i for i in entities['a.P::f']['attributes']['invocations'] if i['member'] == '<init>')
    assert constructor['receiver_type'] == 'Event'  # constructors are recorded as '<init>' invocations


def test_lombok_accessors_are_synthesized():
    code = '''package a;
import lombok.Data;
@Data
public class Item {
    private String name;
    private boolean active;
    private static int COUNT;
    public String getName() { return name; }
}'''
    entities, _ = _extract(code)
    assert entities['a.Item::isActive']['attributes']['synthetic'] is True
    assert entities['a.Item::setName']['attributes']['lombok'] == 'Setter'
    assert 'synthetic' not in entities['a.Item::getName']['attributes']  # explicit method kept
    assert 'a.Item::getCOUNT' not in entities


def test_rest_mappings():
    code = '''package a;
import org.springframework.web.bind.annotation.*;
@RestController
@RequestMapping(Api.BASE)
class C {
    static final String BASE_ITEMS = "/items";
    @GetMapping public String all() { return ""; }
    @GetMapping({"/x", "/y"}) public String two() { return ""; }
    @RequestMapping(value = BASE_ITEMS, method = {RequestMethod.PUT, RequestMethod.PATCH}) public void up() {}
    @RequestMapping("/any") public void any() {}
}'''
    entities, _ = _extract(code)
    assert entities['a.C::all']['endpoint'] == '{{Api.BASE}}'  # constant from another file, resolved later
    assert [e['path'] for e in entities['a.C::two']['attributes']['endpoints']] == ['{{Api.BASE}}/x', '{{Api.BASE}}/y']
    assert entities['a.C::up']['methods'] == ['PUT', 'PATCH']
    assert entities['a.C::up']['endpoint'] == '{{Api.BASE}}/items'
    assert entities['a.C::any']['methods'] == ['ANY']


def test_annotation_arguments():
    code = '''package a;
@Table(name = "orders", uniqueConstraints = {})
class E {
    @Column(name = "c", nullable = false, length = 10) private String c;
    @Value("${app.x:1}") private int x;
}'''
    entities, _ = _extract(code)
    table = entities['a.E']['attributes']['annotations'][0]
    assert table['name'] == 'Table' and table['args']['name'] == 'orders'
    column = entities['a.E::c']['attributes']['annotations'][0]['args']
    assert column == {'name': 'c', 'nullable': False, 'length': 10}
    assert entities['a.E::x']['attributes']['annotations'][0]['args']['value'] == '${app.x:1}'


def test_helpers():
    assert combine_paths('/api/', '/users') == '/api/users'
    assert combine_paths('', '') == '/'
    assert combine_paths('api', '') == '/api'
    assert javadoc_summary('/**\n * Returns the order.\n * More.\n * @param id id\n */') == 'Returns the order.'


def test_javalang_fallback(monkeypatch):
    from repo_knowledge_graph.settings import get_settings
    monkeypatch.setenv('JAVA_PARSER', 'javalang')
    get_settings.cache_clear()
    try:
        extractor = JavaEntityExtractor()
        assert type(extractor).__name__ == 'JavalangEntityExtractor'
        declared, _ = extractor.extract_entities('package a; class B { void f() {} }')
        assert any(e['name'] == 'a.B' for e in declared)
    finally:
        monkeypatch.delenv('JAVA_PARSER')
        get_settings.cache_clear()


def test_same_named_methods_of_different_classes_are_not_merged():
    registry = EntityRegistry()
    first = registry.register_declared({'name': 'com.a.OrderService::find', 'type': 'method',
                                        'aliases': generate_entity_aliases('com.a.OrderService::find', 'com/a/OrderService.java')}, 'x_0')
    second = registry.register_declared({'name': 'com.b.UserService::find', 'type': 'method',
                                         'aliases': generate_entity_aliases('com.b.UserService::find', 'com/b/UserService.java')}, 'y_0')
    assert first != second
    # 'find' is now ambiguous: it is not resolved to one of them arbitrarily
    assert registry.resolve('find') is None
    assert registry.resolve('find', imports=['com.b.UserService']) == 'com.b.UserService::find'


def test_external_qualified_calls_do_not_resolve_by_simple_name():
    registry = EntityRegistry()
    registry.register_declared({'name': 'com.a.Dto::status', 'type': 'variable', 'aliases': ['com.a.Dto::status']}, 'x_0')
    assert registry.resolve('org.springframework.Matchers::status') is None
    assert registry.resolve('com.a.Dto::status') == 'com.a.Dto::status'


@pytest.mark.parametrize('code', [
    'package a; class A { void f() { int x = switch (1) { case 1 -> 2; default -> 3; }; } }',
    'package a; record R(int x) { R { if (x < 0) throw new IllegalArgumentException(); } }',
    'package a; class A { String s = """\n  hi\n  """; }',
])
def test_java_17_21_constructs_do_not_fail(code):
    entities, _ = _extract(code)
    assert entities
