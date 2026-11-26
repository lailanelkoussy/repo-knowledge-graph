"""
Shared fixtures.

By default the test-suite is hermetic: `create_model_service` is replaced by a deterministic
`FakeModelService` (no model download, no LLM endpoint). Tests marked `real_models`, `weaviate`
or `llm` are skipped unless the corresponding environment is available.
"""
import hashlib
import os
import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / 'fixtures'
MINI_REPO = FIXTURES / 'mini_repo'
VULN_SHOP = FIXTURES / 'vuln_shop'  # deliberately vulnerable code, each flaw next to its safe variant
SPRING_SHOP = FIXTURES / 'spring_shop'  # Spring Boot 3 / Java 21 multi-module application


class FakeModelService:
    """Deterministic embeddings and canned LLM answers."""

    dim = 32
    embed_model_name = 'fake-embedder'
    model_name = 'fake-llm'

    def __init__(self, *args, **kwargs):
        self.queries = []

    def _vec(self, text: str) -> list:
        # Bag-of-words hashing: texts sharing words get similar vectors
        vec = [0.0] * self.dim
        for word in (text or '').lower().split():
            h = int(hashlib.md5(word.encode()).hexdigest(), 16)
            vec[h % self.dim] += 1.0
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        return [v / norm for v in vec]

    def embed(self, text):
        return self._vec(text)

    async def embed_async(self, text):
        return self._vec(text)

    def embed_query(self, text):
        return self._vec(text)

    def embed_chunk_code(self, text):
        return self._vec(text)

    def embed_batch(self, texts):
        return [self._vec(t) for t in texts]

    def embed_chunk_code_batch(self, texts):
        return [self._vec(t) for t in texts]

    def query(self, prompt, model_name=None):
        self.queries.append(prompt)
        return 'Fake description.'

    async def query_async(self, prompt, model_name=None):
        self.queries.append(prompt)
        return 'Fake description.'


@pytest.fixture(autouse=True)
def _hermetic_models(request, monkeypatch):
    if request.node.get_closest_marker('real_models') or os.getenv('KG_REAL_MODELS') == '1':
        return
    import repo_knowledge_graph.model_service as model_service_module
    monkeypatch.setattr(model_service_module, 'create_model_service', lambda **kwargs: FakeModelService())


@pytest.fixture(autouse=True)
def _isolated_index_dir(tmp_path, monkeypatch):
    """Every test gets its own LanceDB directory (no ./local_code_index_db left behind)."""
    monkeypatch.setenv('LANCEDB_PATH', str(tmp_path / 'lancedb'))
    from repo_knowledge_graph.settings import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def pytest_collection_modifyitems(config, items):
    skips = {
        'real_models': (os.getenv('KG_REAL_MODELS') != '1', 'set KG_REAL_MODELS=1 to download and run real models'),
        'weaviate': (not os.getenv('WEAVIATE_HOST'), 'set WEAVIATE_HOST to run Weaviate tests'),
        'llm': (not os.getenv('KG_LLM_TESTS'), 'set KG_LLM_TESTS=1 (and OPENAI_BASE_URL) to run LLM tests'),
    }
    try:
        from clang import cindex
        cindex.Index.create()
        clang_missing = False
    except Exception:
        clang_missing = True
    skips['clang'] = (clang_missing, 'libclang not available')
    for item in items:
        for marker, (skip, reason) in skips.items():
            if skip and item.get_closest_marker(marker):
                item.add_marker(pytest.mark.skip(reason=reason))


@pytest.fixture
def fake_model_service():
    return FakeModelService()


@pytest.fixture
def mini_repo(tmp_path) -> Path:
    """A writable copy of the multi-language fixture repository."""
    target = tmp_path / 'mini_repo'
    shutil.copytree(MINI_REPO, target)
    return target


@pytest.fixture
def spring_repo(tmp_path) -> Path:
    """A writable copy of the Spring Boot fixture application."""
    target = tmp_path / 'spring_shop'
    shutil.copytree(SPRING_SHOP, target)
    return target


@pytest.fixture(scope='module')
def spring_kg():
    """Graph of the Spring Boot fixture with every enricher (built once per test module, no search index)."""
    from repo_knowledge_graph.knowledge_graph import RepoKnowledgeGraph
    return RepoKnowledgeGraph.from_path(str(SPRING_SHOP), extract_entities=True, index_nodes=False, enrichers='auto')


@pytest.fixture
def mini_kg(mini_repo, fake_model_service):
    from repo_knowledge_graph.knowledge_graph import RepoKnowledgeGraph
    return RepoKnowledgeGraph.from_path(str(mini_repo), extract_entities=True, index_nodes=True,
                                        model_service=fake_model_service,
                                        code_index_kwargs={'index_type': 'hybrid', 'backend': 'lancedb'})
