# Repo Knowledge Graph — MCP server

[![CI](https://github.com/lailanelkoussy/repo-knowledge-graph/actions/workflows/ci.yml/badge.svg)](https://github.com/lailanelkoussy/repo-knowledge-graph/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)

Turn a code repository into a **knowledge graph** (files, code chunks, classes, functions, imports,
inheritance, call graph, git history) and serve it to LLM agents through the
**[Model Context Protocol](https://modelcontextprotocol.io) (MCP)**.

An agent connected to the server can browse the tree of the repository, jump to a definition, find the
usages of a function, follow imports, calls and class hierarchies, search the code and read its history,
without having to load the whole repository into its context.

- **Zero-config by default**: keyword search runs on an embedded index (LanceDB), so no model,
  GPU or external service is needed.
- **Multi-language entity extraction**: Python, Java (8 to 21), JavaScript, TypeScript/TSX, Rust, C/C++ and HTML.
- **Framework-aware enrichment**: for Java / Spring Boot projects the graph also holds REST endpoints,
  beans and their injections, JPA entities and tables, configuration properties (secrets masked),
  messaging channels, Maven / Gradle modules and the tests of each class, so an agent can trace a request
  from the endpoint down to the database.
- **Build once, serve many times**: graphs are saved to JSON and can be updated incrementally.
- **Works with any MCP client** over `stdio` (Claude Desktop, Claude Code, VS Code, Cursor...) or HTTP.

---

## Contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Connecting an MCP client](#connecting-an-mcp-client)
- [MCP tools](#mcp-tools)
- [Command line reference](#command-line-reference)
- [Python API](#python-api)
- [Search index](#search-index)
- [Supported languages](#supported-languages)
- [Java / Spring Boot](#java--spring-boot)
- [Configuration](#configuration)
- [Docker](#docker)
- [Examples](#examples)
- [Development](#development)
- [Project layout](#project-layout)
- [Troubleshooting](#troubleshooting)
- [Limitations](#limitations)
- [Scope and responsible use](#scope-and-responsible-use)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [License](#license)

---

## How it works

```
               ┌───────────────┐  build / load   ┌──────────────────┐  MCP tools    ┌───────────┐
 repository ──▶│ graph builder │────────────────▶│ knowledge graph  │──────────────▶│ LLM agent │
 (path, URL,   └───────────────┘  (graph.json)   │ + search index   │ stdio / HTTP  └───────────┘
  archive)                                       └──────────────────┘
```

1. **Read** the repository: text files only, honouring `.gitignore`, skipping binaries, very large files and
   tool directories (`.git`, `node_modules`, `__pycache__`...).
2. **Chunk** every file with a language-aware splitter (classes and functions are kept together where possible).
3. **Extract entities** with language-specific AST parsers: declared classes, functions, methods, variables,
   interfaces..., the names each file calls, its imports and its base classes. For Java, every declaration
   also carries its annotations (with their arguments), modifiers, signature, Javadoc summary and position.
4. **Resolve** calls, imports and base classes across the repository, using aliases
   (`Cart`, `shop.cart.Cart`, `math::Calculator`) and the file's imports. Java calls are resolved with the
   type of the receiver (field, parameter, local variable). Ambiguous names are left unresolved rather
   than guessed.
5. **Enrich** with framework knowledge (`--enrichers`, automatic by default): build files, Spring beans and
   injection, web endpoints, JPA persistence, messaging, configuration and tests become nodes, edges and
   attributes of the graph (see [Java / Spring Boot](#java--spring-boot)).
6. **Index** the chunks for search (keyword, semantic or hybrid).
7. **Serve** the graph through MCP tools.

### Graph model

```
root ─contains─▶ directory ─contains─▶ file ─contains─▶ chunk ─declares─▶ entity ─called_by─▶ chunk
                                        │                                   │
                                        └──imports──▶ file                  ├──inherits──▶ entity
                                                                            ├──calls──▶ entity (method)
                                                                            └──uses_type──▶ entity (type)
commit ─modifies─▶ file                                  (commit nodes only with --git-history)
```

Framework enrichment (Java / Spring Boot) adds resource nodes and relations on top of it:

```
module ─depends_on─▶ dependency | module      endpoint ─handled_by─▶ entity (method) ─calls─▶ entity (method)
entity ─injects─▶ entity (bean)               entity (interface) ─implemented_by─▶ entity (class)
entity (repository) ─persists─▶ entity (JPA) ─maps_to─▶ table      entity ─reads_config─▶ config_property
entity (method) ─publishes / listens_to─▶ channel                  entity (test) ─tests─▶ entity
```

| Node type         | Id example                                   | Content |
|-------------------|----------------------------------------------|---------|
| `root`            | `root`                                       | Repository root, aggregated entities |
| `directory`       | `shop`                                       | Directory, aggregated entities |
| `file`            | `shop/cart.py`                               | Full content, language, imports, content hash |
| `chunk`           | `shop/cart.py_0`                             | Part of a file (`<path>_<index>`), lines, declared and called entities, optional LLM description and embedding |
| `entity`          | `Cart`, `com.acme.Inventory`, `com.acme.Inventory::count` | Class, function, method, variable, interface... with its type, aliases, declaring and calling chunks, position (`file_path`, `start_line`, `end_line`) and `attributes` |
| `commit`          | `commit:<sha>`                               | Hash, author, date, message, files changed |
| `endpoint`        | `endpoint:GET /api/orders/{id}`, `endpoint:GRAPHQL Query.article` | HTTP method (or `GRAPHQL`), path (or `Type.field`), parameters / arguments, request / response types, security; `external` for Feign / HTTP clients, `implemented: false` for contract operations without code |
| `config_property` | `config:spring.datasource.url`               | Values per profile (secrets masked), defining files, `defined` flag |
| `table`           | `table:orders`                               | Database table from JPA mappings and SQL / Liquibase migrations, columns, drift between both |
| `module`          | `module:shop-api`                            | Maven / Gradle module, Java and Spring Boot versions |
| `dependency`      | `dep:org.springframework.boot:spring-boot-starter-web` | Library, versions per module |
| `channel`         | `channel:kafka:order-created`                | Kafka topic, RabbitMQ / JMS / SQS queue or application event type |

| Relation         | From → to                              | Meaning |
|------------------|----------------------------------------|---------|
| `contains`       | root/directory → directory/file, file → chunk | Tree structure |
| `declares`       | chunk → entity                         | The chunk defines the entity |
| `called_by`      | entity → chunk                         | The chunk uses / calls the entity |
| `imports`        | file → file                            | Import resolved to a file of the repository |
| `inherits`       | entity → entity                        | A class extends / implements another class or interface |
| `implemented_by` | entity (interface) → entity (class)    | Concrete class implementing the interface, directly or not |
| `calls`          | entity (method) → entity               | Type-resolved call (or instantiation); `sites` lists each call site with its argument → parameter bindings |
| `uses_type`      | entity → entity (type)                 | Parameter, return, field or generic type reference |
| `modifies`       | commit → file                          | The commit changed the file |
| `injects`        | entity (bean) → entity (bean)          | Dependency injection (`via`: constructor / field / setter / bean_method, `confidence`) |
| `provides`       | entity (@Bean method) → entity (type)  | Type of the bean created by a `@Bean` method |
| `handled_by`     | endpoint → entity (method)             | Controller method serving the endpoint |
| `calls_endpoint` | entity (method) → endpoint             | Outgoing HTTP call (Feign, RestTemplate, WebClient, RestClient, MockMvc) |
| `handles`        | entity (method) → entity (exception)   | `@ExceptionHandler` |
| `persists`       | entity (repository) → entity (JPA)     | Spring Data repository of an entity |
| `maps_to`        | entity (JPA) → table                   | Table of a JPA entity |
| `relates_to`     | entity (JPA) → entity (JPA)            | JPA association (`cardinality`, `mapped_by`, `fetch`, `cascade`...) |
| `queries`        | entity (repository / mapper method) → table | Tables read or written by a `@Query` or a MyBatis statement (`operation`: select, insert, update, delete) |
| `migrates`       | file → table                           | SQL migration creating / altering the table |
| `reads_config`   | entity → config_property               | `@Value`, `@ConfigurationProperties`, `@ConditionalOnProperty`, `Environment`, `${...}` in annotations |
| `defined_in`     | config_property / module → file        | File defining the property / module |
| `publishes` / `listens_to` | entity (method) → channel    | Message or event producer / consumer |
| `tests`          | entity (test class) → entity           | Class under test (`via`: annotation, subject, naming) |
| `depends_on`     | module → dependency / module           | Build dependency (`scope`, `version`) |
| `builds`         | module → directory                     | Root directory of the module |

Edges carry extra attributes when useful (`confidence`, `via`, `scope`...). The graph keeps one edge per
pair of nodes: a framework relation replaces a generic one (`calls`, `uses_type`), which is kept in the
edge's `also` attribute. All paths and ids use `/` separators, whatever the operating system.

---

## Requirements

- Python **3.10+**
- `git` on the `PATH` (cloning with `--repo-url`, git history)
- Optional: [libclang](https://pypi.org/project/libclang/) for C/C++ (installed by the `cpp` extra),
  a GPU for faster local embeddings, a Weaviate server for the Weaviate backend.

---

## Installation

```bash
git clone https://github.com/lailanelkoussy/repo-knowledge-graph.git && cd repo-knowledge-graph
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .
```

Optional features are installed with extras:

| Extra           | Adds                                               | Needed for |
|-----------------|----------------------------------------------------|------------|
| *(none)*        | graph, LanceDB keyword search, MCP server, CLI     | the default setup |
| `st`            | sentence-transformers, torch                       | local embeddings (`--index-type hybrid` / `embedding-only`) |
| `cpp`           | libclang                                           | C/C++ entity extraction |
| `weaviate`      | weaviate-client                                    | `--backend weaviate` |
| `observability` | langfuse                                           | tracing of MCP tool calls |
| `dev`           | pytest, ruff, mypy                                 | development |
| `all`           | `st`, `cpp`, `weaviate`, `observability`           | everything |

```bash
pip install -e ".[st,cpp]"
```

`requirements.lock` pins the exact versions the test-suite was run with:
`pip install -r requirements.lock && pip install --no-deps -e .`

---

## Quick start

```bash
# 1. Build the graph of a repository and save it
python -m repo_knowledge_graph build --repo-path ../my-project --output my-project.graph.json

# 2. Serve it over stdio (for a local MCP client) ...
python -m repo_knowledge_graph serve --graph-file my-project.graph.json

# ... or over HTTP: endpoint http://localhost:4000/mcp, health check http://localhost:4000/health
python -m repo_knowledge_graph serve --graph-file my-project.graph.json --transport http --port 4000
```

The server can also build the graph at start-up (`--repo-path` or `--repo-url` instead of `--graph-file`).
Framework enrichers are detected automatically: on a Spring Boot project the same `build` command also
produces endpoints, beans, JPA entities, tables and configuration properties.
Once the package is installed, the `repo-kg` command is equivalent to `python -m repo_knowledge_graph`.

---

## Connecting an MCP client

Clients that launch the server themselves (stdio) need the **absolute** path of the Python interpreter
of your environment and of the graph file.

**Claude Desktop** (`claude_desktop_config.json`), **Cursor** (`.cursor/mcp.json`) and most clients:

```json
{
  "mcpServers": {
    "repo-graph": {
      "command": "/abs/path/to/.venv/bin/python",
      "args": ["-m", "repo_knowledge_graph", "serve", "--graph-file", "/abs/path/to/my-project.graph.json"]
    }
  }
}
```

**Claude Code**:

```bash
claude mcp add repo-graph -- /abs/path/to/.venv/bin/python -m repo_knowledge_graph serve --graph-file /abs/path/to/my-project.graph.json
```

**VS Code** (`.vscode/mcp.json`), building the graph of the open workspace at start-up:

```json
{
  "servers": {
    "repo-graph": {
      "type": "stdio",
      "command": "/abs/path/to/.venv/bin/python",
      "args": ["-m", "repo_knowledge_graph", "serve", "--repo-path", "${workspaceFolder}"]
    }
  }
}
```

**HTTP clients**: start the server with `--transport http` and connect to `http://<host>:4000/mcp`
(streamable HTTP). `--transport sse` is also available for older clients.

> In stdio mode, logs are written to **stderr** only, so they never corrupt the protocol stream.

---

## MCP tools

Node ids are the ids of the [graph model](#graph-model). Wherever a node id is expected, an entity name
or alias is accepted too (`shop.cart.Cart` resolves to `Cart`). List tools are paginated with
`limit` (1-200) and `offset`.

### Overview and navigation

| Tool | Parameters | Returns |
|------|------------|---------|
| `get_graph_stats` | — | Node counts by type, edge counts by relation, number of entities, whether search is available |
| `print_tree` | `root_id='root'`, `max_depth=3`, `include_chunks=false` | Directory tree (follows `contains` edges, capped at 500 nodes) |
| `get_node_info` | `node_id`, `include_content=false` | Type, name, description, declared / called entities, content preview or full content (20 000 characters max); for entities their `location` (`file:line`) and `attributes` (annotations, signature, Spring / JPA data), for resource nodes their attributes |
| `get_node_edges` | `node_id`, `relation=None`, `limit=50`, `offset=0` | Incoming and outgoing edges with their attributes, optionally filtered by relation |
| `get_neighbors` | `node_id`, `limit=20`, `offset=0` | Directly connected nodes with relation and direction |
| `find_path` | `source_id`, `target_id`, `max_depth=5` | Shortest directed path, with the relation of each step |
| `get_subgraph` | `node_id`, `depth=2` (max 4), `edge_types=None` | Nodes and edges around a node |

### Finding things

| Tool | Parameters | Returns |
|------|------------|---------|
| `search_nodes` | `query`, `limit=10` | Most relevant chunks (path, content, entities, score) from the search index |
| `search_file_names_by_regex` | `pattern`, `limit=20` | Files whose path matches a Python regular expression |
| `search_by_type_and_name` | `node_type`, `name_query`, `entity_type=None`, `limit=10` | Nodes whose name contains a substring (case-insensitive) |
| `list_nodes_by_type` | `node_type`, `entity_type=None`, `limit=20`, `offset=0` | Nodes of a type: `root`, `directory`, `file`, `chunk`, `entity`, `commit`, `endpoint`, `config_property`, `table`, `module`, `dependency`, `channel` |
| `list_all_entities` | `limit=50`, `offset=0`, `entity_type=None`, `declared_only=false` | Entities with their types, declaration and usage counts |

### Code understanding

| Tool | Parameters | Returns |
|------|------------|---------|
| `go_to_definition` | `entity_name`, `limit=5` | `location` (`file:line`), signature and chunks declaring the entity (or a note when it is external to the repository) |
| `find_usages` | `entity_name`, `limit=20`, `offset=0` | Chunks using / calling the entity, plus `typed_usages`: callers (`calls`), injecting beans (`injects`), declarations using the type (`uses_type`), implementations, tests... |
| `get_file_structure` | `file_path`, `max_entities=30`, `max_chunks=30` | Language, chunks with descriptions, declared entities, imported files |
| `get_file_stats` | `path` | Lines, chunks, declared and called entities of a file or directory |
| `get_chunk_context` | `node_id` | A chunk with the previous and next chunks of its file |
| `get_related_chunks` | `chunk_id`, `relation_type='calls'` or `'called_by'`, `limit=15` | `calls`: chunks declaring what this chunk uses; `called_by`: chunks using what it declares |
| `diff_chunks` | `node_id_1`, `node_id_2` | Unified diff between the contents of two nodes |

### Java / Spring Boot

Available when the framework enrichers found something (see [Java / Spring Boot](#java--spring-boot));
`list_endpoints` / `get_endpoint` / `trace_flow` also cover GraphQL operations (`list_endpoints(http_method="GRAPHQL")`,
`trace_flow("GRAPHQL Mutation.createArticle")`) and the Flask, FastAPI, Django REST, Actix, Rocket and Axum routes. On a Spring Boot project, start with `get_project_overview`, then `list_endpoints`, and use
`trace_flow` on an endpoint to see the controller → service → repository → table chain.

| Tool | Parameters | Returns |
|------|------------|---------|
| `get_project_overview` | — | Languages, modules, Java / Spring Boot versions, beans by stereotype, endpoints (REST and GraphQL), JPA entities, repositories, MyBatis statements, tables, channels, configuration properties, profiles, tests |
| `list_endpoints` | `http_method=None`, `path_prefix=None`, `include_external=false`, `limit=50`, `offset=0` | Endpoints with HTTP method, path, handler (`file:line`) and security |
| `get_endpoint` | `endpoint` (`'GET /api/orders/{id}'`, a concrete path `'GET /api/orders/42'` or an id) | Handler, path / query parameters, body, response, status, consumes / produces, validation, security (annotations and `SecurityFilterChain` rules), exception handlers, callers; for contract-first APIs the OpenAPI operation (schemas, responses) and whether it is implemented |
| `trace_flow` | `start` (endpoint, method or channel), `max_depth=6`, `limit=60`, `include_trivial=false` | Execution tree: `handled_by` → `calls` → `implemented_by` → repository (`persists`) → entity (`maps_to`) → table, `queries`, `publishes` → consumers, `calls_endpoint` |
| `list_beans` | `stereotype=None`, `profile=None`, `limit=50`, `offset=0` | Beans with stereotype, name, type, scope, profiles, conditions, `@Primary` / `@Qualifier` |
| `get_bean_dependencies` | `bean`, `direction='both'` (`injects` / `injected_into`), `depth=1` | Injected beans and consumers with `via` and `confidence` (exact, inferred, primary, qualifier, by_name, collection, ambiguous), unresolved injection points |
| `find_implementations` | `type_name` | Implementations and subtypes (transitive) |
| `find_by_annotation` | `annotation`, `entity_type=None`, `limit=50`, `offset=0` | Entities carrying an annotation (simple or qualified name), with its arguments |
| `get_data_model` | `entity=None` | JPA entities (table, columns, id, associations), repositories, tables (sources, migrations, drift) |
| `get_config` | `key=None`, `prefix=None`, `profile=None`, `undefined_only=false`, `limit=50`, `offset=0` | Properties with values per profile (secrets masked), files and readers; properties read but never defined |
| `list_messaging` | `channel=None` | Channels with producers and consumers, `@Scheduled` jobs |
| `list_dependencies` | `module=None`, `name_query=None`, `limit=100`, `offset=0` | Modules (Java / Spring Boot versions) and their dependencies with versions and scopes |
| `find_sinks` | `category=None`, `status=None`, `cwe=None`, `include_safe=false`, `path_prefix=None`, `limit=30`, `offset=0` | Calls to dangerous APIs (SQL, command, path, SSRF, expression, LDAP, XPath, redirect, XXE, deserialization, weak crypto, insecure randomness, trust-all TLS...) with CWE, severity and status, most worrying first; safe ones hidden unless asked |
| `find_sources` | `kind=None`, `limit=50`, `offset=0` | Untrusted inputs: handler parameters (`@RequestParam`, `@PathVariable`, `@RequestBody`, GraphQL arguments, message payloads) and calls returning request data (`getParameter`, `getHeader`...) |
| `trace_taint` | `target=None` (endpoint, method, class or channel), `category=None`, `cwe=None`, `min_confidence='low'`, `limit=20`, `offset=0` | Source → sink paths: entry point, each call site crossed (`file:line`, parameter bound, interface dispatch), sink, confidence |
| `find_callers` | `entity`, `max_depth=4`, `limit=100` | Transitive callers of a method (interface methods included) with call sites, and the endpoints / listeners reaching it |
| `get_security_posture` | `severity=None`, `limit=40`, `offset=0` | Filter chains (conditions, rules in order, CSRF, CORS, headers, session, authentication), method security, ignored paths, CORS, risky properties, candidate findings (CWE, severity, confidence, hint) |
| `list_attack_surface` | `access=None`, `http_method=None`, `path_prefix=None`, `with_findings=false`, `limit=50`, `offset=0` | Entry points (HTTP, GraphQL, listeners) with effective authorization and what decides it, inputs, taint flows, findings; least protected first |
| `list_findings` | `severity=None`, `cwe=None`, `kind=None`, `min_confidence=None`, `path_prefix=None`, `include_unconfirmed=false`, `limit=30`, `offset=0` | Every candidate finding (taint, sink, secret, posture, dependency) with stable id, CWE, severity, confidence, `file:line`, evidence and a hint to verify it |
| `export_sarif` | `severity=None`, `min_confidence=None` | The findings as a SARIF 2.1.0 log (CWE tags, taint flows as code flows) |
| `get_code` | `file_path`, `start_line=1`, `end_line=None`, `context=0` | Numbered lines of a file (secrets masked, at most 200 lines) |
| `grep_code` | `pattern`, `path_prefix=None`, `case_insensitive=false`, `max_results=50` | Regular-expression search in file contents (secrets masked) |
| `get_dependency_usage` | `dependency`, `include_tests=false` | Files importing a library and the entry points reaching them (offline) |
| `check_dependencies` | `include_tests=false`, `refresh=false` | Known vulnerabilities (OSV) of the dependencies with a resolved version, with reachability; network only with `OSV_LOOKUP=1` |
| `find_tests_for` | `entity` | Test classes covering the class (kind: unit, web_slice, data_slice, integration; how the link was found) |

### Git history

Available when the graph was built with `--git-history`.

| Tool | Parameters | Returns |
|------|------------|---------|
| `get_file_history` | `file_path`, `limit=20` | Commits that modified the file, most recent first |
| `get_commit_info` | `commit` (full or short hash) | Author, date, message, files changed |
| `diff_file_versions` | `file_path`, `rev_a`, `rev_b='HEAD'` | `git diff` of the file between two revisions; needs the repository on disk (`--repo-path`, or `--repo-url` with `--clone-dir`) |

### Errors

Tools never raise: they return a structured error that agents can act on, with suggestions for
misspelled ids.

```json
{"error": "Node 'shop/crat.py' not found in knowledge graph. Did you mean: shop/cart.py?",
 "error_type": "node_not_found", "context": "get_node_info"}
```

`error_type` is one of `node_not_found`, `entity_not_found`, `invalid_input`, `git_error`, `internal_error`.

---

## Command line reference

```
python -m repo_knowledge_graph [--log-level LEVEL] {build,serve} [options]
```

### `build`: create or update a graph file

```bash
python -m repo_knowledge_graph build --repo-path ./my-project -o graph.json
python -m repo_knowledge_graph build --repo-url https://github.com/owner/repo -o graph.json --git-history --clone-dir ./clone
python -m repo_knowledge_graph build --update graph.json               # re-analyse changed files only
python -m repo_knowledge_graph build --update graph.json --repo-path ./my-project -o graph-v2.json
```

| Option | Description |
|--------|-------------|
| `-o, --output FILE` | Output JSON file |
| `--update GRAPH` | Update an existing graph: only added / modified files are re-analysed, deleted files are removed |

With `build`, embeddings are computed and saved only for `--index-type hybrid` or `embedding-only`
(keyword indexes are cheap to rebuild at start-up).

### `serve`: run the MCP server

| Option | Default | Description |
|--------|---------|-------------|
| `--transport` | `stdio` | `stdio`, `http` or `sse` |
| `--host` / `--port` | `0.0.0.0` / `4000` | HTTP address |
| `--path` | `/mcp` | HTTP path of the MCP endpoint |
| `--server-name` | `knowledge-graph-mcp-server` | Name announced to clients |
| `--save-graph FILE` | — | Also save the graph built at start-up |

### `findings`: candidate security findings (CI)

```bash
python -m repo_knowledge_graph findings --graph-file graph.json --format sarif -o findings.sarif --severity medium --fail-on-high
```

`--format text|json|sarif`, `--severity`, `--min-confidence`; `--fail-on-high` exits with code 1 when a
high-severity finding remains. The SARIF file can be uploaded to GitHub code scanning.

### Options shared by `build` and `serve`

| Option | Default | Description |
|--------|---------|-------------|
| `--repo-path PATH` | — | Local directory, `.zip` / `.tar.gz` archive or single file |
| `--repo-url URL` | — | Git URL to clone (HTTPS or `git@` SSH form; private repositories with `GITHUB_TOKEN`) |
| `--graph-file FILE` | — | *(serve only)* Load a graph built beforehand; takes precedence over the other sources |
| `--clone-dir DIR` | temporary | Keep the clone of `--repo-url` (required for `diff_file_versions`) |
| `--no-entities` | — | Skip entity extraction (tree and search only) |
| `--describe` | off | Generate an LLM summary of every chunk (needs an OpenAI-compatible endpoint) |
| `--git-history` | off | Add commit nodes |
| `--max-commits N` | `200` | Commits read with `--git-history` |
| `--skip-dir NAME` | — | Directory name to skip, repeatable |
| `--extension EXT` | all | Only these file extensions, repeatable (`--extension py --extension ts`) |
| `--enrichers` | `auto` | Framework enrichers: `auto` (those detecting their framework), `none`, or a comma-separated list of `build`, `supply_chain`, `spring_config`, `spring_beans`, `openapi`, `spring_web`, `web_routes`, `graphql`, `spring_data_jpa`, `mybatis`, `spring_messaging`, `spring_tests`, `spring_security`, `security` |
| `--mask-config-values` / `--no-mask-config-values` | on | Mask secret-looking configuration values in the graph, the search index and tool outputs |
| `--max-call-edges N` | `0` (unlimited) | Cap on `calls` / `uses_type` edges for very large repositories |
| `--no-index` | — | No search index (`search_nodes` disabled) |
| `--index-type` | `keyword-only` | `keyword-only`, `embedding-only` or `hybrid` |
| `--backend` | `lancedb` | `lancedb` or `weaviate` |
| `--db-path DIR` | `./local_code_index_db` | LanceDB directory |
| `--embedder` | `sentence-transformers` | `sentence-transformers` (local) or `openai` (any OpenAI-compatible API) |
| `--embed-model NAME` | `Salesforce/SFR-Embedding-Code-400M_R` | Embedding model |
| `--progress` | off | Progress bars on stderr |

Every option can also be set through an environment variable (see [Configuration](#configuration)).

---

## Python API

```python
from repo_knowledge_graph import RepoKnowledgeGraph, KnowledgeGraphMCPServer

# Build
kg = RepoKnowledgeGraph.from_path(
    "./my-project",
    extract_entities=True,
    index_nodes=True,
    code_index_kwargs={"index_type": "hybrid", "backend": "lancedb"},
    model_service_kwargs={"embedder_type": "sentence-transformers",
                          "embed_model_name": "Salesforce/SFR-Embedding-Code-400M_R"},
    include_git_history=True,
)
# Or: RepoKnowledgeGraph.from_repo("https://github.com/owner/repo", clone_dir="./clone", ...)

# Explore
kg.entities["Cart"]                      # {'declaring_chunk_ids', 'calling_chunk_ids', 'type', 'dtype', 'aliases'}
kg.resolve_entity("shop.cart.Cart")      # 'Cart'
kg.get_chunks_of_file("shop/cart.py")
kg.find_path("shop/checkout.py", "Item")
kg.get_file_history("shop/cart.py")
kg.code_index.search("where is the total computed?", 5)   # list of SearchResult
kg.graph                                 # the underlying networkx.DiGraph

# Framework knowledge (enrichers=... : 'auto' by default, 'none', or a list such as 'spring_web,build')
kg.get_project_overview()                # versions, modules, beans, endpoints, tables...
kg.get_endpoints(http_method="POST")     # endpoint ResourceNodes
kg.trace_flow("POST /api/orders")["text"]
kg["com.acme.OrderService"].attributes["spring"]   # stereotype, bean name, injections...
kg.get_resources("table")                # also: endpoint, config_property, module, dependency, channel

# Persist and update
kg.save_graph_to_file("graph.json")
kg = RepoKnowledgeGraph.load_graph_from_file("graph.json", index_nodes=False)
summary = kg.update_from_path("./my-project")   # {'added': [...], 'modified': [...], 'deleted': [...], 'unchanged': [...]}

# Serve
KnowledgeGraphMCPServer(knowledge_graph=kg).run(transport="stdio")
```

`from_path` can be called from a running event loop (Jupyter, async frameworks); in async code,
`await RepoKnowledgeGraph.from_path_async(...)` avoids a helper thread. No model is loaded unless
embeddings or descriptions are requested.

---

## Search index

| `--index-type` | How | Needs |
|----------------|-----|-------|
| `keyword-only` *(default)* | BM25 full-text search on path, name, description and code | nothing |
| `embedding-only` | Vector similarity | an embedding model |
| `hybrid` | BM25 and vector results fused by reciprocal rank fusion | an embedding model |

| `--backend` | Notes |
|-------------|-------|
| `lancedb` *(default)* | Embedded, stored in `--db-path` / `LANCEDB_PATH` |
| `weaviate` | External server (`WEAVIATE_HOST`, `WEAVIATE_PORT`, `WEAVIATE_GRPC_PORT`), `weaviate` extra |

Embeddings come either from a local sentence-transformers model (`--embedder sentence-transformers`, `st`
extra, GPU used when available) or from an OpenAI-compatible API (`--embedder openai`: OpenAI, vLLM,
Ollama...; configured with `OPENAI_EMBED_BASE_URL` and `OPENAI_EMBED_TOKEN`).

Indexes are named after a fingerprint of their content: restarting the server on an unchanged repository
reuses the existing index instead of re-embedding everything. Embeddings stored in a graph file are reused too.

---

## Supported languages

| Language | Extensions | Chunking | Entities | Imports → files | Inheritance |
|----------|------------|:--------:|:--------:|:---------------:|:-----------:|
| Python | `.py` `.pyw` | ✓ | ✓ (`ast`) | ✓ (absolute and relative) | ✓ |
| Java | `.java` | ✓ | ✓ (tree-sitter, Java 8-21: records, sealed types, text blocks, switch expressions, pattern matching) | ✓ | ✓ (classes and interfaces) |
| JavaScript | `.js` `.mjs` `.cjs` `.jsx` | ✓ | ✓ (esprima) | ✓ (relative) | ✓ |
| TypeScript | `.ts` `.tsx` `.mts` `.cts` | ✓ | ✓ (tree-sitter) | ✓ (relative) | ✓ |
| Rust | `.rs` | ✓ | ✓ (tree-sitter) | — | — |
| C / C++ | `.c` `.h` `.cpp` `.cc` `.cxx` `.hpp` `.hh` `.hxx` | ✓ | ✓ (libclang, `cpp` extra) | — | — |
| HTML | `.html` `.htm` | ✓ | ✓ (ids, classes, inline scripts) | — | — |
| Go, Markdown | `.go` `.md` | ✓ | — | — | — |
| Build, configuration and API files | `pom.xml`, `build.gradle(.kts)`, `application*.yml` / `.properties`, `*.sql`, Liquibase changelogs, OpenAPI / Swagger specifications | generic | through the [enrichers](#java--spring-boot) (modules, dependencies, properties, tables, endpoints) | — | — |
| Other text files | any | generic | — | — | — |

Web API definitions (Flask, FastAPI, Django REST, Spring, Actix / Rocket / Axum) and HTTP calls
(`fetch`, `axios`...) are recorded as entities as well, and become `endpoint` nodes through the enrichers.

---

## Java / Spring Boot

Java declarations carry `attributes`: annotations with their arguments (`{"name": "GetMapping",
"qualified": "org.springframework...GetMapping", "args": {"value": "/{id}"}}`), modifiers, signature,
parameters with their annotations, return type, `throws`, Javadoc summary, the calls each method makes
and the types it references. Lombok `@Data` / `@Getter` / `@Setter` / `@Value` / `@Builder` accessors are
added as synthetic methods so that calls to them resolve.

On top of the code graph, **enrichers** (`repo_knowledge_graph/enrichers/`) add framework knowledge. With
`--enrichers auto` (default) each one runs only when it detects its framework:

| Enricher | Reads | Adds |
|----------|-------|------|
| `build` | `pom.xml`, `build.gradle(.kts)`, `settings.gradle(.kts)` | `module` and `dependency` nodes, Java and Spring Boot versions (inherited from parent modules) |
| `supply_chain` | dependency nodes, Maven `<dependencyManagement>`, Gradle `libs.versions.toml`, imports | `resolved_version` / `version_source` (declared, dependency_management, version_catalog, spring_boot, managed), `packages`, `uses_dependency` edges (file -> library) |
| `spring_config` | `application*.yml` / `.properties` (per profile, YAML multi-documents) | `config_property` nodes, `reads_config` edges from `${...}` in annotations, `@ConfigurationProperties` (relaxed binding), `@ConditionalOnProperty`, `Environment.getProperty`; secrets masked |
| `spring_beans` | stereotypes (also meta-annotations of the repository), Spring Data interfaces, `@Bean` methods | `attributes.spring` (stereotype, bean name, scope, profiles, conditions...), `injects` edges (constructor, Lombok constructors, fields, setters, `@Bean` parameters) |
| `openapi` | OpenAPI 3 / Swagger 2 specifications (`openapi.yml`, `swagger.json`...) | Contract-first APIs: each operation is matched to the controller method implementing the generated interface (`<Tag>Api`, method named after the `operationId`), then turned into an endpoint by `spring_web` |
| `spring_web` | `@*Mapping`, security annotations, `SecurityFilterChain`, `@ExceptionHandler`, Feign / `@HttpExchange`, RestTemplate / WebClient / RestClient / MockMvc calls, matched OpenAPI operations | `endpoint` nodes, `handled_by`, `handles`, `calls_endpoint` (matched to the repository's own endpoints when possible) |
| `web_routes` | Flask, FastAPI, Django REST, Actix, Rocket, Axum routes | `endpoint` nodes and `handled_by` edges |
| `graphql` | GraphQL schemas (`*.graphqls`, `*.graphql`), Netflix DGS (`@DgsQuery`, `@DgsMutation`, `@DgsData`...) and Spring for GraphQL (`@QueryMapping`, `@MutationMapping`, `@SchemaMapping`...) resolvers | `endpoint:GRAPHQL Query.article` nodes for root operations (arguments, return type, security, filter-chain rule of the GraphQL path), `attributes.graphql` on every resolver; DGS codegen constants (`MUTATION.CreateArticle`) resolved against the schema |
| `spring_data_jpa` | `@Entity`, `@MappedSuperclass`, associations, Spring Data repositories, `@Query`, Flyway / Liquibase migrations | `attributes.jpa`, `table` nodes, `maps_to`, `relates_to`, `persists`, `queries`, `migrates`, column drift between entities and migrations |
| `mybatis` | Mapper XML files (`<select>` / `<insert>` / `<update>` / `<delete>`, `<include>` fragments) and `@Select` / `@Insert`... annotations | `attributes.mybatis` (statement, SQL, tables) on mapper methods, `queries` edges (with `operation`) to `table` nodes; `@Mapper` interfaces become beans |
| `spring_messaging` | `@KafkaListener`, `@RabbitListener`, `@JmsListener`, `@SqsListener`, `KafkaTemplate`, `RabbitTemplate`, `JmsTemplate`, `StreamBridge`, `publishEvent`, `@EventListener`, `@Scheduled`, `@Async`, `@Transactional` | `channel` nodes, `publishes` / `listens_to`, schedule / async / transaction attributes |
| `spring_tests` | test classes, `@WebMvcTest(X.class)`, `@DataJpaTest`, `@InjectMocks`, naming (`FooTest` → `Foo`) | `attributes.test` (kind, mocks), `tests` edges |
| `spring_security` | `SecurityFilterChain` / `configure(HttpSecurity)` (lambda and chained DSL), `@EnableMethodSecurity`, `WebSecurityCustomizer.ignoring()`, CORS, `@ConditionalOnProperty` / `@Profile`, risky properties | `security_posture:application` node (chains, method security, CORS, findings), `attributes.access` on every endpoint (level, roles, decisions, alternatives, findings) |
| `security` | Java invocations matched against `security_catalog.yaml` (sources, sinks, sanitizers), MyBatis / `@Query` SQL, trust-all `TrustManager` / `HostnameVerifier`, `"redirect:" + …` returns | `attributes.security` (`sources`, `sinks` with CWE, severity, status and origins, `sanitizers`) on methods; summary in `get_project_overview` |

**Contract-first APIs.** With openapi-generator (`interfaceOnly`), the `@GetMapping`... annotations live in
interfaces generated at build time, which are not in the repository. The `openapi` enricher reads the
specification and matches each operation to the controller method implementing it (interface named
`camelize(tag) + "Api"`, e.g. `owner-v2` → `OwnerV2Api`, or the first path segment without tag; method named
after the `operationId`). The endpoint path is the controller's class-level `@RequestMapping` followed by the
path of the specification, and the endpoint gets the summary, parameters, request / response schemas of the
contract (`source: "openapi"`, `openapi: {operation_id, tags, responses...}`). Operations without
implementation are kept with `implemented: false`.

MockMvc requests always target the application itself: a request that matches no endpoint is recorded on the
test method (`attributes.test.unmatched_requests`) instead of creating an external endpoint.

Constants (`@RequestMapping(ApiPaths.ORDERS)`) and `${...}` placeholders are resolved when their value is
known. Uncertain links are never guessed silently: injections into an interface with several
implementations get one edge per candidate with `confidence: "ambiguous"`, rules read from a
`SecurityFilterChain` are marked `confidence: "inferred"`.

**Secrets.** Configuration values whose key looks secret (`password`, `secret`, `token`, `credentials`,
private / API / access keys...) and credentials embedded in URLs are replaced by `***` in the property
nodes and in the content of the configuration files and their chunks, so they reach neither the graph
file, nor the search index, nor the tool outputs. References such as `${DB_PASSWORD}` are kept.
Secrets hard-coded in source files are masked too (`secrets_masking.py`): assignments and arguments
whose name designates a secret (`JWT_SECRET = "…"`, `setPassword("…")`), secret defaults of `${key:default}`
placeholders, private keys and well-known credential formats (AWS, GitHub, Slack, `sk-…` keys, JWTs). The
fact is kept as a CWE-798 candidate: `attributes.hardcoded_secrets` (line, kind) on the file node and
`hardcoded_secret: true` on the constant. Labels such as `TOKEN_HEADER = "Authorization"` and shell / batch
variables are not masked. `--no-mask-config-values` disables all masking.

**Untrusted content.** The analysed repository may contain text aimed at an AI agent ("ignore the previous
instructions…"). Every tool answer that carries repository content gets `untrusted_content: true`; lines that
look like instructions to an agent are listed in `suspicious_instructions` and a warning is prepended to
`text` (`content_safety.py`). They are reported, not removed.

**Data flow (Java).** Each invocation records a `site` (`line:column`), the shape of each argument
(`arg_shapes`: `literal`, `constant`, `param`, `field`, `local`, `concat`, `format`, `builder`, `call`, `new`,
`lambda`, `expression`) and its `origins` (`param:x`, `field:x`, `call:<site>`, `lambda:x`), computed through
local variables (flow-insensitive def-use, `+=`, `StringBuilder.append`; a local keeps `built_by: "concat"`
when it was built by concatenation). Methods record where their return value comes from and which fields
they write (`attributes.dataflow`). Chained calls (`repository.findById(id).orElseThrow().getName()`,
`var q = em.createQuery(…); q.setParameter(…)`, Lombok builders) are resolved after the whole repository is
known, from declared return types, Spring Data base interfaces and a table of JDK / Jakarta / Spring return
types (`call_resolution.py`). `calls` edges list their call sites (`sites`: file, line, and for each argument
the callee parameter it binds to with its shape and origins); `trace_flow` shows them (`called at file:line`).
This is the foundation of the source → sink analysis below.

**Sources, sinks and sanitizers.** `security_catalog.yaml` lists the dangerous APIs (sinks, with their CWE
and the dangerous argument), the untrusted inputs (sources) and the calls that neutralise a value
(sanitizers: `Long.parseLong`, `UUID.fromString`, HTML / LDAP encoders, `getFileName`...). The `security`
enricher gives every sink call a status from the shape and origins of its dangerous argument:

| Status | Meaning |
|---|---|
| `tainted` | built from an untrusted source in the same method (handler parameter, `request.getParameter`...) |
| `weak_value` | constant known to be weak (`MD5`, `DES`, `AES/ECB`, key written in the source) |
| `dangerous_api` | the API is dangerous whatever its arguments (`ObjectInputStream.readObject`, unhardened XML factory, `new Random()` for a token, trust-all `TrustManager`) |
| `depends_on_param` / `depends_on_field` / `depends_on_call` | built from a method parameter, a field or a call result, and no source reaches it (`reachable_from_source: false` when no caller passes untrusted data) |
| `unknown` | not enough information |
| `mitigated` | dangerous API hardened in the same method (`setFeature(".../disallow-doctype-decl", true)`) |
| `sanitized` | every variable part goes through a sanitizer or is a typed value (number, UUID, date) |
| `safe_by_construction` | constant, or variable data passed as bind parameters (`?`, `#{}`) |

MyBatis `${…}` placeholders are spliced into the SQL text: the statement is reported `depends_on_param` on the
parameter they name. `find_sinks` hides the three safe statuses unless `include_safe=true` or a `status` is
given; on spring-petclinic-rest every JDBC call (87) is `safe_by_construction` or `sanitized`.

**Taint flows (source → sink).** Untrusted data is then followed across methods (`enrichers/taint.py`): an
argument that depends on a source taints the callee parameter it binds to; a call to an interface method
continues in every implementation; a call to a method of the repository is replaced by what that method
returns (method summary: which parameters reach its return value), so a helper such as `normalize(name)`
does not break the chain; a sanitizer met on the way (`FilenameUtils.getName`, `Long.parseLong`...) stops
the flow for the categories it cleans, and so does a parameter of a numeric / UUID / date type. A sink reached
this way becomes `tainted` (its intra-procedural status is kept in `local_status`) and lists up to five
`flows`: the source (method, kind, parameter or call, entry endpoint, or the channel and its publishers for a
message listener) and every call site crossed (`file:line`, parameter bound, interface dispatch), with a
confidence: `high` when every step is resolved, `medium` through an interface with several
implementations or a local variable defined in several ways. `trace_taint` returns these paths and
`find_callers` the transitive callers and entry points of a method. Not modelled: aliasing, reflection,
elements of collections (a collection is one value), control flow (definitions are united), and values
kept in fields between two requests.

**Security posture and access matrix.** The `spring_security` enricher reads the Spring Security configuration
statically (comments ignored). Each filter chain keeps its condition (`@ConditionalOnProperty` evaluated against
the main configuration, `@Profile`), scope (`securityMatcher`), order, authorization rules in declaration order,
CSRF, CORS, headers, session policy, authentication mechanisms and custom filters; method security is enabled or
not, and for which annotations. Every endpoint then gets `attributes.access`: the first matching rule of the
first applicable active chain (or `WebSecurityCustomizer.ignoring()`, the Spring Boot default when Spring
Security has no chain, `public` without Spring Security), refined by `@PreAuthorize` / `@Secured` /
`@RolesAllowed` when method security makes them effective; the other (conditional or inactive) chains are
listed as `alternatives`. Candidate findings: state-changing endpoint reachable without authentication
(CWE-306), annotation not enforced (CWE-862), rule shadowed by an earlier broader rule (CWE-284), CSRF disabled
with sessions (CWE-352), CORS wildcard with credentials (CWE-942), frame options / headers / HSTS disabled,
object fetched by identifier with no reference to the current user (IDOR, CWE-639, confidence `low`), request
body bound to a JPA entity or exposing `role` / `admin` (CWE-915), body without `@Valid`, response type
serialising `password` / `token` fields (CWE-200), exception handlers returning `getMessage()` (CWE-209),
actuator exposure, H2 console, stack traces in errors, insecure cookie flags, secrets written as literals in
the configuration (CWE-798). `get_security_posture` and `list_attack_surface` return them. Configuration
files under `src/test` no longer override the main values.

**Findings, SARIF and dependencies.** `list_findings` gathers every security fact in one list of candidates:
sinks reached by untrusted data (with the taint flow as evidence), weak values and dangerous APIs, secrets
written in the code, the posture findings, and the known vulnerabilities of the dependencies once
`check_dependencies` has run. Each candidate has a stable id (the same across builds), a CWE, a severity, a
confidence, a location and a **hint to verify it** (the request to send, the test to write). Findings in
test code are kept with a low severity; unconfirmed sinks (`depends_on_*`) are left out unless asked.
`export_sarif` / `findings --format sarif` produce a SARIF 2.1.0 log for GitHub code scanning and IDEs.
The `supply_chain` enricher resolves the version of each dependency (declared, Maven dependency management,
Gradle version catalog, Spring Boot group; versions chosen by the Spring Boot BOM are marked `managed` and
not guessed) and links each Java file to the libraries it imports. `get_dependency_usage` tells which entry
points reach the code importing a library, which separates a library alert from a reachable risk.
`check_dependencies` queries [OSV](https://osv.dev) **only when the server runs with `OSV_LOOKUP=1`**: it
sends the dependency coordinates (group, artifact, version) and nothing else, and caches the answers
(`OSV_CACHE_FILE`, 24 h) so that later calls work offline.

**Extending the catalog.** Sinks, sources and sanitizers live in `repo_knowledge_graph/security_catalog.yaml`.
A sink names its `receivers` (simple type names), `members` (`<init>` for a constructor), the dangerous `arg`
(index, `any`, or `none` with `always: true` for an API dangerous whatever its arguments), its `category`,
`cwe` and `severity`, and optionally `dangerous_values` / `safe_values` (regular expressions on a constant
argument), `mitigated_by` (calls that harden it in the same method), `context` / `context_exclude` (regular
expressions on the method and class names). A sanitizer lists its `members`, optional `receivers` and the
`categories` it neutralises (`all` for a conversion to a number). Rebuild the graph after a change.

**Evaluation.** `benchmarks/owasp_benchmark.py` scores the analysis on the
[OWASP Benchmark for Java](https://github.com/OWASP-Benchmark/BenchmarkJava) 1.2 (2,740 labelled test cases).
The score of a category is its true positive rate minus its false positive rate, as in the Benchmark's own
scorecards. The script needs a clone of the Benchmark (`--clone` downloads it):

```bash
python benchmarks/owasp_benchmark.py --benchmark-dir ../BenchmarkJava --clone
```

| Category | TPR | FPR | Score |
|----------|----:|----:|------:|
| Weak cipher | 0.75 | 0.00 | **0.75** |
| Weak hash | 0.69 | 0.00 | **0.69** |
| LDAP injection | 0.96 | 0.69 | 0.28 |
| Cross-site scripting | 0.63 | 0.37 | 0.25 |
| Path traversal | 0.94 | 0.74 | 0.20 |
| XPath injection | 1.00 | 0.80 | 0.20 |
| SQL injection | 0.59 | 0.46 | 0.14 |
| Command injection | 0.80 | 0.68 | 0.12 |
| Weak randomness, insecure cookie, trust boundary | 0 | 0 | 0 (not modelled) |

The average is 0.33 over the 8 modelled categories (0.24 over all 11). Recall is high on injections, but so is the
false positive rate: most safe cases of the Benchmark depend on control flow (a variable overwritten by a
constant, a condition that is always true, an element removed from a collection), which the flow-insensitive
def-use analysis does not distinguish. The remaining weak-hash cases read the algorithm from
`benchmark.properties`, a file outside the Spring configuration. Weak randomness is only reported in a secret
context (method or class name), which the Benchmark servlets do not have.

---

## Configuration

Settings are read from environment variables (and from a `.env` file by the CLI). Command line options win
over environment variables. `.env.example` lists them all.

| Variable | Default | Purpose |
|----------|---------|---------|
| `REPO_PATH`, `REPO_URL`, `GRAPH_FILE`, `CLONE_DIR` | — | Source of the graph (same as `--repo-path`...) |
| `GITHUB_TOKEN` (or `GITHUB_OAUTH_TOKEN`) | — | Token for private repositories; never written to logs, URLs or `.git/config` |
| `TRANSPORT`, `HOST`, `PORT`, `MCP_PATH`, `SERVER_NAME` | `stdio`, `0.0.0.0`, `4000`, `/mcp` | Server |
| `LOG_LEVEL` | `INFO` | Log level (logs go to stderr) |
| `EXTRACT_ENTITIES`, `DESCRIBE_NODES`, `GIT_HISTORY`, `MAX_COMMITS`, `INDEX_NODES` | `true`, `false`, `false`, `200`, `true` | Construction |
| `MAX_FILE_SIZE` | `1000000` | Larger files are skipped (bytes) |
| `ENRICHERS`, `MASK_CONFIG_VALUES`, `MAX_CALL_EDGES` | `auto`, `true`, `0` | Framework enrichment (same as `--enrichers`...) |
| `JAVA_PARSER` | `tree-sitter` | Java parser: `tree-sitter` (Java 8-21) or `javalang` (legacy, Java 8 only, kept for one release) |
| `CODE_CHUNK_SIZE`, `CODE_CHUNK_OVERLAP` | `2000`, `0` | Chunking (characters) |
| `INDEX_TYPE`, `INDEX_BACKEND`, `LANCEDB_PATH` | `keyword-only`, `lancedb`, `./local_code_index_db` | Search index |
| `WEAVIATE_HOST`, `WEAVIATE_PORT`, `WEAVIATE_GRPC_PORT`, `ALPHA_SEARCH_VALUE` | `localhost`, `8080`, `50051`, `0.8` | Weaviate backend |
| `EMBEDDER_TYPE`, `EMBED_MODEL_NAME` | `sentence-transformers`, `Salesforce/SFR-Embedding-Code-400M_R` | Embeddings |
| `OPENAI_EMBED_BASE_URL`, `OPENAI_EMBED_TOKEN` | `http://0.0.0.0:8001/v1` | OpenAI-compatible embedding API |
| `OPENAI_BASE_URL`, `OPENAI_TOKEN`, `MODEL_NAME` | `http://0.0.0.0:8000/v1` | OpenAI-compatible chat API (`--describe`) |
| `LLM_CONCURRENCY`, `REQUEST_TIMEOUT`, `STOP_AFTER_ATTEMPT`, `WAIT_BETWEEN_RETRIES` | `8`, `240`, `5`, `2` | LLM calls (only network errors, timeouts, rate limits and 5xx are retried) |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` | — | Langfuse tracing of tool calls (`observability` extra); disabled when unset |
| `OSV_LOOKUP`, `OSV_CACHE_FILE` | `false`, `~/.cache/repo-knowledge-graph/osv.json` | Opt-in OSV lookup of `check_dependencies` (sends dependency coordinates to osv.dev) and its cache |

---

## Docker

```bash
cp .env.example .env            # set REPO_URL, or REPO_HOST_PATH to mount a local repository
docker compose up mcp-server    # http://localhost:4000/mcp  (health: /health)
```

| Command | Purpose |
|---------|---------|
| `docker compose up mcp-server` | MCP server over HTTP; serves `GRAPH_FILE`, `REPO_URL` or the repository mounted from `REPO_HOST_PATH` (read-only, on `/repo`) |
| `docker compose --profile build run --rm graph-builder` | Write `./data/graph.json`; then set `GRAPH_FILE=/data/graph.json` to serve it |
| `docker compose --profile weaviate up` | Start Weaviate as well (with `INDEX_BACKEND=weaviate`) |
| `docker compose --profile tests run --rm tests` | Run the test-suite inside the image |

`./data` holds graphs, the search index and the model cache. The image runs as a non-root user.
To build a lighter image without torch (keyword search only):

```bash
docker build --build-arg EXTRAS=cpp,observability -t repo-kg .
docker run --rm -p 4000:4000 -v "$PWD/../my-project:/repo:ro" -e REPO_PATH=/repo repo-kg
```

---

## Examples

`examples/` contains clients to run against a started server (`MCP_SERVER_URL`, default
`http://localhost:4000`):

| Script | What it shows |
|--------|---------------|
| `mcp_client_demo.py` | Scripted walk-through of every tool, the way an LLM would use them |
| `mcp_error_handling_demo.py` | Error responses for invalid inputs |
| `smolagent_remote.py`, `smolagent_local.py` | A [smolagents](https://github.com/huggingface/smolagents) agent answering questions with the server's tools (needs an LLM endpoint) |
| `vllm_build_graph.py` | Building a graph with embeddings served by vLLM |

---

## Development

```bash
pip install -e ".[cpp,dev]"
pytest                                          # hermetic: models are replaced by a fake service
ruff check . && mypy
```

Some tests are skipped unless their environment is available:

| Marker | Enable with |
|--------|-------------|
| `real_models` | `KG_REAL_MODELS=1` (downloads embedding models) |
| `weaviate` | `WEAVIATE_HOST=localhost` and a running Weaviate |
| `network` | `KG_NETWORK_TESTS=1` (clones a public repository) |
| `clang` | libclang installed (`cpp` extra) |

`tests/fixtures/mini_repo` is a small Python / TypeScript / JavaScript / Java repository used by the
end-to-end tests. `tests/fixtures/spring_shop` is a small Spring Boot 3 / Java 21 multi-module application
(controllers, services, Lombok, JPA, Flyway, Kafka, Feign, security, profiles, tests) used by the enrichment
and Spring MCP tool tests, including a precision / recall check of the `calls` edges.
`tests/fixtures/openapi_shop` is a contract-first API (OpenAPI specification and controllers implementing the
generated interfaces), `tests/fixtures/graphql_mybatis` a GraphQL (DGS and Spring for GraphQL) + MyBatis
application. `KG_NETWORK_TESTS=1` also runs the enrichment on clones of spring-petclinic, spring-petclinic-rest
and the RealWorld Spring Boot example (REST + GraphQL + MyBatis). CI (`.github/workflows/ci.yml`) runs lint, type checks and the tests on Python 3.10, 3.12
and 3.13, plus the Weaviate tests against a service container.

---

## Project layout

```
repo_knowledge_graph/
├── knowledge_graph.py          # public API: build, update, navigate, save / load
├── builder.py                  # pipeline: analyse files, describe chunks, assemble the graph
├── entity_registry.py          # entities, aliases, call resolution
├── entity_extractor.py         # AST extractors per language
├── java_extractor.py           # Java extractor (tree-sitter): attributes, positions, typed calls, Lombok, data flow
├── call_resolution.py          # second pass: chained calls (return types, Spring Data, library table)
├── secrets_masking.py          # secrets hard-coded in source files (masked, kept as CWE-798 candidates)
├── content_safety.py           # repository content flagged as untrusted in tool answers
├── security_catalog.yaml       # sources, sinks (CWE) and sanitizers; security_catalog.py loads it
├── findings.py                 # candidate findings (stable ids, hints) and SARIF 2.1.0 export
├── vulnerability_lookup.py     # opt-in OSV client with a local cache
├── entity_chunk_mapper.py      # maps file-level entities to chunks (when positions are unknown)
├── enrichers/                  # framework knowledge: build_files, spring_config, spring_beans, spring_web,
│                               #   openapi, web_routes, graphql, spring_data_jpa, mybatis, spring_messaging,
│                               #   spring_tests, spring_security, supply_chain, security (+ taint.py; base.py: helpers)
├── framework_queries.py        # queries over the enrichment (endpoints, trace_flow, beans, data model...)
├── framework_tools.py          # MCP tools over the enrichment
├── code_parser.py              # language-aware chunking
├── languages.py                # table of supported languages
├── code_index.py               # LanceDB / Weaviate search index
├── model_service.py            # embedding and LLM services
├── serialization.py            # JSON format (schema_version 3, reads older files)
├── queries.py                  # traversal (paths, subgraphs, tree)
├── git_history.py              # commits, diffs, authenticated cloning
├── mcp_server.py               # MCP tools
├── cli.py, __main__.py         # command line
├── observability.py            # optional Langfuse tracing
├── settings.py                 # environment configuration
└── utils/                      # file reading, paths and aliases, chunk helpers, logging
benchmarks/                     # OWASP Benchmark scoring script
examples/                       # MCP client demos
tests/                          # pytest suite and fixtures
compose.yml, Dockerfile         # container setup
```

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `search_nodes` returns `invalid_input: No code index` | The graph was built or loaded with `--no-index`; restart without it |
| A C/C++ repository has no entities | Install the `cpp` extra (libclang) |
| `ImportError: sentence-transformers is required` | Install the `st` extra, or use `--index-type keyword-only` / `--embedder openai` |
| The stdio client reports a protocol error | Custom code must not print to stdout; the server itself logs to stderr |
| `diff_file_versions` is unavailable | The repository must be on disk: `--repo-path`, or `--repo-url` with `--clone-dir` |
| `get_file_history` is unavailable | Build the graph with `--git-history` |
| First start with embeddings is slow | Models are downloaded once (cache in `HF_HOME`); index reuse makes later starts fast |
| A private repository cannot be cloned | Set `GITHUB_TOKEN` |
| No endpoints / beans on a Spring project | Check `--enrichers` (not `none`) and that `.java`, `pom.xml` / `build.gradle` and `application*` files are not excluded by `--extension` / `--skip-dir` |
| Endpoints missing on a contract-first project | The OpenAPI specification must be in the repository (any `.yml` / `.yaml` / `.json` file starting with `openapi:` / `swagger:`); custom generator naming (`apiNameSuffix`...) falls back to matching the `operationId` alone |
| A configuration secret appears in the output | Make sure the graph was not built with `--no-mask-config-values`; only `application*` / `bootstrap*` files are masked |
| Graph too large on a big Java monorepo | `--max-call-edges N` caps `calls` / `uses_type` edges; `--enrichers none` skips the framework layer |

---

## Limitations

- Entity resolution is name based (aliases and imports), not type-checked: dynamic calls such as
  `obj.method()` on an object of unknown type stay unresolved instead of being linked to a guess. For Java,
  calls are resolved with the declared type of fields, parameters and local variables; chained calls on
  expressions of unknown type (`a.b().c()`) and methods inherited from external classes are not linked.
- The Spring analysis is static: the application context is never started, so auto-configured beans
  (`DataSource`, `KafkaTemplate`...), conditional beans and active profiles are approximated; Gradle files
  are read without being executed. Kotlin Spring projects are not covered. MyBatis dynamic SQL is flattened
  (every `<if>` branch is kept) and `${...}` table names are not resolved; GraphQL field resolvers on non-root
  types are recorded as attributes, not endpoints.
- On the OWASP Benchmark for Java the analysis scores 0.75 (weak ciphers) and 0.69 (weak hashes) with no false
  positive, and 0.12 to 0.28 on the injection categories: recall is high but so is the false positive rate,
  because the flow-insensitive def-use cannot tell a variable overwritten by a constant or a branch that
  never runs. Dependency versions managed by the Spring Boot BOM are not resolved offline.
- The access matrix is static: Spring Security DSL written through helper methods or custom
  `AuthorizationManager` beans, `@Profile` activation outside the configuration files and servlet filters
  registered elsewhere are approximated (`unknown` level, `conditional` status).
- Security facts are candidates, not verdicts: the taint analysis does not model aliasing, reflection,
  collection elements, control flow or values kept in fields between requests, and a method overloaded
  several times shares one entity (parameters are bound by position).
- When several files declare an entity with the same name, they share one entity node with several
  declaring chunks.
- Import edges are resolved for Python, Java, JavaScript and TypeScript only.
- The graph is kept in memory; very large monorepos need a correspondingly large amount of RAM.

---

## Scope and responsible use

This is not a replacement for CodeQL or Semgrep. The aim is to give an agent a map of the code and paths it can
verify (`file:line` at every step); results from those tools can be brought in as SARIF and linked to the
endpoints that reach them.

The security analysis is meant for auditing code you own or are authorised to review: defensive code review,
security assessments and teaching. It never runs the analysed code and does not generate exploits. See
[SECURITY.md](SECURITY.md) to report a vulnerability in the tool itself.

---

## Roadmap

Contributions are welcome on any of these:

- **Languages**: Kotlin Spring projects (tree-sitter-kotlin, same attributes as the Java extractor); move C/C++
  extraction to tree-sitter and drop libclang; remove the legacy `javalang` Java parser.
- **Precision**: measure the precision and recall of `called_by` edges on an annotated multi-language
  repository (today only measured on the Java fixture).
- **Security analysis**: flow-sensitive def-use for constant branches and overwritten variables (the main
  source of false positives on the OWASP Benchmark); read `.properties` files outside the Spring
  configuration; rules for cookies without `secure` and trust-boundary violations.
- **Evaluation**: an agentic benchmark (questions answered by an agent with and without the tools); WebGoat.
- **History**: chunk-level diff between two versions of a file.
- **MCP**: prompts for common workflows (security audit, request tracing).

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup, the checks to run and the design notes.

---

## License

Apache License 2.0, see [LICENSE](LICENSE) and [NOTICE](NOTICE).
