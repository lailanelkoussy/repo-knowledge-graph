"""
Test script to verify EntityNode serialization and deserialization.
This test ensures that all EntityNode information is preserved when saving
and loading a knowledge graph from a file.
"""

import json
import os
import tempfile

from repo_knowledge_graph.knowledge_graph import RepoKnowledgeGraph
from repo_knowledge_graph.node import EntityNode


def test_entity_node_serialization():
    """Test that EntityNode data is fully preserved through save/load cycle"""

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create test files with entities that will be extracted
        test_py = os.path.join(tmpdir, "test.py")
        with open(test_py, 'w') as f:
            f.write("""
class MyClass:
    def __init__(self):
        self.value = 42

    def method_one(self):
        return self.value

    def method_two(self):
        # Call another method
        result = self.method_one()
        return result * 2

def standalone_function():
    # Create an instance and use it
    obj = MyClass()
    return obj.method_two()

def another_function():
    # Call the standalone function
    return standalone_function()
""")

        # Build graph with entity extraction enabled
        kg = RepoKnowledgeGraph.from_path(
            tmpdir,
            index_nodes=False,
            describe_nodes=False,
            extract_entities=True,
            model_service_kwargs={
                "embedder_type": "sentence-transformers",
                "embed_model_name": "Salesforce/SFR-Embedding-Code-400M_R",
            }
        )

        # Collect all EntityNode information from the original graph
        original_entity_nodes = {}
        for node_id, node_data in kg.graph.nodes(data=True):
            if 'data' not in node_data:
                continue
            node = node_data['data']
            if isinstance(node, EntityNode):
                original_entity_nodes[node_id] = {
                    'id': node.id,
                    'name': node.name,
                    'entity_type': node.entity_type,
                    'node_type': node.node_type,
                    'declaring_chunk_ids': list(node.declaring_chunk_ids),
                    'calling_chunk_ids': list(node.calling_chunk_ids),
                    'aliases': list(node.aliases),
                    'declared_entities': list(node.declared_entities),
                    'called_entities': list(node.called_entities),
                    'description': node.description,
                }


        # Print details of original EntityNodes
        for node_id, data in original_entity_nodes.items():
            pass

        # Save graph to file
        save_path = os.path.join(tmpdir, "test_graph.json")
        kg.save_graph_to_file(save_path)

        # Verify the saved file contains EntityNode data
        with open(save_path) as f:
            saved_data = json.load(f)

        entity_nodes_in_json = [
            node for node in saved_data['nodes']
            if node['class'] == 'EntityNode'
        ]

        # Check what fields are saved for EntityNodes
        if entity_nodes_in_json:
            sample_node = entity_nodes_in_json[0]

        # Load graph from file
        kg_loaded = RepoKnowledgeGraph.load_graph_from_file(
            save_path,
            index_nodes=False,
            use_embed=False,
            model_service_kwargs={
                "embedder_type": "sentence-transformers",
                "embed_model_name": "Salesforce/SFR-Embedding-Code-400M_R",
            }
        )

        # Collect all EntityNode information from the loaded graph
        loaded_entity_nodes = {}
        for node_id, node_data in kg_loaded.graph.nodes(data=True):
            if 'data' not in node_data:
                continue
            node = node_data['data']
            if isinstance(node, EntityNode):
                loaded_entity_nodes[node_id] = {
                    'id': node.id,
                    'name': node.name,
                    'entity_type': node.entity_type,
                    'node_type': node.node_type,
                    'declaring_chunk_ids': list(node.declaring_chunk_ids),
                    'calling_chunk_ids': list(node.calling_chunk_ids),
                    'aliases': list(node.aliases),
                    'declared_entities': list(node.declared_entities),
                    'called_entities': list(node.called_entities),
                    'description': node.description,
                }


        # Compare original and loaded EntityNodes

        # Check if same number of EntityNodes
        assert len(original_entity_nodes) == len(loaded_entity_nodes), \
            f"EntityNode count mismatch: original={len(original_entity_nodes)}, loaded={len(loaded_entity_nodes)}"

        # Check if all EntityNode IDs are preserved
        original_ids = set(original_entity_nodes.keys())
        loaded_ids = set(loaded_entity_nodes.keys())
        assert original_ids == loaded_ids, \
            f"EntityNode IDs mismatch:\n  Missing: {original_ids - loaded_ids}\n  Extra: {loaded_ids - original_ids}"

        # Compare each EntityNode field by field
        all_fields_match = True
        for node_id in original_entity_nodes:
            original = original_entity_nodes[node_id]
            loaded = loaded_entity_nodes[node_id]


            for field in ['id', 'name', 'entity_type', 'node_type', 'description']:
                if original[field] != loaded[field]:
                    all_fields_match = False
                else:
                    pass

            # For list fields, compare as sets (order doesn't matter)
            for field in ['declaring_chunk_ids', 'calling_chunk_ids', 'aliases']:
                original_set = set(original[field])
                loaded_set = set(loaded[field])
                if original_set != loaded_set:
                    all_fields_match = False
                else:
                    pass

            for field in ['declared_entities', 'called_entities']:
                # These are lists, compare them directly
                if original[field] != loaded[field]:
                    all_fields_match = False
                else:
                    pass

        assert all_fields_match, "Some EntityNode fields do not match between original and loaded graph"



def test_entity_node_with_aliases():
    """Test that EntityNode aliases are preserved"""

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create test file with qualified names that generate aliases
        test_py = os.path.join(tmpdir, "mymodule.py")
        with open(test_py, 'w') as f:
            f.write("""
class OuterClass:
    class InnerClass:
        def inner_method(self):
            pass

    def outer_method(self):
        inner = self.InnerClass()
        inner.inner_method()

def use_classes():
    obj = OuterClass()
    obj.outer_method()
""")

        # Build graph with entity extraction
        kg = RepoKnowledgeGraph.from_path(
            tmpdir,
            index_nodes=False,
            describe_nodes=False,
            extract_entities=True,
            model_service_kwargs={
                "embedder_type": "sentence-transformers",
                "embed_model_name": "Salesforce/SFR-Embedding-Code-400M_R",
            }
        )

        # Find EntityNodes with aliases
        entity_nodes_with_aliases = []
        for node_id, node_data in kg.graph.nodes(data=True):
            if 'data' not in node_data:
                continue
            node = node_data['data']
            if isinstance(node, EntityNode) and node.aliases:
                entity_nodes_with_aliases.append((node_id, node))

        if entity_nodes_with_aliases:
            # Save and load
            save_path = os.path.join(tmpdir, "test_aliases_graph.json")
            kg.save_graph_to_file(save_path)
            kg_loaded = RepoKnowledgeGraph.load_graph_from_file(
                save_path,
                index_nodes=False,
                use_embed=False,
                model_service_kwargs={
                    "embedder_type": "sentence-transformers",
                    "embed_model_name": "Salesforce/SFR-Embedding-Code-400M_R",
                }
            )

            # Check aliases are preserved
            for node_id, original_node in entity_nodes_with_aliases:
                loaded_node = kg_loaded[node_id]
                assert isinstance(loaded_node, EntityNode), \
                    f"Node {node_id} should be EntityNode, got {type(loaded_node)}"

                original_aliases = set(original_node.aliases)
                loaded_aliases = set(loaded_node.aliases)

                assert original_aliases == loaded_aliases, \
                    f"Aliases mismatch for {node_id}:\n  Original: {original_aliases}\n  Loaded: {loaded_aliases}"


        else:
            pass
