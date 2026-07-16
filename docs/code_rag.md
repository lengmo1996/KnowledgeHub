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
knowledgehub build code --library transformers --incremental
```

The default Transformers strategy selects the installed stable version, the
nearest earlier stable tag and the nearest later stable tag when available.
Each checkout is shallow, sparse, detached at a resolved tag and recorded with
its full commit SHA. Full history, all tags, Issues and PRs are not downloaded.
The source-selection configuration is fingerprinted; changing include/exclude
or bounds publishes a parallel checkout instead of silently reusing incomplete
localized files.

Source data is stored under `/data/KnowledgeHub/code/sources`, normalized
documents under `normalized`, sync/build manifests under `manifests`, and
consumer state under `state`. Repeating an unchanged sync or build is
idempotent. `--prune` removes stale vectors and creates tombstones but retains
downloaded and normalized artifacts.

Normalized manifests are version-scoped and never overwrite another version.
Because index state is shared across Code versions, `--prune` is rejected for
version-filtered or limited builds and is valid only for a complete build.

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

## Configuration and failures

Libraries are defined in `configs/sources/code.yaml`. Additions require only a
new registry record. Missing installed packages, unresolved official tags,
Git/HTTP timeouts, file limits and parse failures are explicit errors. Tokens
are read from `GITHUB_TOKEN`; unauthenticated Git and Release reads remain
available subject to upstream rate limits.
