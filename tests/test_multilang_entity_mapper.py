"""
EntityChunkMapper support for C, C++ and Java (comment stripping, scope resolution, language detection).
"""
import pytest

from repo_knowledge_graph.entity_chunk_mapper import EntityChunkMapper


def _find(name, chunks, entity_type, file_name):
    return EntityChunkMapper().find_entity_in_chunks(name, chunks, entity_type=entity_type, file_name=file_name)


def test_c_comments():
    chunks = [
        """
// calculate_sum is defined elsewhere
int helper() {
    return 0;
}
""",
        """
/* This function calculates the sum
   using calculate_sum helper
*/
int calculate_sum(int a, int b) {
    return a + b;
}
""",
        """
int main() {
    int result = calculate_sum(5, 10);
    return 0;
}
""",
    ]
    # chunk 0 only mentions the name in a // comment
    assert _find('calculate_sum', chunks, 'function', 'test.c') == {1, 2}


def test_cpp_class():
    chunks = [
        """
// The DataProcessor class handles data
void helper() {}
""",
        """
class DataProcessor {
public:
    void process();
};
""",
        """
void main() {
    DataProcessor dp;
    dp.process();
}
""",
    ]
    assert _find('DataProcessor', chunks, 'class', 'test.cpp') == {1, 2}


def test_cpp_method():
    chunks = [
        """
class MyClass {
    void doSomething();
};
""",
        """
// Implementation of doSomething
void MyClass::doSomething() {
    // Do work
}
""",
        """
int main() {
    MyClass obj;
    obj.doSomething();
}
""",
    ]
    # The in-class declaration (chunk 0), the out-of-line definition (1) and the call (2)
    assert _find('MyClass::doSomething', chunks, 'method', 'test.cpp') == {0, 1, 2}


def test_java_multiline_comments():
    chunks = [
        """
/**
 * The processData method is very important
 * It uses processData internally
 */
public void helper() {
    return;
}
""",
        """
public void processData(String data) {
    System.out.println(data);
}
""",
        """
public void main() {
    processData("Hello");
}
""",
    ]
    # chunk 0 only mentions the name in javadoc
    assert _find('processData', chunks, 'method', 'Test.java') == {1, 2}


def test_java_class():
    chunks = [
        """
// Employee class is defined below
public void helper() {}
""",
        """
public class Employee {
    private String name;

    public Employee(String name) {
        this.name = name;
    }
}
""",
        """
public class Main {
    public static void main(String[] args) {
        Employee emp = new Employee("John");
    }
}
""",
    ]
    assert _find('Employee', chunks, 'class', 'Employee.java') == {1, 2}


def test_c_struct():
    chunks = [
        """
/* Point struct definition */
struct Point {
    int x;
    int y;
};
""",
        """
void movePoint(struct Point* p, int dx, int dy) {
    p->x += dx;
    p->y += dy;
}
""",
        """
int main() {
    struct Point p = {0, 0};
    movePoint(&p, 10, 20);
}
""",
    ]
    # Definition (0), parameter type (1) and local variable type (2)
    assert _find('Point', chunks, 'class', 'point.c') == {0, 1, 2}


def test_mixed_comments():
    chunks = [
        """
// Single-line comment about compute
/* Multi-line comment
   also mentioning compute
*/
int helper() { return 0; }
""",
        """
int compute(int x) {
    return x * 2;
}
""",
        """
int main() {
    return compute(5);
}
""",
    ]
    assert _find('compute', chunks, 'function', 'test.cpp') == {1, 2}


@pytest.mark.parametrize('file_name, expected', [
    ('test.py', 'PYTHON'),
    ('test.c', 'C'),
    ('test.h', 'C'),
    ('test.cpp', 'CPP'),
    ('test.hpp', 'CPP'),
    ('test.cc', 'CPP'),
    ('test.cxx', 'CPP'),
    ('test.java', 'JAVA'),
    ('test.js', 'GENERIC'),
    ('test.ts', 'GENERIC'),
    ('test.rs', 'GENERIC'),
    ('notes.txt', 'PYTHON'),  # unknown extension -> default
    (None, 'PYTHON'),         # default
])
def test_language_detection(file_name, expected):
    assert EntityChunkMapper()._detect_language(file_name).name == expected
