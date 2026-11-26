"""
Entities must not be duplicated when they are declared / called under a different name or alias.
"""
import pytest

from repo_knowledge_graph.knowledge_graph import RepoKnowledgeGraph


def _build(root, files):
    for rel, code in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code, encoding='utf-8')
    return RepoKnowledgeGraph.from_path(str(root), extract_entities=True, index_nodes=False,
                                        describe_nodes=False)


def _assert_no_shared_alias(kg):
    owner = {}
    duplicates = []
    for name, info in kg.entities.items():
        for alias in info.get('aliases', []):
            if alias in owner and owner[alias] != name:
                duplicates.append((alias, owner[alias], name))
            owner.setdefault(alias, name)
    assert not duplicates, f"aliases shared by several entities: {duplicates}"


def _relation(kg, source, target):
    assert kg.graph.has_edge(source, target), f"missing edge {source} -> {target}"
    return kg.graph.edges[source, target]['relation']


def test_no_duplicate_entities_with_aliases(tmp_path):
    kg = _build(tmp_path, {
        'module1.py': """
class MyClass:
    def __init__(self):
        self.value = 0

    def my_method(self):
        return self.value
""",
        'module2.py': """
from module1 import MyClass

def use_class():
    obj = MyClass()
    return obj.my_method()
""",
    })
    _assert_no_shared_alias(kg)

    # Exactly one canonical entity for MyClass, declared in module1 and called from module2
    owners = [name for name, info in kg.entities.items() if name == 'MyClass' or 'MyClass' in info['aliases']]
    assert owners == ['MyClass']
    my_class = kg.entities['MyClass']
    assert my_class['type'] == ['class']
    assert my_class['declaring_chunk_ids'] == ['module1.py_0']
    assert my_class['calling_chunk_ids'] == ['module2.py_0']
    assert 'module1.MyClass' in my_class['aliases']

    assert kg.entities['MyClass.my_method']['calling_chunk_ids'] == ['module2.py_0']
    assert kg.entities['obj']['dtype'] == 'MyClass'
    assert not [n for n in kg.entities if n.endswith('.MyClass') or n == 'my_method']

    assert _relation(kg, 'module1.py_0', 'MyClass') == 'declares'
    assert _relation(kg, 'MyClass', 'module2.py_0') == 'called_by'
    assert _relation(kg, 'module2.py', 'module1.py') == 'imports'


@pytest.mark.clang
def test_c_cpp_no_duplicate_entities(tmp_path):
    kg = _build(tmp_path, {
        'calculator.h': """
#ifndef CALCULATOR_H
#define CALCULATOR_H

class Calculator {
public:
    int add(int a, int b);
    int subtract(int a, int b);
};

#endif
""",
        'calculator.cpp': """
#include "calculator.h"

int Calculator::add(int a, int b) {
    return a + b;
}

int Calculator::subtract(int a, int b) {
    return a - b;
}
""",
        'main.cpp': """
#include "calculator.h"

int main() {
    Calculator calc;
    int result = calc.add(5, 3);
    return 0;
}
""",
    })
    _assert_no_shared_alias(kg)

    calculator = kg.entities['Calculator']
    assert calculator['type'] == ['class']
    assert calculator['declaring_chunk_ids'] == ['calculator.h_0']
    assert calculator['calling_chunk_ids'] == ['main.cpp_0']

    # Header declaration and out-of-line definition are merged into one entity per method
    add = kg.entities['add']
    assert sorted(add['declaring_chunk_ids']) == ['calculator.cpp_0', 'calculator.h_0']
    assert add['calling_chunk_ids'] == ['main.cpp_0']
    assert sorted(kg.entities['subtract']['declaring_chunk_ids']) == ['calculator.cpp_0', 'calculator.h_0']
    assert kg.entities['subtract']['calling_chunk_ids'] == []
    assert not [n for n in kg.entities if n.endswith('::add') or n.endswith('::subtract')]

    assert _relation(kg, 'Calculator', 'main.cpp_0') == 'called_by'
    assert _relation(kg, 'add', 'main.cpp_0') == 'called_by'


def test_entity_resolution_with_qualified_names(tmp_path):
    kg = _build(tmp_path, {
        'utils/helper.py': """
class Helper:
    def help(self):
        return "helping"
""",
        'main.py': """
from utils.helper import Helper

def main():
    h = Helper()
    return h.help()
""",
    })
    _assert_no_shared_alias(kg)

    helper_entities = [n for n, info in kg.entities.items() if 'Helper' in info['aliases']]
    assert helper_entities == ['Helper']
    helper = kg.entities['Helper']
    assert 'utils.helper.Helper' in helper['aliases']
    assert helper['declaring_chunk_ids'] == ['utils/helper.py_0']
    assert helper['calling_chunk_ids'] == ['main.py_0']
    assert kg.entities['Helper.help']['calling_chunk_ids'] == ['main.py_0']

    assert _relation(kg, 'utils/helper.py_0', 'Helper') == 'declares'
    assert _relation(kg, 'Helper', 'main.py_0') == 'called_by'
    assert _relation(kg, 'main.py', 'utils/helper.py') == 'imports'
