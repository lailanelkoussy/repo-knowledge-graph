"""
Test to verify that alias resolution prevents duplicate entity entries in RepoKnowledgeGraph.
This ensures entities are not added multiple times when called with different names.
"""

import os
import tempfile

from repo_knowledge_graph.knowledge_graph import RepoKnowledgeGraph


def test_alias_resolution_prevents_duplicates():
    """Test that calling an entity by different names doesn't create duplicates"""

    # Create a temporary directory with sample code
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a Python module that defines a class
        module_path = os.path.join(tmpdir, "utils", "processor.py")
        os.makedirs(os.path.dirname(module_path), exist_ok=True)

        with open(module_path, 'w') as f:
            f.write("""
class DataProcessor:
    def __init__(self, name):
        self.name = name

    def process(self, data):
        return data.upper()

def helper_function(x):
    return x * 2
""")

        # Create another file that imports and uses the class
        main_path = os.path.join(tmpdir, "main.py")
        with open(main_path, 'w') as f:
            f.write("""
from utils.processor import DataProcessor
import utils.processor

# Using simple name (should resolve to DataProcessor)
processor1 = DataProcessor("test1")

# Using qualified name (should resolve to same DataProcessor)
processor2 = utils.processor.DataProcessor("test2")

# Using helper function
result = helper_function(5)
""")

        # Build knowledge graph
        kg = RepoKnowledgeGraph.from_path(
            path=tmpdir,
            extract_entities=True,
            index_nodes=False,
            describe_nodes=False,
            model_service_kwargs = {
        "embedder_type": "sentence-transformers",
        "embed_model_name": "Salesforce/SFR-Embedding-Code-400M_R",
    },
            code_index_kwargs={"index_type": "keyword-only"},
        )


        # Check for DataProcessor
        data_processor_entries = [
            name for name in kg.entities.keys()
            if 'DataProcessor' in name
        ]

        for name in data_processor_entries:
            info = kg.entities[name]

        # Verify that there's only ONE DataProcessor entity (not duplicates)
        data_processor_classes = [
            name for name in kg.entities.keys()
            if name == 'DataProcessor' or name.endswith('.DataProcessor')
        ]


        # There should be exactly ONE entry for the DataProcessor class
        canonical_entry = 'DataProcessor'
        assert canonical_entry in kg.entities, "DataProcessor not found in entities"

        dp_info = kg.entities[canonical_entry]

        # It should have declarations (defined in utils/processor.py)
        assert len(dp_info['declaring_chunk_ids']) > 0, "DataProcessor should have declaring chunks"

        # It should have calls (used in main.py)
        assert len(dp_info['calling_chunk_ids']) > 0, "DataProcessor should have calling chunks"

        # Check aliases
        aliases = dp_info.get('aliases', [])
        assert 'DataProcessor' in aliases, "Simple name should be in aliases"
        assert any('processor.DataProcessor' in a for a in aliases), "Qualified name should be in aliases"



def test_cross_file_resolution():
    """Test that entities are resolved across files"""

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a module with a class
        lib_path = os.path.join(tmpdir, "lib", "database.py")
        os.makedirs(os.path.dirname(lib_path), exist_ok=True)

        with open(lib_path, 'w') as f:
            f.write("""
class Connection:
    def connect(self):
        pass

    def query(self, sql):
        return []
""")

        # Create file that uses it
        app_path = os.path.join(tmpdir, "app", "service.py")
        os.makedirs(os.path.dirname(app_path), exist_ok=True)

        with open(app_path, 'w') as f:
            f.write("""
from lib.database import Connection

def get_data():
    conn = Connection()  # Called as 'Connection'
    return conn.query("SELECT * FROM users")
""")

        # Build knowledge graph
        kg = RepoKnowledgeGraph.from_path(
            path=tmpdir,
            extract_entities=True,
            index_nodes=False,
            describe_nodes=False,
            model_service_kwargs={
                "embedder_type": "sentence-transformers",
                "embed_model_name": "Salesforce/SFR-Embedding-Code-400M_R",
            },
            code_index_kwargs={"index_type": "keyword-only"},
        )

        # Check Connection entity
        connection_entities = [
            name for name in kg.entities.keys()
            if 'Connection' in name
        ]


        # Should only have ONE Connection entity
        assert 'Connection' in kg.entities, "Connection not found"

        conn_info = kg.entities['Connection']
        declaring_files = set(chunk_id.split('_')[0] for chunk_id in conn_info['declaring_chunk_ids'])
        calling_files = set(chunk_id.split('_')[0] for chunk_id in conn_info['calling_chunk_ids'])


        assert 'lib/database.py' in declaring_files or os.path.join(tmpdir, 'lib', 'database.py') in str(declaring_files), \
            "Connection should be declared in lib/database.py"
        assert any('service.py' in f for f in calling_files), \
            "Connection should be called in app/service.py"



def test_cpp_namespace_resolution():
    """Test that C++ namespace resolution works"""

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create header file
        header_path = os.path.join(tmpdir, "math", "calculator.h")
        os.makedirs(os.path.dirname(header_path), exist_ok=True)

        with open(header_path, 'w') as f:
            f.write("""
namespace math {
    class Calculator {
    public:
        int add(int a, int b);
    };
}
""")

        # Create implementation file that uses it
        impl_path = os.path.join(tmpdir, "main.cpp")
        with open(impl_path, 'w') as f:
            f.write("""
#include "math/calculator.h"

using namespace math;

int main() {
    Calculator calc;  // Called as 'Calculator', should resolve to 'math::Calculator'
    return calc.add(1, 2);
}
""")

        # Build knowledge graph
        kg = RepoKnowledgeGraph.from_path(
            path=tmpdir,
            extract_entities=True,
            index_nodes=False,
            describe_nodes=False,
            model_service_kwargs={
                "embedder_type": "sentence-transformers",
                "embed_model_name": "Salesforce/SFR-Embedding-Code-400M_R",
            },
            code_index_kwargs={"index_type": "keyword-only"},
        )

        # Check Calculator entity
        calculator_entities = [
            name for name in kg.entities.keys()
            if 'Calculator' in name
        ]


        # Should have math::Calculator
        assert 'math::Calculator' in kg.entities, "math::Calculator not found"

        calc_info = kg.entities['math::Calculator']

        # It should have both declarations and calls
        assert len(calc_info['declaring_chunk_ids']) > 0, "Should have declarations"
        assert len(calc_info['calling_chunk_ids']) > 0, "Should have calls"



# Test functions will be auto-discovered and run by pytest
