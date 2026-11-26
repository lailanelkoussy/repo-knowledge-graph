"""
CLI, TypeScript extraction, language table, settings, logging and observability helpers.
"""
import json
import logging

import pytest

from repo_knowledge_graph import cli
from repo_knowledge_graph.entity_extractor import HybridEntityExtractor, TypeScriptEntityExtractor
from repo_knowledge_graph.languages import get_extension, get_language_name, get_language_spec

# ---------------------------------------------------------------- CLI

def test_cli_build_and_update(mini_repo, tmp_path):
    output = tmp_path / 'graph.json'
    assert cli.main(['--log-level', 'WARNING', 'build', '--repo-path', str(mini_repo), '-o', str(output)]) == 0
    data = json.loads(output.read_text(encoding='utf-8'))
    assert data['schema_version'] == 3
    assert 'Cart' in data['entities']

    (mini_repo / 'shop' / 'extra.py').write_text('def extra():\n    return 1\n')
    assert cli.main(['--log-level', 'WARNING', 'build', '--update', str(output)]) == 0
    updated = json.loads(output.read_text(encoding='utf-8'))
    assert 'extra' in updated['entities']


def test_cli_requires_a_source(monkeypatch, tmp_path):
    for var in ('REPO_PATH', 'REPO_URL', 'GRAPH_FILE', 'FILE_NAME'):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(SystemExit):
        cli.main(['build', '-o', str(tmp_path / 'g.json')])


def test_cli_serve_arguments_from_environment(monkeypatch):
    monkeypatch.setenv('TRANSPORT', 'http')
    monkeypatch.setenv('PORT', '4321')
    monkeypatch.setenv('INDEX_TYPE', 'hybrid')
    args = cli.build_parser().parse_args(['serve', '--graph-file', 'g.json'])
    assert (args.transport, args.port, args.index_type, args.graph_file) == ('http', 4321, 'hybrid', 'g.json')


def test_cli_serve_loads_a_graph_file(mini_kg, tmp_path, monkeypatch):
    path = tmp_path / 'g.json'
    mini_kg.save_graph_to_file(str(path))
    captured = {}
    from repo_knowledge_graph.mcp_server import KnowledgeGraphMCPServer
    monkeypatch.setattr(KnowledgeGraphMCPServer, 'run', lambda self, **kw: captured.update(kw, server=self))
    assert cli.main(['serve', '--graph-file', str(path), '--transport', 'stdio']) == 0
    assert captured['transport'] == 'stdio'
    assert captured['server'].knowledge_graph.entities == mini_kg.entities


# ---------------------------------------------------------------- languages

@pytest.mark.parametrize('name, ext, language, extractor', [
    ('src/app.py', 'py', 'python', 'python'),
    ('a/b/c.tsx', 'tsx', 'typescript', 'typescript'),
    ('lib.rs', 'rs', 'rust', 'rust'),
    ('main.go', 'go', 'go', None),
    ('Makefile', '', '', None),
    ('.gitignore', '', '', None),
    ('dir.with.dots/file.HPP', 'hpp', 'c++', 'cpp'),
])
def test_language_table(name, ext, language, extractor):
    assert get_extension(name) == ext
    assert get_language_name(name) == language
    spec = get_language_spec(name)
    assert (spec.extractor if spec else None) == extractor


# ---------------------------------------------------------------- TypeScript

TS_CODE = '''
import { Router } from 'express';
import axios from 'axios';

export interface Shape { area(): number; }
export type Id = string;
export enum Color { Red, Green }

export abstract class Base {}

export class Circle extends Base implements Shape {
  radius: number = 1;
  area(): number { return Math.PI * this.radius ** 2; }
}

export const double = (x: number): number => x * 2;
const LIMIT: number = 10;

export async function load(): Promise<void> {
  const shape = new Circle();
  await axios.post('/api/shapes', shape);
  double(LIMIT);
}
'''


