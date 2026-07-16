# Multi-RAG implementation report

## Result

KnowledgeHub now provides isolated Literature, Code and Writing knowledge bases
over shared Chunk, embedding, sparse encoding, Qdrant and retrieval components.
The existing Zotero source, manifests, pipeline state, parsed artifacts, CLI
defaults and `zotero_papers_qwen3_4b_1024_v2` collection were not migrated or
rewritten.

The pre-change baseline was a clean `main` worktree with 266 passing tests,
3,574 current Literature manifest records and 3,497 parsed/chunk artifacts. The
final suite contains 278 passing tests.

## Implemented architecture

```text
configs/
├── knowledgehub.yaml
└── sources/code.yaml
src/knowledgehub/
├── code_rag/          # registry, environment, Git/Release sync, parsers/build
├── writing_rag/       # analyzer protocol, rules and Literature derivation
├── hub/               # collection catalog and unified query routing
├── indexing/incremental.py
└── core/models.py
tests/multi_rag/       # offline registry/Git/AST/index/query/writing coverage
docs/
├── architecture_review.md
├── multi_rag_extension_design.md
├── code_rag.md
├── writing_rag.md
├── data_sources.md
└── skill_integration.md
```

Runtime data remains outside Git:

```text
/data/KnowledgeHub/code/{sources,normalized,manifests,state,logs}
/data/KnowledgeHub/writing/{derived,manifests,state,logs}
/data/KnowledgeHub/rag/{code,writing}
```

Code uses `knowledgehub_code_qwen3_4b_1024_v1`; Writing uses
`knowledgehub_writing_qwen3_4b_1024_v1`. Deletion reconciliation removes active
vectors and records tombstones only during an explicit complete prune. Bounded
or filtered builds reject prune.

## Code RAG verification

- Captured the local Python/package environment in dry-run mode; Transformers
  resolved to 5.13.1.
- Synchronized official tags 5.13.0, 5.13.1 and 5.14.0 as shallow sparse
  checkouts fixed to full commit SHAs. Registry selection configuration is also
  fingerprinted, and license metadata resolves to `LICENSE`.
- Built bounded real indexes for 20 documents from 5.13.0 (213 chunks) and 20
  documents from 5.13.1 (216 chunks). Repeating the unchanged 5.13.1 build
  skipped all 20 documents before the final license-metadata refresh.
- A real hybrid compatibility query returned both versions from the v5
  Migration Guide and labelled 5.13.0 as `target_version_evidence`, 5.13.1 as
  `current_version_evidence`, with `inference=false`, tag, commit, path and
  official source URL.
- A complete three-version dry-run selected 12,138 documents. It was not
  embedded because the first release is intentionally bounded; operators can
  expand by version after reviewing the plan.

GitHub's unauthenticated Releases API returned HTTP 403 rate limiting. Repository
sync results were retained and the manifest reports `status=partial` with
`release_error=github_releases_http_403`; the CLI returns non-zero for this
partial result. Set `GITHUB_TOKEN` and rerun sync to fill Release records without
redownloading unchanged checkouts.

## Writing RAG verification

- Read the Literature pipeline database through SQLite read-only mode and used
  canonical parsed Markdown, never vector reconstruction.
- A bounded five-paper run derived and indexed 134 rule-analyzed entries.
- Indexed text contains the function, abstract pattern, rhetorical structure,
  usage guidance and research domains; full original and normalized text remain
  only in the local derived JSONL. The vector payload contains a short source
  excerpt for provenance.
- A real hybrid query filtered to Introduction/research-gap returned
  `pattern_first` results with abstract pattern, rhetorical structure, usage
  guidance, source paper ID/title, quality and confidence.
- An immediate repeated derivation skipped all 134 entries, verifying content,
  metadata, processor and embedding idempotency.

## Interfaces

Existing `knowledgehub zotero`, `knowledgehub rag` and `knowledgehub mcp`
commands remain available. New commands cover source inspection, environment
capture, Code sync/build, Writing derivation and unified queries. HTTP/MCP
`rag_search` defaults to Literature when `knowledge_base` is omitted. MCP now
also exposes `rag_compare_versions` and `writing_patterns`; all nine schemas are
strict and read-only.

## Test and compatibility results

- `pytest -q`: 278 passed.
- `ruff check src tests`: passed.
- `mypy src/knowledgehub`: passed in strict mode.
- `git diff --check`: passed; no credentials or private runtime payloads are
  tracked.
- Real legacy `knowledgehub rag query` succeeded against
  `zotero_papers_qwen3_4b_1024_v2` after the extension.
- Offline tests use temporary Git repositories, fake embedding/Qdrant services
  and deterministic Writing analysis; they require no network or user papers.

## Known limits and next steps

- Version 5.14.0 source is synchronized but was not embedded in the bounded
  first release.
- Release records await an authenticated/rate-limit-available GitHub API call.
- Issue/PR/commit ingestion remains disabled and bounded by design.
- The rules Writing analyzer is reproducible but semantically limited; a model
  analyzer can implement the existing protocol while retaining prompt/model
  provenance.
- No automatic timers or full-library/full-paper builds were enabled.
