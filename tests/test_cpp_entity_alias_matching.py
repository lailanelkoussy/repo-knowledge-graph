"""
C++ entity extraction with #include directives and alias-based matching of calls to declarations.
"""
import pytest

from repo_knowledge_graph.entity_extractor import HybridEntityExtractor
from repo_knowledge_graph.entity_registry import EntityRegistry
from repo_knowledge_graph.knowledge_graph import RepoKnowledgeGraph
from repo_knowledge_graph.utils.path_utils import (
    build_entity_alias_map,
    normalize_include_path,
    resolve_entity_call,
)

MATH_HEADER = """
#ifndef MATH_H
#define MATH_H

namespace utils {
    class Calculator {
    public:
        Calculator();
        int add(int a, int b);
        int multiply(int a, int b);
    };
}

#endif
"""

MAIN_CPP = """
#include "utils/math.h"
#include <iostream>
#include <vector>

using namespace utils;

int main() {
    Calculator calc;
    int result = calc.add(5, 3);
    std::cout << "Result: " << result << std::endl;

    std::vector<int> numbers;
    numbers.push_back(result);

    return 0;
}
"""


def _record(aliases, declaring=(), calling=(), types=()):
    return {'declaring_chunk_ids': list(declaring), 'calling_chunk_ids': list(calling),
            'aliases': list(aliases), 'type': list(types)}


# Declared entities only, as the graph builder sees them when resolving calls
DECLARED = {
    'utils::Calculator': _record(['utils::Calculator', 'Calculator', 'utils.math.utils::Calculator'],
                                 declaring=['utils/math.h_0'], types=['class']),
    'utils::Calculator::add': _record(['utils::Calculator::add', 'add', 'utils.math.utils::Calculator::add'],
                                      declaring=['utils/math.h_0'], types=['method']),
}


@pytest.mark.parametrize('include_path, expected', [
    ('<vector>', 'vector'),
    ('<iostream>', 'iostream'),
    ('"myheader.h"', 'myheader'),
    ('"utils/helper.h"', 'utils.helper'),
    ('<boost/algorithm/string.hpp>', 'boost.algorithm.string'),
])
def test_cpp_include_normalization(include_path, expected):
    assert normalize_include_path(include_path) == expected


@pytest.mark.clang
def test_cpp_header_declares_namespaced_entities_with_aliases():
    declared, called = HybridEntityExtractor().extract_entities(MATH_HEADER, 'utils/math.h')
    by_name = {e['name']: e for e in declared}

    assert by_name['utils::Calculator']['type'] == 'class'
    for method in ('utils::Calculator::Calculator', 'utils::Calculator::add', 'utils::Calculator::multiply'):
        assert by_name[method]['type'] == 'method'

    aliases = by_name['utils::Calculator']['aliases']
    assert aliases[0] == 'utils::Calculator'
    assert 'Calculator' in aliases                       # unqualified name, for 'using namespace'
    assert 'utils.math.utils::Calculator' in aliases     # module (file path) qualified name
    assert 'add' in by_name['utils::Calculator::add']['aliases']
    assert called == []


@pytest.mark.clang
def test_cpp_main_reports_include_directives_as_calls():
    declared, called = HybridEntityExtractor().extract_entities(MAIN_CPP, 'src/main.cpp')

    for include in ('utils/math.h', 'iostream', 'vector'):
        assert include in called
    names = {e['name'] for e in declared}
    assert {'main', 'calc', 'result'} <= names
    main = next(e for e in declared if e['name'] == 'main')
    assert main['type'] == 'function'
    assert 'src.main.main' in main['aliases']


def test_alias_map_resolves_simple_and_qualified_names():
    alias_map = build_entity_alias_map(DECLARED)

    assert resolve_entity_call('utils::Calculator', alias_map) == 'utils::Calculator'
    assert resolve_entity_call('Calculator', alias_map) == 'utils::Calculator'
    assert resolve_entity_call('utils.math.utils::Calculator', alias_map) == 'utils::Calculator'
    # A unique simple name is resolved even without context
    assert resolve_entity_call('add', alias_map) == 'utils::Calculator::add'
    assert resolve_entity_call('Calculator::add', alias_map) == 'utils::Calculator::add'
    assert resolve_entity_call('iostream', alias_map) is None
    assert resolve_entity_call('', alias_map) is None
    assert resolve_entity_call('   ', alias_map) is None


def test_exact_canonical_name_wins_over_fuzzy_match():
    # A called-only record named 'Calculator' maps to itself: exact matches are tried first.
    entities = dict(DECLARED, Calculator=_record([], calling=['src/main.cpp_0']))
    alias_map = build_entity_alias_map(entities)
    assert resolve_entity_call('Calculator', alias_map) == 'Calculator'


