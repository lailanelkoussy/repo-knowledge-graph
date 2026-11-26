"""
Command line interface.

    python -m repo_knowledge_graph build --repo-path ./my-repo --output graph.json
    python -m repo_knowledge_graph build --update graph.json            # incremental update
    python -m repo_knowledge_graph serve --graph-file graph.json        # stdio (IDEs, Claude Desktop...)
    python -m repo_knowledge_graph serve --repo-url https://github.com/org/repo --transport http --port 4000

Every option can also come from the environment (REPO_PATH, REPO_URL, GRAPH_FILE, TRANSPORT, PORT...).
"""
import argparse
import os
import sys
from typing import List, Optional

from dotenv import load_dotenv

from .utils.logger_utils import configure_logging

DEFAULT_EMBED_MODEL = 'Salesforce/SFR-Embedding-Code-400M_R'


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == '':
        return default
    return value.strip().lower() in ('1', 'true', 'yes', 'on')


def _add_source_args(parser: argparse.ArgumentParser, graph_file: bool) -> None:
    source = parser.add_argument_group('source (one of)')
    source.add_argument('--repo-path', default=os.getenv('REPO_PATH'), help='Local repository, archive or file.')
    source.add_argument('--repo-url', default=os.getenv('REPO_URL'), help='Git URL to clone (HTTPS or SSH).')
    if graph_file:
        source.add_argument('--graph-file', default=os.getenv('GRAPH_FILE'),
                            help='Pre-computed graph (JSON) produced by the build command.')
    source.add_argument('--clone-dir', default=os.getenv('CLONE_DIR'),
                        help='Keep the clone of --repo-url here (enables git diff tools).')


def _add_build_args(parser: argparse.ArgumentParser) -> None:
    build = parser.add_argument_group('graph construction')
    build.add_argument('--no-entities', dest='extract_entities', action='store_false',
                       default=_env_bool('EXTRACT_ENTITIES', True), help='Do not extract code entities.')
    build.add_argument('--describe', dest='describe_nodes', action='store_true',
                       default=_env_bool('DESCRIBE_NODES', False), help='Generate LLM descriptions of chunks.')
    build.add_argument('--git-history', action='store_true', default=_env_bool('GIT_HISTORY', False),
                       help='Add commit nodes (repository must be a git checkout).')
    build.add_argument('--max-commits', type=int, default=int(os.getenv('MAX_COMMITS', 200)))
    build.add_argument('--skip-dir', action='append', default=[], help='Directory name to skip (repeatable).')
    build.add_argument('--extension', action='append', default=None,
                       help='Only index files with this extension (repeatable), e.g. --extension py.')
    build.add_argument('--progress', action='store_true', help='Show progress bars (stderr).')
    build.add_argument('--enrichers', default=os.getenv('ENRICHERS', 'auto'),
                       help="Framework enrichers: 'auto' (default), 'none' or a comma-separated list "
                            "(build, supply_chain, spring_config, spring_beans, openapi, spring_web, web_routes, graphql, "
                            "spring_data_jpa, mybatis, spring_messaging, spring_tests, spring_security, security).")
    build.add_argument('--mask-config-values', dest='mask_config_values', action=argparse.BooleanOptionalAction,
                       default=_env_bool('MASK_CONFIG_VALUES', True),
                       help='Mask secret-looking configuration values in the graph, the index and tool outputs.')
    build.add_argument('--max-call-edges', type=int, default=int(os.getenv('MAX_CALL_EDGES') or 0),
                       help="Cap on 'calls' / 'uses_type' edges (0 = unlimited).")

    index = parser.add_argument_group('code index')
    index.add_argument('--no-index', dest='index_nodes', action='store_false',
                       default=_env_bool('INDEX_NODES', True), help='Do not build the search index.')
    index.add_argument('--index-type', choices=['keyword-only', 'embedding-only', 'hybrid'],
                       default=os.getenv('INDEX_TYPE', 'keyword-only'))
    index.add_argument('--backend', choices=['lancedb', 'weaviate'], default=os.getenv('INDEX_BACKEND', 'lancedb'))
    index.add_argument('--db-path', default=os.getenv('LANCEDB_PATH'), help='LanceDB directory.')
    index.add_argument('--embedder', choices=['sentence-transformers', 'openai'],
                       default=os.getenv('EMBEDDER_TYPE', 'sentence-transformers'))
    index.add_argument('--embed-model', default=os.getenv('EMBED_MODEL_NAME', DEFAULT_EMBED_MODEL))


