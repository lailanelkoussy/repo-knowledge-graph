"""
Regression tests for fixed bugs:
chunk embedding text, LLM call signature / retry policy, MCP regex tool,
GitHub token handling, LanceDB query safety, code parser fallback.
"""
import hashlib
import os
import shutil
import subprocess
from types import SimpleNamespace

import networkx as nx
import pytest

from repo_knowledge_graph.node import ChunkNode, FileNode, Node

# --------------------------------------------------------------------------- helpers

class FakeModelService:
    """Deterministic embeddings without downloading any model."""
    dim = 16

    def _vec(self, text: str) -> list:
        digest = hashlib.sha256(text.encode()).digest()
        return [b / 255.0 for b in digest[:self.dim]]

    def embed_query(self, text):
        return self._vec(text)

    def embed_chunk_code_batch(self, texts):
        return [self._vec(t) for t in texts]


def make_chunk(i: int, path: str, content: str, description: str = '') -> ChunkNode:
    return ChunkNode(id=f'{path}_{i}', name=f'{path}_{i}', path=path, content=content,
                     order_in_file=i, description=description, language='python')


# --------------------------------------------------------------------------- 1. embeddings

def test_chunk_embedding_text_contains_code_without_description():
    chunk = make_chunk(0, 'pkg/a.py', 'def foo():\n    return 1')
    text = chunk.get_field_to_embed()
    assert 'def foo()' in text
    assert 'pkg/a.py' in text


def test_chunk_embedding_text_differs_between_chunks():
    a = make_chunk(0, 'a.py', 'def foo(): pass')
    b = make_chunk(1, 'a.py', 'class Bar: pass')
    assert a.get_field_to_embed() != b.get_field_to_embed()


def test_chunk_embedding_text_includes_description_when_present():
    chunk = make_chunk(0, 'a.py', 'def foo(): pass', description='Returns nothing.')
    text = chunk.get_field_to_embed()
    assert 'Returns nothing.' in text and 'def foo()' in text


# --------------------------------------------------------------------------- 2. LLM calls

def _openai_service(monkeypatch, create):
    from repo_knowledge_graph.model_service import OpenAIModelService
    service = OpenAIModelService(model_name='test-model')
    fake_completions = SimpleNamespace(create=create)
    monkeypatch.setattr(service, 'async_client', SimpleNamespace(chat=SimpleNamespace(completions=fake_completions)))
    return service


@pytest.mark.asyncio
async def test_query_async_works_without_model_name(monkeypatch):
    calls = []

    async def create(model, messages):
        calls.append(model)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='summary'))])

    service = _openai_service(monkeypatch, create)
    assert await service.query_async('Summarize this') == 'summary'
    assert calls == ['test-model']


@pytest.mark.asyncio
async def test_query_async_does_not_retry_programming_errors(monkeypatch):
    calls = []

    async def create(model, messages):
        calls.append(model)
        raise ValueError('bad request')

    service = _openai_service(monkeypatch, create)
    with pytest.raises(ValueError):
        await service.query_async('Summarize this')
    assert len(calls) == 1


# --------------------------------------------------------------------------- 3. MCP regex tool

def _fake_knowledge_graph():
    from repo_knowledge_graph.knowledge_graph import RepoKnowledgeGraph
    kg = RepoKnowledgeGraph.__new__(RepoKnowledgeGraph)
    kg.graph = nx.DiGraph()
    kg.entities = {}
    kg.graph.add_node('root', data=Node(id='root', name='root', node_type='root'))
    for path in ('src/main.py', 'src/utils.py', 'README.md'):
        kg.graph.add_node(path, data=FileNode(id=path, name=os.path.basename(path), path=path))
    return kg


@pytest.mark.asyncio
async def test_search_file_names_by_regex_returns_matches():
    from fastmcp import Client

    from repo_knowledge_graph.mcp_server import KnowledgeGraphMCPServer

    server = KnowledgeGraphMCPServer(knowledge_graph=_fake_knowledge_graph())
    async with Client(server.app) as client:
        result = await client.call_tool('search_file_names_by_regex', {'pattern': r'\.py$'})

    data = result.structured_content
    assert data is not None
    assert data['count'] == 2
    assert {m['node_id'] for m in data['matches']} == {'src/main.py', 'src/utils.py'}


# --------------------------------------------------------------------------- 4. GitHub token

