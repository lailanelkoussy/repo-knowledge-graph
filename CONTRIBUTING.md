# Contributing

Thanks for your interest in repo-knowledge-graph! Bug reports, fixes, new language extractors, new framework
enrichers and catalog entries are all welcome.

## Getting started

```bash
git clone https://github.com/lailanelkoussy/repo-knowledge-graph.git && cd repo-knowledge-graph
python -m venv .venv && source .venv/bin/activate
pip install -e ".[cpp,dev]"
```

Before opening a pull request, run the same checks as CI:

```bash
ruff check .
mypy
pytest
```

The test suite is hermetic: embedding models are replaced by a deterministic fake service
(`tests/conftest.py`) and no test calls an LLM. Tests that need a model download, a Weaviate server or the
network are skipped unless enabled (see *Development* in the README).

## Pull requests

- Keep a pull request focused on one change, and add or update tests for it. Fixtures live in
  `tests/fixtures/`; prefer extending an existing fixture over adding a new one.
- Update the README when you add a tool, an option or an environment variable, and add a line to
  `CHANGELOG.md` under *Unreleased*.
- New modules are type-checked: add them to the `files` list of `[tool.mypy]` in `pyproject.toml`.
- Changes to the saved graph format must keep older files readable (see *Design notes* below).

## Reporting bugs

Open an issue with the command or code you ran, what you expected and what happened, and if possible a small
repository (or fixture) that reproduces it. For security problems, follow [SECURITY.md](SECURITY.md)
instead.

## Design notes

A few decisions that are easy to break without noticing:

- **One edge per pair of nodes.** The graph is a `networkx.DiGraph`. When a framework relation (`injects`,
  `queries`, `handles`...) is added between two nodes already linked by a generic one (`calls`, `uses_type`,
  `called_by`), it replaces it and the generic relation is kept in the edge's `also` attribute
  (`enrichers/base.py`). Several call sites between the same two methods are stored as a `sites` list on the
  `calls` edge rather than as parallel edges.
- **Stable chunk ids.** Chunk ids are `<file path>_<index>` (`builder.py`). Saved graphs and MCP clients rely on
  this format; read `ChunkNode.path` rather than parsing the id.
- **Backward-compatible graph files.** `serialization.SCHEMA_VERSION` is 3. Adding fields is fine; when the
  layout changes, bump the version and keep reading older files.
- **Bounded analysis.** Taint propagation follows at most `taint.MAX_DEPTH` (8) calls, with memoised
  per-method summaries and cycles cut, so large repositories stay tractable.
- **Candidates, not verdicts.** Security results carry a confidence and `file:line` evidence. Ambiguous names
  are left unresolved rather than guessed.
- **Untrusted content.** Text read from an analysed repository may contain instructions aimed at the agent;
  tool answers mark it as untrusted (`content_safety.py`) and secrets are masked (`secrets_masking.py`).

### Adding a framework enricher

An enricher is a module of `repo_knowledge_graph/enrichers/` with two functions, `applies(context)` (does the
repository use this framework?) and `enrich(context)` (add nodes, edges and attributes through the
`EnrichmentContext` helpers of `base.py`). Register it in `enrichers/__init__.py`: the order of the registry
matters, since later enrichers read what earlier ones produced. `web_routes.py` is a short example.

### Adding a language

Declare the language in `languages.py` (extensions, splitter), then add an entity extractor in
`entity_extractor.py` (tree-sitter is preferred for new languages) and tests next to the existing
`tests/test_<language>.py` files.

### Extending the security catalog

Sources, sinks and sanitizers live in `repo_knowledge_graph/security_catalog.yaml`; the README section
*Extending the catalog* describes the fields. Add a vulnerable case and its fixed variant to
`tests/fixtures/vuln_shop` with each new rule, so that false positives are measured too.
