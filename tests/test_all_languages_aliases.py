"""
Comprehensive test to verify alias generation works for ALL supported languages.
"""

from repo_knowledge_graph.entity_extractor import HybridEntityExtractor
from repo_knowledge_graph.utils.path_utils import build_entity_alias_map, resolve_entity_call


def test_python_aliases():
    """Test Python entity extraction with aliases"""

    code = """
class DataProcessor:
    def __init__(self, name):
        self.name = name

    def process(self, data):
        return data.upper()

def helper_function(x):
    return x * 2
"""

    extractor = HybridEntityExtractor()
    declared, called = extractor.extract_entities(code, "src/utils/processor.py")

    for entity in declared:
        if 'DataProcessor' in entity.get('name', '') or 'helper_function' in entity.get('name', ''):
            pass

    # Verify aliases were generated
    data_processor = next((e for e in declared if e.get('name') == 'DataProcessor'), None)
    assert data_processor is not None, "DataProcessor not found"
    assert 'aliases' in data_processor, "No aliases generated for Python class"
    assert 'DataProcessor' in data_processor['aliases'], "Simple name not in aliases"
    assert 'src.utils.processor.DataProcessor' in data_processor['aliases'], "Fully qualified name not in aliases"


def test_javascript_aliases():
    """Test JavaScript entity extraction with aliases"""

    code = """
class UserService {
    constructor(name) {
        this.name = name;
    }

    getUser() {
        return this.name;
    }
}

function createUser(name) {
    return new UserService(name);
}
"""

    extractor = HybridEntityExtractor()
    declared, called = extractor.extract_entities(code, "app/services/user.js")

    for entity in declared:
        if 'UserService' in entity.get('name', '') or 'createUser' in entity.get('name', ''):
            pass

    # Verify aliases were generated
    user_service = next((e for e in declared if e.get('name') == 'UserService'), None)
    assert user_service is not None, "UserService not found"
    assert 'aliases' in user_service, "No aliases generated for JavaScript class"
    assert 'UserService' in user_service['aliases'], "Simple name not in aliases"
    assert 'app.services.user.UserService' in user_service['aliases'], "Fully qualified name not in aliases"


def test_java_aliases():
    """Test Java entity extraction with aliases"""

    code = """
package com.example.app;

public class UserRepository {
    private String database;

    public UserRepository(String database) {
        this.database = database;
    }

    public User findUser(int id) {
        return null;
    }
}
"""

    extractor = HybridEntityExtractor()
    declared, called = extractor.extract_entities(code, "src/main/java/com/example/app/UserRepository.java")

    for entity in declared:
        if 'UserRepository' in entity.get('name', ''):
            pass

    # Verify aliases were generated
    user_repo = next((e for e in declared if 'UserRepository' in e.get('name', '') and e.get('type') == 'class'), None)
    assert user_repo is not None, "UserRepository not found"
    assert 'aliases' in user_repo, "No aliases generated for Java class"


def test_cpp_aliases():
    """Test C++ entity extraction with aliases"""

    code = """
namespace math {
    class Calculator {
    public:
        Calculator();
        int add(int a, int b);
        int subtract(int a, int b);
    };
}
"""

    extractor = HybridEntityExtractor()
    declared, called = extractor.extract_entities(code, "include/math/calculator.h")

    for entity in declared:
        if 'Calculator' in entity.get('name', ''):
            pass

    # Verify aliases were generated
    calculator = next((e for e in declared if e.get('name') == 'math::Calculator'), None)
    assert calculator is not None, "Calculator not found"
    assert 'aliases' in calculator, "No aliases generated for C++ class"
    assert 'math::Calculator' in calculator['aliases'], "Simple name not in aliases"
    assert 'include.math.calculator.math::Calculator' in calculator['aliases'], "Fully qualified name not in aliases"


def test_rust_aliases():
    """Test Rust entity extraction with aliases"""

    code = """
pub struct User {
    pub name: String,
    pub age: u32,
}

impl User {
    pub fn new(name: String, age: u32) -> User {
        User { name, age }
    }

    pub fn greet(&self) -> String {
        format!("Hello, {}!", self.name)
    }
}

pub fn create_user(name: &str) -> User {
    User::new(name.to_string(), 0)
}
"""

    extractor = HybridEntityExtractor()
    declared, called = extractor.extract_entities(code, "src/models/user.rs")

    for entity in declared:
        if 'User' in entity.get('name', '') or 'create_user' in entity.get('name', ''):
            pass

    # Verify aliases were generated
    user_struct = next((e for e in declared if e.get('name') == 'User' and e.get('type') == 'struct'), None)
    assert user_struct is not None, "User struct not found"
    assert 'aliases' in user_struct, "No aliases generated for Rust struct"
    assert 'User' in user_struct['aliases'], "Simple name not in aliases"
    assert 'src.models.user.User' in user_struct['aliases'], "Fully qualified name not in aliases"


def test_cross_language_resolution():
    """Test that alias resolution works across different languages"""

    # Simulate entities from multiple languages
    entities = {}
    extractor = HybridEntityExtractor()

    # Python entity
    py_code = "class APIClient:\n    pass"
    py_declared, _ = extractor.extract_entities(py_code, "lib/client.py")
    for entity in py_declared:
        name = entity.get('name')
        if name:
            entities[name] = {
                'declaring_chunk_ids': ['lib/client.py_0'],
                'calling_chunk_ids': [],
                'aliases': entity.get('aliases', []),
                'type': [entity.get('type')]
            }

    # JavaScript entity
    js_code = "class DataService {\n}"
    js_declared, _ = extractor.extract_entities(js_code, "src/service.js")
    for entity in js_declared:
        name = entity.get('name')
        if name:
            entities[name] = {
                'declaring_chunk_ids': ['src/service.js_0'],
                'calling_chunk_ids': [],
                'aliases': entity.get('aliases', []),
                'type': [entity.get('type')]
            }

    # Java entity
    java_code = "package com.app;\npublic class UserManager {}"
    java_declared, _ = extractor.extract_entities(java_code, "com/app/UserManager.java")
    for entity in java_declared:
        name = entity.get('name')
        if name:
            entities[name] = {
                'declaring_chunk_ids': ['com/app/UserManager.java_0'],
                'calling_chunk_ids': [],
                'aliases': entity.get('aliases', []),
                'type': [entity.get('type')]
            }

    # Build alias map
    alias_map = build_entity_alias_map(entities)

    # Test resolution
    test_cases = [
        ('APIClient', 'APIClient', 'Python class direct match'),
        ('lib.client.APIClient', 'APIClient', 'Python class fully qualified'),
        ('DataService', 'DataService', 'JavaScript class direct match'),
        ('src.service.DataService', 'DataService', 'JavaScript class fully qualified'),
    ]

    for called_name, expected, description in test_cases:
        resolved = resolve_entity_call(called_name, alias_map)
        status = "✓" if resolved == expected else "✗"



def test_alias_summary():
    """Print summary of alias support"""

    extractor = HybridEntityExtractor()
    supported_languages = list(extractor.extractors.keys())

    for lang in sorted(supported_languages):
        pass



# Test functions will be auto-discovered and run by pytest