def test_ambiguous_simple_name_is_not_guessed():
    # Two declared 'add' methods and no bare 'add' alias: the simple name is ambiguous
    entities = {
        'utils::Calculator::add': _record(['utils::Calculator::add', 'utils.math.utils::Calculator::add'],
                                          declaring=['utils/math.h_0'], types=['method']),
        'geometry::Vector::add': _record(['geometry::Vector::add', 'geometry.vector.geometry::Vector::add'],
                                         declaring=['geometry/vector.h_0'], types=['method']),
    }
    alias_map = build_entity_alias_map(entities)

    assert resolve_entity_call('add', alias_map) is None
    assert resolve_entity_call('other::add', alias_map) is None
    # The qualified call disambiguates
    assert resolve_entity_call('Vector::add', alias_map) == 'geometry::Vector::add'
    assert resolve_entity_call('Calculator::add', alias_map) == 'utils::Calculator::add'
    # ... and so do the imports of the calling file
    assert resolve_entity_call('add', alias_map, imports=['geometry.vector']) == 'geometry::Vector::add'
    assert resolve_entity_call('add', alias_map, imports=['utils.math']) == 'utils::Calculator::add'
    assert resolve_entity_call('add', alias_map, imports=['unrelated']) is None


def test_first_registered_alias_wins_in_alias_map():
    entities = {
        'utils::Calculator::add': _record(['utils::Calculator::add', 'add'], declaring=['utils/math.h_0']),
        'geometry::Vector::add': _record(['geometry::Vector::add', 'add'], declaring=['geometry/vector.h_0']),
    }
    alias_map = build_entity_alias_map(entities)
    assert alias_map['add'] == 'utils::Calculator::add'
    assert resolve_entity_call('add', alias_map) == 'utils::Calculator::add'


def test_registry_resolves_calls_and_refuses_ambiguity():
    registry = EntityRegistry()
    registry.register_declared({'name': 'utils::Calculator', 'type': 'class',
                                'aliases': ['utils::Calculator', 'Calculator']}, 'utils/math.h_0')
    registry.register_declared({'name': 'utils::Calculator::add', 'type': 'method',
                                'aliases': ['utils.math.utils::Calculator::add']}, 'utils/math.h_0')
    registry.register_declared({'name': 'geometry::Vector::add', 'type': 'method',
                                'aliases': ['geometry.vector.geometry::Vector::add']}, 'geometry/vector.h_0')

    assert registry.register_called('Calculator', 'src/main.cpp_0') == 'utils::Calculator'
    assert registry.entities['utils::Calculator']['calling_chunk_ids'] == ['src/main.cpp_0']
    # Registering the same call twice does not duplicate the link
    registry.register_called('Calculator', 'src/main.cpp_0')
    assert registry.entities['utils::Calculator']['calling_chunk_ids'] == ['src/main.cpp_0']

    assert registry.ambiguous_candidates('add') == ['geometry::Vector::add', 'utils::Calculator::add']
    assert registry.resolve('add') is None
    assert registry.resolve('add', imports=['utils.math']) == 'utils::Calculator::add'
    assert registry.resolve('Vector::add') == 'geometry::Vector::add'

    # An unresolved call becomes its own (called-only) record
    assert registry.register_called('add', 'src/main.cpp_0') == 'add'
    assert registry.entities['add']['declaring_chunk_ids'] == []
    assert registry.entities['add']['calling_chunk_ids'] == ['src/main.cpp_0']


def _write(root, files):
    for rel, code in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code, encoding='utf-8')


@pytest.mark.clang
def test_full_workflow_same_directory_include(tmp_path):
    _write(tmp_path, {
        'math/calculator.h': """
namespace math {
    class Calculator {
    public:
        int add(int a, int b);
        int subtract(int a, int b);
    };
}
""",
        'math/app.cpp': """
#include "calculator.h"

int main() {
    math::Calculator calc;
    int result = calc.add(10, 20);
    return 0;
}
"""})
    kg = RepoKnowledgeGraph.from_path(str(tmp_path), extract_entities=True, index_nodes=False)

    calculator = kg.entities['math::Calculator']
    assert calculator['declaring_chunk_ids'] == ['math/calculator.h_0']
    assert calculator['calling_chunk_ids'] == ['math/app.cpp_0']
    assert 'math.calculator.math::Calculator' in calculator['aliases']
    assert kg.entities['math::Calculator::add']['calling_chunk_ids'] == ['math/app.cpp_0']
    assert kg.entities['math::Calculator::subtract']['calling_chunk_ids'] == []
    assert kg.graph.has_edge('math::Calculator', 'math/app.cpp_0')
    assert kg.graph.edges['math::Calculator', 'math/app.cpp_0']['relation'] == 'called_by'


@pytest.mark.clang
def test_full_workflow_repo_root_relative_include(tmp_path):
    _write(tmp_path, {
        'math/calculator.h': """
namespace math {
    class Calculator {
    public:
        int add(int a, int b);
    };
}
""",
        'src/app.cpp': """
#include "math/calculator.h"

int main() {
    math::Calculator calc;
    int result = calc.add(10, 20);
    return 0;
}
"""})
    kg = RepoKnowledgeGraph.from_path(str(tmp_path), extract_entities=True, index_nodes=False)

    assert kg.entities['math::Calculator']['declaring_chunk_ids'] == ['math/calculator.h_0']
    assert kg.entities['math::Calculator']['calling_chunk_ids'] == ['src/app.cpp_0']
    assert kg.entities['math::Calculator::add']['calling_chunk_ids'] == ['src/app.cpp_0']
