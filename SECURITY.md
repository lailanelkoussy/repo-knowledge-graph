# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security problem in repo-knowledge-graph itself. Report it privately
through GitHub: **Security → Report a vulnerability** on the repository page
(<https://github.com/lailanelkoussy/repo-knowledge-graph/security/advisories/new>).

Include the version or commit, how to reproduce the problem and its impact. We aim to acknowledge reports within
a week and will keep you informed while a fix is prepared.

Examples of what we consider in scope:

- a secret from an analysed repository (configuration value, hard-coded credential, `GITHUB_TOKEN`) that is
  written to logs, the saved graph, the search index or an MCP answer despite masking;
- a way for an analysed repository to execute code or read files outside of itself during graph construction;
- a way for an MCP client to read files outside the analysed repository through the server's tools;
- network access that happens without the corresponding opt-in (`OSV_LOOKUP`, `--repo-url`).

## Intended use of the security analysis

The security features (taint flows, sink classification, security posture, dependency checks, SARIF export)
are meant for auditing code **you own or are authorised to review**: defensive code review, security
assessments and teaching. They report *candidates to verify* with `file:line` evidence, never verdicts.

The tool never runs the analysed code and does not generate exploits. Content read from an analysed
repository is untrusted: tool answers flag it as such, because a comment or a string in the code can contain
instructions aimed at the agent reading it (prompt injection).