def _model_service_kwargs(args) -> dict:
    return {'embedder_type': args.embedder, 'embed_model_name': args.embed_model}


def _code_index_kwargs(args) -> dict:
    kwargs = {'index_type': args.index_type, 'backend': args.backend, 'show_progress': args.progress}
    if args.db_path:
        kwargs['db_path'] = args.db_path
    return kwargs


def _apply_enrichment_settings(args) -> None:
    """--enrichers / --mask-config-values / --max-call-edges reach the graph builder through the settings."""
    from .settings import get_settings
    os.environ['ENRICHERS'] = args.enrichers
    os.environ['MASK_CONFIG_VALUES'] = 'true' if args.mask_config_values else 'false'
    os.environ['MAX_CALL_EDGES'] = str(args.max_call_edges or 0)
    get_settings.cache_clear()


def _build_kwargs(args) -> dict:
    return dict(
        enrichers=args.enrichers,
        skip_dirs=args.skip_dir,
        extensions=args.extension,
        index_nodes=args.index_nodes,
        describe_nodes=args.describe_nodes,
        extract_entities=args.extract_entities,
        include_git_history=args.git_history,
        max_commits=args.max_commits,
        show_progress=args.progress,
        model_service_kwargs=_model_service_kwargs(args),
        code_index_kwargs=_code_index_kwargs(args),
    )


def load_or_build_graph(args, allow_graph_file: bool = True):
    from .knowledge_graph import RepoKnowledgeGraph
    _apply_enrichment_settings(args)
    graph_file = getattr(args, 'graph_file', None) if allow_graph_file else None
    if graph_file:
        return RepoKnowledgeGraph.load_graph_from_file(
            graph_file, index_nodes=args.index_nodes, model_service_kwargs=_model_service_kwargs(args),
            code_index_kwargs=_code_index_kwargs(args))
    if args.repo_url:
        return RepoKnowledgeGraph.from_repo(args.repo_url, clone_dir=args.clone_dir, **_build_kwargs(args))
    if args.repo_path:
        return RepoKnowledgeGraph.from_path(args.repo_path, **_build_kwargs(args))
    raise SystemExit('error: provide --repo-path, --repo-url' + (' or --graph-file' if allow_graph_file else '')
                     + ' (or the REPO_PATH / REPO_URL / GRAPH_FILE environment variables)')


def cmd_build(args) -> int:
    from .knowledge_graph import RepoKnowledgeGraph
    if args.update:
        _apply_enrichment_settings(args)
        kg = RepoKnowledgeGraph.load_graph_from_file(args.update, index_nodes=False)
        summary = kg.update_from_path(args.repo_path or kg.repo_path, rebuild_index=False)
        print({k: len(v) for k, v in summary.items()}, file=sys.stderr)
        kg.save_graph_to_file(args.output or args.update)
        return 0
    if not args.output:
        raise SystemExit('error: --output is required (or use --update GRAPH)')
    args.index_nodes = args.index_nodes and args.index_type != 'keyword-only'  # embeddings are what is worth saving
    kg = load_or_build_graph(args, allow_graph_file=False)
    kg.save_graph_to_file(args.output)
    print(f'Graph saved to {args.output} ({kg.graph.number_of_nodes()} nodes, '
          f'{kg.graph.number_of_edges()} edges)', file=sys.stderr)
    return 0


