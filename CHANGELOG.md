# Changelog

All notable changes to this project are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- The distribution is named `repo-kg` (`pip install repo-kg` once released), since `repo-knowledge-graph` is taken on
  PyPI. The import name (`repo_knowledge_graph`) and the `repo-kg` command are unchanged.
- `examples/mcp_client_demo.py` is now a short walk-through of the main tools instead of a scripted test
  of every tool.

### Removed

- The legacy javalang Java parser and the `JAVA_PARSER` setting. Java is always parsed with tree-sitter
  (Java 8 to 21); the `javalang` dependency is gone.

## [0.3.0] - 2026-11-26

First public release.

### Added

- Knowledge graph of a repository: directories, files, language-aware chunks, entities (classes, functions,
  methods, variables...), imports, inheritance, call graph and optional git history.
- Entity extraction for Python, Java 8-21 (tree-sitter), JavaScript, TypeScript/TSX, Rust, C/C++ and HTML.
- Keyword, semantic and hybrid search over LanceDB (default, embedded) or Weaviate.
- MCP server (stdio, HTTP, SSE) exposing navigation, search, history and framework tools.
- `repo-kg` command line: `build`, `serve`, `findings`, incremental `--update`.
- Java / Spring Boot enrichment: Maven / Gradle modules, beans and injection, REST endpoints, contract-first
  OpenAPI, GraphQL (DGS, Spring for GraphQL), JPA and MyBatis persistence, messaging, configuration and
  profiles, tests; generic web routes for Flask, FastAPI, Django REST and Actix.
- Security analysis for agent-assisted audits: catalog of sources, sinks and sanitizers, inter-procedural
  taint flows, security posture and effective authorization of each endpoint, supply chain with opt-in OSV
  lookup, candidate findings and SARIF 2.1.0 export, OWASP Benchmark script.
- Masking of secrets found in configuration files and code; repository content flagged as untrusted in tool
  answers.
- Optional Langfuse tracing of MCP tool calls.
- Docker image and compose file.

[Unreleased]: https://github.com/lailanelkoussy/repo-knowledge-graph/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/lailanelkoussy/repo-knowledge-graph/releases/tag/v0.3.0
