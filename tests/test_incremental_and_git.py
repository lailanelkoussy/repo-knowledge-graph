"""
Incremental updates and git history.
"""
import shutil
import subprocess

import pytest

from repo_knowledge_graph.git_history import validate_revision
from repo_knowledge_graph.knowledge_graph import RepoKnowledgeGraph

pytestmark = pytest.mark.skipif(shutil.which('git') is None, reason='git not installed')


def git(repo, *args):
    subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.com', '-C', str(repo), *args],
                   check=True, capture_output=True)


@pytest.fixture
def git_repo(mini_repo):
    git(mini_repo, 'init', '-q')
    git(mini_repo, 'add', '.')
    git(mini_repo, 'commit', '-q', '-m', 'initial import')
    (mini_repo / 'shop' / 'cart.py').write_text(
        (mini_repo / 'shop' / 'cart.py').read_text() + '\n\ndef empty_cart(cart):\n    cart.items.clear()\n')
    git(mini_repo, 'commit', '-q', '-am', 'add empty_cart')
    return mini_repo


def test_update_only_reanalyses_changed_files(mini_repo, fake_model_service):
    kg = RepoKnowledgeGraph.from_path(str(mini_repo), extract_entities=True, index_nodes=True,
                                      model_service=fake_model_service,
                                      code_index_kwargs={'index_type': 'hybrid'})
    models_chunk = kg['shop/models.py_0']
    embedding_before = list(models_chunk.embedding)

    (mini_repo / 'shop' / 'cart.py').write_text(
        (mini_repo / 'shop' / 'cart.py').read_text() + '\n\ndef empty_cart(cart):\n    cart.items.clear()\n')
    (mini_repo / 'shop' / 'coupon.py').write_text('from shop.cart import Cart\n\nclass Coupon(Cart):\n    pass\n')
    (mini_repo / 'README.md').unlink()

    summary = kg.update_from_path()
    assert summary['modified'] == ['shop/cart.py']
    assert summary['added'] == ['shop/coupon.py']
    assert summary['deleted'] == ['README.md']
    assert 'shop/models.py' in summary['unchanged']

    assert kg['shop/models.py_0'] is models_chunk          # unchanged analysis reused as-is
    assert kg['shop/models.py_0'].embedding == embedding_before
    assert 'README.md' not in kg.graph
    assert 'empty_cart' in kg.entities
    assert ('Coupon', 'Cart') in {(u, v) for u, v, d in kg.graph.edges(data=True) if d['relation'] == 'inherits'}
    assert 'shop/cart.py' in [r.path for r in kg.code_index.search('empty_cart', 6)]


def test_update_without_changes_is_a_noop(mini_repo):
    kg = RepoKnowledgeGraph.from_path(str(mini_repo), index_nodes=False)
    graph_before = kg.graph
    summary = kg.update_from_path()
    assert not summary['added'] and not summary['modified'] and not summary['deleted']
    assert kg.graph is graph_before


def test_update_after_reload_from_file(mini_repo, tmp_path):
    kg = RepoKnowledgeGraph.from_path(str(mini_repo), index_nodes=False, extract_entities=True)
    kg.save_graph_to_file(str(tmp_path / 'g.json'))
    loaded = RepoKnowledgeGraph.load_graph_from_file(str(tmp_path / 'g.json'), index_nodes=False)
    (mini_repo / 'shop' / 'models.py').write_text('class Item:\n    pass\n')
    summary = loaded.update_from_path()
    assert summary['modified'] == ['shop/models.py']
    assert 'DiscountedItem' not in loaded.entities


def test_update_refuses_a_missing_directory(mini_repo, tmp_path):
    kg = RepoKnowledgeGraph.from_path(str(mini_repo), index_nodes=False)
    with pytest.raises(ValueError):
        kg.update_from_path(str(tmp_path / 'deleted_clone'))
    assert kg.get_all_files()


def test_git_history_nodes(git_repo):
    kg = RepoKnowledgeGraph.from_path(str(git_repo), index_nodes=False, include_git_history=True)
    commits = [n for n in kg if n.node_type == 'commit']
    assert len(commits) == 2
    history = kg.get_file_history('shop/cart.py')
    assert [c.message for c in history] == ['add empty_cart', 'initial import']
    assert [c.message for c in kg.get_file_history('shop/models.py')] == ['initial import']
    assert kg.metadata['git_revision'] == history[0].commit_hash


def test_git_history_survives_incremental_update(git_repo):
    kg = RepoKnowledgeGraph.from_path(str(git_repo), index_nodes=False, include_git_history=True)
    (git_repo / 'shop' / 'models.py').write_text('class Item:\n    pass\n')
    kg.update_from_path()
    assert len(kg.get_file_history('shop/cart.py')) == 2


def test_history_is_skipped_outside_git(mini_repo):
    kg = RepoKnowledgeGraph.from_path(str(mini_repo), index_nodes=False, include_git_history=True)
    assert not [n for n in kg if n.node_type == 'commit']


@pytest.mark.parametrize('revision', ['HEAD', 'HEAD~1', 'main', 'v1.0', 'abc123', 'feature/x', 'HEAD^'])
def test_valid_revisions(revision):
    assert validate_revision(revision) == revision


@pytest.mark.parametrize('revision', ['', '--output=/tmp/x', '-p', 'a..b', 'HEAD; rm -rf /', 'a b'])
def test_invalid_revisions(revision):
    with pytest.raises(ValueError):
        validate_revision(revision)