def cmd_serve(args) -> int:
    from .mcp_server import KnowledgeGraphMCPServer
    kg = load_or_build_graph(args)
    if args.save_graph:
        kg.save_graph_to_file(args.save_graph)
    server = KnowledgeGraphMCPServer(knowledge_graph=kg, server_name=args.server_name)

    if args.transport == 'stdio':
        server.run(transport='stdio')
        return 0

    from starlette.responses import JSONResponse

    @server.app.custom_route('/health', methods=['GET'])
    async def health_check(request):
        return JSONResponse({'status': 'healthy', 'service': args.server_name})

    server.run(transport=args.transport, host=args.host, port=args.port, path=args.path)
    return 0


def cmd_findings(args) -> int:
    """Candidate security findings as text, JSON or SARIF 2.1.0 (for CI and code scanning)."""
    import json

    from . import findings as fd
    args.index_nodes = False
    kg = load_or_build_graph(args)
    items = fd.filter_findings(fd.collect_findings(kg), args.severity, min_confidence=args.min_confidence)
    if args.format == 'sarif':
        payload = json.dumps(fd.to_sarif(items), indent=2)
    elif args.format == 'json':
        payload = json.dumps([{k: v for k, v in f.items() if k != 'flows'} for f in items], indent=2, default=str)
    else:
        payload = '\n'.join(f"{f['id']} [{f['severity']}/{f['confidence']}] CWE-{f.get('cwe')} {f['title']}"
                            + (f" ({f['file']}:{f['line']})" if f.get('file') and f.get('line') else '') for f in items)
    if args.output:
        with open(args.output, 'w', encoding='utf-8') as handle:
            handle.write(payload + '\n')
        print(f'{len(items)} finding(s) written to {args.output}', file=sys.stderr)
    else:
        print(payload)
    return 1 if args.fail_on and any(f['severity'] == 'high' for f in items) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='python -m repo_knowledge_graph',
                                     description='Code repository knowledge graph and MCP server.')
    parser.add_argument('--log-level', default=os.getenv('LOG_LEVEL', 'INFO'))
    sub = parser.add_subparsers(dest='command', required=True)

    build = sub.add_parser('build', help='Build (or update) a graph and save it to a JSON file.')
    _add_source_args(build, graph_file=False)
    _add_build_args(build)
    build.add_argument('--output', '-o', help='Output JSON file.')
    build.add_argument('--update', metavar='GRAPH', help='Incrementally update an existing graph file.')
    build.set_defaults(func=cmd_build)

    serve = sub.add_parser('serve', help='Run the MCP server.')
    _add_source_args(serve, graph_file=True)
    _add_build_args(serve)
    serve.add_argument('--transport', choices=['stdio', 'http', 'sse'], default=os.getenv('TRANSPORT', 'stdio'))
    serve.add_argument('--host', default=os.getenv('HOST', '0.0.0.0'))
    serve.add_argument('--port', type=int, default=int(os.getenv('PORT', 4000)))
    serve.add_argument('--path', default=os.getenv('MCP_PATH', '/mcp'), help='HTTP path of the MCP endpoint.')
    serve.add_argument('--server-name', default=os.getenv('SERVER_NAME', 'knowledge-graph-mcp-server'))
    serve.add_argument('--save-graph', default=os.getenv('SAVE_GRAPH'), help='Also save the built graph here.')
    serve.set_defaults(func=cmd_serve)

    findings = sub.add_parser('findings', help='List the candidate security findings (text, JSON or SARIF).')
    _add_source_args(findings, graph_file=True)
    _add_build_args(findings)
    findings.add_argument('--format', choices=['text', 'json', 'sarif'], default='text')
    findings.add_argument('--output', '-o', help='Write to this file instead of the standard output.')
    findings.add_argument('--severity', choices=['high', 'medium', 'low'], help='Minimum severity.')
    findings.add_argument('--min-confidence', choices=['high', 'medium', 'low'], help='Minimum confidence.')
    findings.add_argument('--fail-on-high', dest='fail_on', action='store_true',
                          help='Exit with code 1 when a high-severity finding remains (CI gate).')
    findings.set_defaults(func=cmd_findings)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)
    return args.func(args)


if __name__ == '__main__':  # pragma: no cover
    sys.exit(main())
