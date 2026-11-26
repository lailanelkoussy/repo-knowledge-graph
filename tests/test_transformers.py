"""
End-to-end test on a real GitHub repository: clone, build, save / load (with and without embeddings), query.

Needs network access (git clone): set KG_NETWORK_TESTS=1 to run it. Embeddings use the conftest
FakeModelService unless KG_REAL_MODELS=1.
"""
import os

import pytest

from repo_knowledge_graph.knowledge_graph import RepoKnowledgeGraph

REPO_URL = 'https://github.com/lailanelkoussy/streamlit-fastapi-github-authentification'
MODEL_SERVICE_KWARGS = {'embedder_type': 'sentence-transformers',
                        'embed_model_name': 'Salesforce/SFR-Embedding-Code-400M_R'}


@pytest.mark.network
@pytest.mark.skipif(os.getenv('KG_NETWORK_TESTS') != '1', reason='set KG_NETWORK_TESTS=1 to run network tests')
def test_repo_knowledge_graph_with_transformers(tmp_path):
    knowledge_graph = RepoKnowledgeGraph.from_repo(
        repo_url=REPO_URL, index_nodes=False, describe_nodes=False, extract_entities=True,
        model_service_kwargs=MODEL_SERVICE_KWARGS, code_index_kwargs={'index_type': 'keyword-only'},
        clone_dir=str(tmp_path / 'clone'))

    chunks = knowledge_graph.get_all_chunks()
    assert chunks, "the cloned repository should produce chunks"
    assert knowledge_graph.get_all_files()
    assert knowledge_graph.entities, "entity extraction should find entities"
    first = chunks[0]
    assert knowledge_graph.get_neighbors(first.id)

    # save / load without embeddings
    first_file = tmp_path / 'knowledge_graph.json'
    knowledge_graph.save_graph_to_file(filepath=str(first_file))
    loaded = RepoKnowledgeGraph.load_graph_from_file(
        filepath=str(first_file), index_nodes=True, use_embed=False,
        model_service_kwargs=MODEL_SERVICE_KWARGS, code_index_kwargs={'index_type': 'hybrid'})
    loaded_chunks = {c.id: c for c in loaded.get_all_chunks()}
    assert set(loaded_chunks) == {c.id for c in chunks}
    assert loaded_chunks[first.id].declared_entities == first.declared_entities
    assert loaded_chunks[first.id].called_entities == first.called_entities
    assert loaded.graph.number_of_edges() == knowledge_graph.graph.number_of_edges()

    # save the loaded graph again and reload it with embeddings
    second_file = tmp_path / 'knowledge_graph_1.json'
    loaded.save_graph_to_file(filepath=str(second_file))
    loaded_2 = RepoKnowledgeGraph.load_graph_from_file(
        filepath=str(second_file), index_nodes=True, use_embed=True,
        model_service_kwargs=MODEL_SERVICE_KWARGS, code_index_kwargs={'index_type': 'hybrid'})
    assert len(loaded_2.get_all_chunks()) == len(chunks)
    assert loaded_2.graph.number_of_nodes() == knowledge_graph.graph.number_of_nodes()

    results = loaded_2.code_index.query('function', n_results=3)
    assert 'metadatas' in results
    hits = results['metadatas'][0]
    assert 0 < len(hits) <= 3
    for hit in hits:
        assert hit['id'] in loaded_chunks
        assert {'content', 'declared_entities', 'called_entities'} <= set(hit)