def test_redact_url_hides_credentials():
    from repo_knowledge_graph.knowledge_graph import _redact_url
    redacted = _redact_url('https://user:s3cr3t@github.com/owner/repo.git')
    assert 's3cr3t' not in redacted and 'user' not in redacted
    assert redacted == 'https://***@github.com/owner/repo.git'
    assert _redact_url('https://github.com/owner/repo.git') == 'https://github.com/owner/repo.git'


def test_git_auth_env_keeps_token_out_of_url_and_args():
    import base64

    from repo_knowledge_graph.knowledge_graph import _git_auth_env
    env = _git_auth_env('tok123')
    header = env['GIT_CONFIG_VALUE_0']
    assert env['GIT_CONFIG_KEY_0'] == 'http.extraHeader'
    assert base64.b64decode(header.split()[-1]).decode() == 'x-access-token:tok123'
    assert 'GIT_CONFIG_COUNT' not in _git_auth_env(None)


@pytest.mark.skipif(shutil.which('git') is None, reason='git not installed')
def test_git_clone_with_token_does_not_persist_it(tmp_path):
    from repo_knowledge_graph.knowledge_graph import _git_clone
    src = tmp_path / 'src'
    src.mkdir()
    (src / 'a.py').write_text('x = 1\n')
    git = ['git', '-c', 'user.name=t', '-c', 'user.email=t@t', '-C', str(src)]
    subprocess.run(git + ['init', '-q'], check=True)
    subprocess.run(git + ['add', '.'], check=True)
    subprocess.run(git + ['commit', '-q', '-m', 'init'], check=True)

    dest = tmp_path / 'dest'
    _git_clone(str(src), str(dest), token='tok123')
    assert (dest / 'a.py').exists()
    assert 'tok123' not in (dest / '.git' / 'config').read_text()


# --------------------------------------------------------------------------- 5. LanceDB

@pytest.fixture
def lance_chunks():
    return [
        make_chunk(0, 'shop/cart.py', "def add_item(cart, item):\n    cart.append(item)"),
        make_chunk(1, 'shop/cart.py', "def customer_name():\n    return \"O'Brien\""),
        make_chunk(0, 'shop/pay.py', "class PaymentGateway:\n    def charge(self, amount):\n        pass"),
    ]


@pytest.mark.parametrize('index_type', ['keyword-only', 'embedding-only', 'hybrid'])
@pytest.mark.parametrize('query', ["O'Brien", "x' OR '1'='1", 'PaymentGateway charge'])
def test_lancedb_queries_are_safe(tmp_path, lance_chunks, index_type, query):
    from repo_knowledge_graph.code_index import CodeIndex
    index = CodeIndex(lance_chunks, model_service=FakeModelService(), index_type=index_type,
                      backend='lancedb', db_path=str(tmp_path / 'db'))
    results = index.query(query, n_results=3)
    assert set(results) == {'ids', 'distances', 'metadatas', 'documents'}
    assert len(results['ids'][0]) <= 3


def test_lancedb_keyword_search_finds_exact_identifier(tmp_path, lance_chunks):
    from repo_knowledge_graph.code_index import CodeIndex
    index = CodeIndex(lance_chunks, model_service=FakeModelService(), index_type='keyword-only',
                      backend='lancedb', db_path=str(tmp_path / 'db'))
    results = index.query('PaymentGateway', n_results=3)
    assert results['ids'][0][0] == 'shop/pay.py_0'


def test_lancedb_hybrid_returns_results_without_literal_match(tmp_path, lance_chunks):
    """The old 'hybrid' mode post-filtered on LIKE '%query%' and dropped every non-literal match."""
    from repo_knowledge_graph.code_index import CodeIndex
    index = CodeIndex(lance_chunks, model_service=FakeModelService(), index_type='hybrid',
                      backend='lancedb', db_path=str(tmp_path / 'db'))
    results = index.query('how is money taken from the user', n_results=3)
    assert len(results['ids'][0]) > 0


# --------------------------------------------------------------------------- 6. code_parser

def test_code_parser_falls_back_to_single_chunk(monkeypatch):
    from repo_knowledge_graph import code_parser as code_parser_module

    def boom(*args, **kwargs):
        raise RuntimeError('splitter failure')

    monkeypatch.setattr(code_parser_module.RecursiveCharacterTextSplitter, 'from_language', boom)
    parser = code_parser_module.CodeParser()
    assert parser.parse('a.py', 'x = 1\n') == ['x = 1\n']
    assert parser.parse('a.py', '') == []
