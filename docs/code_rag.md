# Code RAG

Code RAG localizes bounded official technical sources and keeps versions
coexistent. Its default collection is
`knowledgehub_code_qwen3_4b_1024_v1`; it never writes to the Literature
collection.

## Workflow

```bash
knowledgehub source inspect transformers
knowledgehub environment capture --name rag
knowledgehub sync code --library transformers --version installed --dry-run
knowledgehub sync code --library transformers --version installed
knowledgehub build code --library transformers --version 5.13.1 --limit 20 \
  --candidate-collection knowledgehub_code_smoke_<unique-id>
knowledgehub index validate-candidate code knowledgehub_code_smoke_<unique-id>
knowledgehub source dependencies transformers --version 5.13.1
knowledgehub symbol build transformers 5.13.0
knowledgehub symbol build transformers 5.13.1
knowledgehub build diff --library transformers \
  --from-version 5.13.0 --to-version 5.13.1 --limit 20
```

The default Transformers strategy selects the installed stable version, the
nearest earlier stable tag and the nearest later stable tag when available.
Each checkout is shallow, sparse, detached at a resolved tag and recorded with
its full commit SHA. Full history, all tags, Issues and PRs are not downloaded.
The source-selection configuration is fingerprinted; changing include/exclude
or bounds publishes a parallel checkout instead of silently reusing incomplete
localized files.

Source data is stored under `/data/KnowledgeHub/code/sources`. Every writable
Code build must target a new physical candidate. Its normalized documents,
SQLite state and chunk artifacts are isolated under
`/data/KnowledgeHub/code/releases/code/<candidate>`; a validated release is
immutable. Direct writes to the configured production collection and stable
alias are rejected. Repeating an unchanged sync is idempotent.

Normalized manifests are version-scoped and never overwrite another version.
`--prune` is rejected for version-filtered or limited builds. Fresh complete
release candidates do not need pruning.

Use `index bootstrap-candidate code <new-collection>` for an atomic maintenance
release equivalent to the current active document scope. `build code --all`
expands to every localized source file and is not promotion eligible unless the
operator also supplies `--allow-source-expansion` explicitly.

Use `--version` and `--limit` for bounded smoke builds before a complete
version. Per-file chunk caps in the registry prevent pathological generated
source files from creating an unbounded indexing job.

## Processing

- Python uses AST nodes for modules, classes, functions and methods, retaining
  symbol hierarchy and line ranges.
- Markdown, MDX and RST are grouped by heading while adjacent code fences stay
  with their explanatory section.
- README, tutorials, examples, migration files and changelogs receive distinct
  `source_type` values.
- Releases are categorized as breaking changes, deprecations, bug fixes,
  migration, security, performance, known issues or features.

Every result carries library, package, version, repository, tag, commit, path,
symbol/section, source URL, content hash and retrieval timestamp. Compatibility
queries additionally label current-version, target-version and change evidence;
retrieval evidence is not represented as an official conclusion.

Dependency manifests are version/commit pinned and generated only from static
project files. PEP 621, requirements files, setup.cfg and literal
`setup(install_requires=[...])` records are declaration evidence. A static
`setup.py:_deps` table is retained as `dependency_catalog` with
`relation=lists_dependency`, not promoted to a runtime requirement. Validate
all manifests and their current source markers with
`knowledgehub validate dependencies --offline`.

Version-diff builds align exact Symbol Catalog entries from two already
synchronized versions. They emit bounded `version_diff` documents with both
commits, line locations, deterministic signature changes and a source patch.
`evidence_role=system_derived_source_diff` distinguishes derived evidence from
official release prose. Builds are incremental, never prune unrelated Code
documents and require an explicit version pair.

## Configuration and failures

Libraries are defined in `configs/sources/code.yaml`. Additions require only a
new registry record. Missing installed packages, unresolved official tags,
Git/HTTP timeouts, file limits and parse failures are explicit errors. Tokens
are read from `GITHUB_TOKEN`; unauthenticated Git and Release reads remain
available subject to upstream rate limits.
