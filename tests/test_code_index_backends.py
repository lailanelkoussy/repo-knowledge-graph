"""
Behaviour of the CodeIndex factory and of the LanceDB backend (Weaviate tests need a server).
"""
import pytest

from repo_knowledge_graph.code_index import BaseCodeIndex, CodeIndex, LanceDBCodeIndex, SearchResult, WeaviateCodeIndex
from repo_knowledge_graph.node import ChunkNode


def _chunks():
    return [
        ChunkNode(id='a.py_0', name='a.py_0', path='a.py', content='def parse_config(path):\n    return {}',
                  order_in_file=0, declared_entities=[{'name': 'parse_config', 'type': 'function'}]),
        ChunkNode(id='b.py_0', name='b.py_0', path='b.py', content='class HttpClient:\n    def get(self): ...',
                  order_in_file=0, called_entities=['requests.get']),
    ]


def test_backends_share_the_base_class():
    assert issubclass(LanceDBCodeIndex, BaseCodeIndex)
    assert issubclass(WeaviateCodeIndex, BaseCodeIndex)


def test_default_backend_is_embedded_lancedb(tmp_path):
    index = CodeIndex(_chunks(), model_service=None, index_type='keyword-only', db_path=str(tmp_path))
    assert isinstance(index, LanceDBCodeIndex)


def test_unknown_backend_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        CodeIndex(_chunks(), model_service=None, index_type='keyword-only', backend='chroma')


def test_vector_index_requires_model_service(tmp_path):
    with pytest.raises(ValueError):
        CodeIndex(_chunks(), model_service=None, index_type='hybrid', db_path=str(tmp_path))


def test_keyword_search_returns_typed_results(tmp_path):
    index = CodeIndex(_chunks(), model_service=None, index_type='keyword-only', db_path=str(tmp_path))
    results = index.search('parse_config', n_results=5)
    assert results and isinstance(results[0], SearchResult)
    assert results[0].id == 'a.py_0'
    assert results[0].declared_entities == [{'name': 'parse_config', 'type': 'function'}]


def test_legacy_query_format_is_kept(tmp_path):
    index = CodeIndex(_chunks(), model_service=None, index_type='keyword-only', db_path=str(tmp_path))
    legacy = index.query('HttpClient', n_results=5)
    assert legacy['ids'][0][0] == 'b.py_0'
    assert legacy['metadatas'][0][0]['path'] == 'b.py'


def test_embeddings_are_computed_in_batches_and_stored_as_lists(tmp_path, fake_model_service):
    calls = []
    original = fake_model_service.embed_chunk_code_batch
    fake_model_service.embed_chunk_code_batch = lambda texts: calls.append(len(texts)) or original(texts)
    chunks = _chunks()
    CodeIndex(chunks, model_service=fake_model_service, index_type='hybrid', db_path=str(tmp_path),
              embedding_batch_size=1)
    assert calls == [1, 1]
    assert all(isinstance(c.embedding, list) and c.embedding for c in chunks)


def test_existing_index_is_reused(tmp_path, fake_model_service):
    first = CodeIndex(_chunks(), model_service=fake_model_service, index_type='hybrid', db_path=str(tmp_path))
    calls = []
    fake_model_service.embed_chunk_code_batch = lambda texts: calls.append(texts) or []
    second = CodeIndex(_chunks(), model_service=fake_model_service, index_type='hybrid', db_path=str(tmp_path))
    assert second.table_name == first.table_name
    assert calls == []  # nothing re-embedded
    assert second.search('HttpClient', 2)


def test_changed_content_gets_a_new_index_and_drop_removes_it(tmp_path, fake_model_service):
    first = CodeIndex(_chunks(), model_service=None, index_type='keyword-only', db_path=str(tmp_path))
    changed = _chunks()
    changed[0].content += '\n# changed'
    second = CodeIndex(changed, model_service=None, index_type='keyword-only', db_path=str(tmp_path))
    assert second.table_name != first.table_name
    second.drop()
    assert second.table_name not in first._table_names()


def test_empty_query_returns_nothing(tmp_path):
    index = CodeIndex(_chunks(), model_service=None, index_type='keyword-only', db_path=str(tmp_path))
    assert index.search('   ', 5) == []


@pytest.mark.weaviate
def test_weaviate_keyword_search():
    index = CodeIndex(_chunks(), model_service=None, index_type='keyword-only', backend='weaviate', reuse=False)
    try:
        assert index.search('parse_config', 5)[0].id == 'a.py_0'
    finally:
        index.drop()
        index.close()
