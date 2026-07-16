# KnowledgeHub V2.0.1 index-validation report

## Scope

This patch closes one explicit V2 acceptance gap: the task specification lists
`knowledgehub validate index code|writing`, but V2.0.0 exposed only source,
normalized, Writing-entry and aggregate validation. The patch is strictly
read-only and does not rebuild, repair, promote, delete or migrate an index.

## Validation chain

For Code and Writing, validation now proves the chain from source record to
domain SQLite state, deterministic Chunk artifact and Qdrant point. Checks
include:

- State schema readability, SHA-256 fields, processor version and active versus
  tombstone consistency.
- One deterministic artifact per active document, no unknown artifacts, valid
  JSON, contiguous Chunk indexes, globally unique Chunk IDs and text hashes.
- Code content/Metadata hashes against version-scoped normalized records, plus
  library, version, commit, source type and source URL.
- Writing identity, paper/function provenance and State hashes against the
  derived entry and artifact.
- Qdrant health, info versus exact count, local versus remote point count, and
  bidirectional Chunk/point/document/knowledge-base membership.

`--offline` validates through local Chunk artifacts and emits
`qdrant_not_checked`. Online validation is the default for an explicit index or
aggregate check.

## Compatibility finding

The first real check showed that frozen `rules-v1` Writing chunks do not carry
`metadata.writing_id`; their canonical `document_id` is the Writing entry ID.
This is a compatible historical representation, not corruption. V2.0.1 accepts
the frozen identity path and strictly checks `metadata.writing_id` whenever a
newer processor supplies it. No Writing data was rewritten.

## Real verification

- Code: 120 active State documents, 120 artifacts, 1,106 chunks and 1,106
  Qdrant points; exact membership matched; collection status green.
- Writing: 134 active State documents, 134 source entries, 134 artifacts and
  134 Qdrant points; exact membership matched; collection status green.
- Code used stable alias `knowledgehub_code_current`; Writing used physical
  collection `knowledgehub_writing_qwen3_4b_1024_v1`.
- Aggregate `knowledgehub validate all` passed all five source, derived and
  index checks in one online run.
- 326 tests passed; Ruff, strict MyPy across 110 source files and
  `git diff --check` passed.

## Deferred gap

The unified `TaskStore` exists and has idempotency, terminal states and expiring
locks, but it is not yet wrapped around every Code sync/build and Writing derive
execution path. That integration remains the next bounded hardening round; it
is not hidden by this index-validation patch.
