"""
C++ entity extraction (libclang): namespaces, classes, methods, calls, and resolution in the graph.
"""
import pytest

from repo_knowledge_graph.entity_extractor import CppEntityExtractor
from repo_knowledge_graph.knowledge_graph import RepoKnowledgeGraph

pytestmark = pytest.mark.clang


def _by_name(declared):
    return {e['name']: e for e in declared}


def test_cpp_entity_extraction():
    """Namespace + class + method + free function + calls."""
    cpp_code = """
#include <iostream>
namespace NS {
    class Foo {
    public:
        void bar(int x) { std::cout << x; }
    };

    void baz() {
        Foo f;
        f.bar(42);
    }
}
"""
    declared, called = CppEntityExtractor().extract_entities(cpp_code)
    entities = _by_name(declared)

    assert entities['NS::Foo']['type'] == 'class'
    assert entities['NS::Foo::bar']['type'] == 'method'
    assert entities['NS::Foo::bar.x'] == {'name': 'NS::Foo::bar.x', 'type': 'variable', 'dtype': 'int'}
    assert entities['NS::baz']['type'] == 'function'
    assert entities['NS::f']['type'] == 'variable'
    # include directive, type reference of `Foo f;` and the method call
    assert called[0] == 'iostream'
    assert {'NS::Foo', 'bar'} <= set(called)


def test_cpp_entity_extraction_with_namespace():
    """Declarations only (header): class, constructor and methods are namespace-qualified."""
    cpp_code = """
namespace utils {
    class Calculator {
    public:
        Calculator();
        int add(int a, int b);
        int multiply(int a, int b);
    };
}
"""
    declared, called = CppEntityExtractor().extract_entities(cpp_code)
    entities = _by_name(declared)

    assert entities['utils::Calculator']['type'] == 'class'
    for method in ('utils::Calculator::Calculator', 'utils::Calculator::add', 'utils::Calculator::multiply'):
        assert entities[method]['type'] == 'method'
    assert entities['utils::Calculator::add.a']['dtype'] == 'int'
    assert called == []


def test_cpp_called_entities_contain_no_empty_names():
    code = """
#include "math/calculator.h"

using namespace math;

int main() {
    Calculator calc;
    return calc.add(1, 2);
}
"""
    _, called = CppEntityExtractor().extract_entities(code)
    assert 'math/calculator.h' in called
    assert '' not in called


def test_using_namespace_call_resolves_to_namespaced_declaration(tmp_path):
    """`using namespace math; Calculator calc;` links main.cpp to math::Calculator in the graph."""
    (tmp_path / 'math').mkdir()
    (tmp_path / 'math' / 'calculator.h').write_text("""
namespace math {
    class Calculator {
    public:
        int add(int a, int b);
    };
}
""", encoding='utf-8')
    (tmp_path / 'main.cpp').write_text("""
#include "math/calculator.h"

using namespace math;

int main() {
    Calculator calc;  // resolved by libclang to math::Calculator
    return calc.add(1, 2);
}
""", encoding='utf-8')

    kg = RepoKnowledgeGraph.from_path(str(tmp_path), extract_entities=True, index_nodes=False)

    calculator = kg.entities['math::Calculator']
    assert calculator['declaring_chunk_ids'] == ['math/calculator.h_0']
    assert calculator['calling_chunk_ids'] == ['main.cpp_0']
    assert 'Calculator' in calculator['aliases']
    assert kg.entities['math::Calculator::add']['calling_chunk_ids'] == ['main.cpp_0']
    assert kg.entities['calc']['dtype'] == 'Calculator'
    # No separate entity is created for the unqualified name
    assert 'Calculator' not in kg.entities
    assert kg.graph.edges['math/calculator.h_0', 'math::Calculator']['relation'] == 'declares'
    assert kg.graph.edges['math::Calculator', 'main.cpp_0']['relation'] == 'called_by'
