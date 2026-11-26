"""
Entity aliases derived from the (repository-relative) file path.
"""
import pytest

from repo_knowledge_graph.entity_extractor import HybridEntityExtractor
from repo_knowledge_graph.utils.path_utils import file_path_to_module_path, generate_entity_aliases


@pytest.mark.parametrize('file_path, expected', [
    ('path/to/repo/python_script.py', 'path.to.repo.python_script'),
    ('src/utils/helper.py', 'src.utils.helper'),
    ('module.py', 'module'),
    ('deep/nested/path/to/file.py', 'deep.nested.path.to.file'),
    ('src\\windows\\style.py', 'src.windows.style'),
])
def test_path_to_module_conversion(file_path, expected):
    assert file_path_to_module_path(file_path) == expected


@pytest.mark.parametrize('entity_name, file_path, fully_qualified', [
    ('Class_1', 'path/to/repo/python_script.py', 'path.to.repo.python_script.Class_1'),
    ('my_function', 'src/utils/helper.py', 'src.utils.helper.my_function'),
    ('MyClass.my_method', 'app/models/user.py', 'app.models.user.MyClass.my_method'),
])
def test_entity_alias_generation(entity_name, file_path, fully_qualified):
    aliases = generate_entity_aliases(entity_name, file_path)

    assert aliases[0] == entity_name
    assert aliases[-1] == fully_qualified
    assert len(aliases) == len(set(aliases))
    # Progressive suffixes of the module path: '.helper.my_function', '.utils.helper.my_function', ...
    module_parts = file_path_to_module_path(file_path).split('.')
    for i in range(1, len(module_parts) + 1):
        assert '.' + '.'.join(module_parts[-i:]) + '.' + entity_name in aliases


def test_cpp_namespaced_alias_generation():
    aliases = generate_entity_aliases('math::Calculator', 'math/calculator.h')
    assert aliases[0] == 'math::Calculator'
    assert 'Calculator' in aliases
    assert aliases[-1] == 'math.calculator.math::Calculator'


def test_temporary_path_components_are_skipped_in_partial_aliases():
    aliases = generate_entity_aliases('pos', 'tmp/tmptqky4yk4/pyinstaller/run_tests.py')
    assert '.run_tests.pos' in aliases
    assert '.pyinstaller.run_tests.pos' in aliases
    # 'tmp' itself is kept, the random 'tmptqky4yk4' component is dropped from the clean path
    assert 'tmp.pyinstaller.run_tests.pos' in aliases
    assert not any('tmptqky4yk4' in a for a in aliases[:-1])
    assert aliases[-1] == 'tmp.tmptqky4yk4.pyinstaller.run_tests.pos'


def _by_name(declared):
    return {e['name']: e for e in declared}


def test_python_entity_extraction_with_aliases():
    code = """
class MyClass:
    def __init__(self, value):
        self.value = value

    def my_method(self):
        return self.value

def my_function(x, y):
    return x + y

CONSTANT = 42
"""
    declared, _ = HybridEntityExtractor().extract_entities(code, 'src/module/test.py')
    entities = _by_name(declared)

    expected = {
        'MyClass': 'class',
        'MyClass.__init__': 'method',
        'MyClass.my_method': 'method',
        'my_function': 'function',
        'CONSTANT': 'variable',
    }
    for name, entity_type in expected.items():
        assert entities[name]['type'] == entity_type
        aliases = entities[name]['aliases']
        assert aliases[0] == name
        assert f'src.module.test.{name}' in aliases
        assert f'.test.{name}' in aliases
    assert entities['CONSTANT']['dtype'] == 'int'


def test_javascript_entity_extraction_with_aliases():
    code = """
class UserController {
    constructor(name) {
        this.name = name;
    }

    getUser() {
        return this.name;
    }
}

function createUser(name) {
    return new UserController(name);
}

const API_URL = 'http://api.example.com';
"""
    declared, _ = HybridEntityExtractor().extract_entities(code, 'app/controllers/user.js')
    entities = _by_name(declared)

    expected = {
        'UserController': 'class',
        'UserController.constructor': 'method',
        'UserController.getUser': 'method',
        'createUser': 'function',
        'API_URL': 'variable',
    }
    for name, entity_type in expected.items():
        assert entities[name]['type'] == entity_type
        assert entities[name]['aliases'][0] == name
        assert f'app.controllers.user.{name}' in entities[name]['aliases']


def test_java_entity_extraction_with_aliases():
    code = """
package com.example.app;

import java.util.List;

public class UserService {
    private String name;

    public UserService(String name) {
        this.name = name;
    }

    public String getName() {
        return this.name;
    }

    public void updateName(String newName) {
        this.name = newName;
    }
}
"""
    declared, called = HybridEntityExtractor().extract_entities(
        code, 'src/main/java/com/example/app/UserService.java')
    entities = _by_name(declared)

    # Java entities are named after the package, members use '::'
    assert entities['com.example.app.UserService']['type'] == 'class'
    assert entities['com.example.app.UserService::UserService']['type'] == 'constructor'
    assert entities['com.example.app.UserService::getName']['type'] == 'method'
    assert entities['com.example.app.UserService::updateName']['type'] == 'method'
    assert entities['com.example.app.UserService::name']['dtype'] == 'String'

    get_name_aliases = entities['com.example.app.UserService::getName']['aliases']
    assert get_name_aliases[0] == 'com.example.app.UserService::getName'
    assert 'getName' in get_name_aliases
    for entity in declared:
        assert entity['aliases'][0] == entity['name']
        assert entity['aliases'][-1].startswith('src.main.java.com.example.app.UserService.')
    assert 'java.util.List' in called
