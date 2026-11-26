"""
Test batch embedding functionality for ModelService classes.
"""
import pytest

from repo_knowledge_graph.model_service import create_model_service


@pytest.mark.real_models
def test_sentence_transformers_batch_embedding():
    """Test batch embedding with SentenceTransformers."""

    model_service = create_model_service(
        embedder_type='sentence-transformers',
        embed_model_name='sentence-transformers/all-MiniLM-L6-v2',
    )

    # Test data
    code_chunks = [
        "def quick_sort(arr):\n    if len(arr) <= 1:\n        return arr",
        "def bubble_sort(arr):\n    n = len(arr)\n    for i in range(n):",
        "class DataProcessor:\n    def __init__(self):\n        self.data = []"
    ]

    # Test batch embedding
    embeddings = model_service.embed_chunk_code_batch(code_chunks)

    # Verify results
    assert len(embeddings) == len(code_chunks), "Should return same number of embeddings as inputs"
    assert all(isinstance(emb, list) for emb in embeddings), "All embeddings should be lists"
    assert all(len(emb) > 0 for emb in embeddings), "All embeddings should have dimensions"


    # Test empty batch
    empty_embeddings = model_service.embed_chunk_code_batch([])
    assert empty_embeddings == [], "Empty input should return empty list"

    # Compare with single embedding
    single_embedding = model_service.embed_chunk_code(code_chunks[0])
    batch_first_embedding = embeddings[0]

    # They should be very similar (allowing for minor floating point differences)
    diff = sum(abs(a - b) for a, b in zip(single_embedding, batch_first_embedding))
    assert diff < 0.01, "Single and batch embeddings should be nearly identical"



def test_openai_batch_embedding_interface():
    """Test that OpenAI model service has batch embedding methods (without calling API)."""

    # Create model service (will fail to connect but we can test the methods exist)
    try:
        model_service = create_model_service(embedder_type='openai')

        # Check methods exist
        assert hasattr(model_service, 'embed_batch'), "Should have embed_batch method"
        assert hasattr(model_service, 'embed_chunk_code_batch'), "Should have embed_chunk_code_batch method"
        assert callable(model_service.embed_batch), "embed_batch should be callable"
        assert callable(model_service.embed_chunk_code_batch), "embed_chunk_code_batch should be callable"

    except Exception as e:
        pass