def test_typescript_extractor_declarations_calls_and_imports():
    extractor = TypeScriptEntityExtractor()
    declared, called = extractor.extract_entities(TS_CODE)
    by_name = {e['name']: e for e in declared}
    assert by_name['Shape']['type'] == 'interface'
    assert by_name['Id']['type'] == 'type'
    assert by_name['Color']['type'] == 'enum'
    assert by_name['Circle']['type'] == 'class'
    assert set(by_name['Circle']['bases']) == {'Base', 'Shape'}
    assert by_name['Circle.area']['type'] == 'method'
    assert by_name['double']['type'] == 'function'
    assert by_name['LIMIT']['type'] == 'variable' and by_name['LIMIT']['dtype'] == 'number'
    assert by_name['load']['type'] == 'function'
    assert {'Circle', 'double', 'API:POST:/api/shapes'} <= set(called)
    assert extractor.get_imports() == ['express', 'axios']


def test_hybrid_extractor_dispatch_and_unknown_languages():
    hybrid = HybridEntityExtractor()
    declared, called, imports = hybrid.extract_file_info('export function f(): void {}', 'src/x.ts')
    assert declared[0]['name'] == 'f' and 'src.x.f' in declared[0]['aliases']
    assert hybrid.extract_entities('whatever', 'notes.txt') == ([], [])
    assert hybrid.extract_file_info('package main', 'main.go') == ([], [], [])
    assert set(hybrid.extractors) == {'typescript'}  # only the needed extractor was created


def test_tsx_is_parsed():
    hybrid = HybridEntityExtractor()
    declared, _ = hybrid.extract_entities('export const App = () => <div>Hello</div>;', 'App.tsx')
    assert any(e['name'] == 'App' for e in declared)


# ---------------------------------------------------------------- settings, logging, observability

def test_settings_read_environment_lazily(monkeypatch):
    from repo_knowledge_graph.settings import get_settings
    monkeypatch.setenv('CODE_CHUNK_SIZE', '123')
    monkeypatch.setenv('GITHUB_OAUTH_TOKEN', 'legacy-token')
    monkeypatch.delenv('GITHUB_TOKEN', raising=False)
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.code_chunk_size == 123
    assert settings.github_token == 'legacy-token'
    assert 'legacy-token' not in repr(settings)


def test_library_does_not_configure_logging(tmp_path):
    """Importing and using the library must not create log files/dirs nor install handlers."""
    import os
    import subprocess
    import sys
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = ("import logging, repo_knowledge_graph.knowledge_graph, repo_knowledge_graph.code_index;"
            "h = logging.getLogger('repo_knowledge_graph').handlers;"
            "assert all(isinstance(x, logging.NullHandler) for x in h), h")
    env = dict(os.environ, PYTHONPATH=repo_root)
    subprocess.run([sys.executable, '-c', code], cwd=tmp_path, env=env, check=True)
    assert not (tmp_path / 'logs').exists()


def test_configure_logging_uses_stderr(capsys):
    from repo_knowledge_graph.utils.logger_utils import configure_logging
    configure_logging('INFO')
    logging.getLogger('repo_knowledge_graph.test').info('hello-from-test')
    captured = capsys.readouterr()
    assert 'hello-from-test' in captured.err and 'hello-from-test' not in captured.out
    configure_logging('WARNING')


def test_observability_is_a_noop_without_keys(monkeypatch):
    from repo_knowledge_graph import observability
    monkeypatch.delenv('LANGFUSE_PUBLIC_KEY', raising=False)
    monkeypatch.setattr(observability, '_initialised', False)
    monkeypatch.setattr(observability, '_client', None)
    monkeypatch.setattr(observability, '_observe', None)

    async def tool():
        return 1

    assert observability.observe_tool('tool')(tool) is tool
    assert observability.tracing_enabled() is False
    observability.annotate_current({'a': 1})  # must not raise
