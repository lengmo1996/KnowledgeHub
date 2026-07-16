# KnowledgeHub V2 architecture

V2 is additive over the frozen V1 contracts. Governance introduces strict
schema envelopes, explicit migrations, unified task/idempotency/lock state,
Qdrant snapshot manifests, candidate collection registration, atomic stable
aliases and cross-domain validation. Existing physical collections and the
embedding model are unchanged. A successful promotion writes an alias pointer;
without that pointer, the original YAML collection remains the query target.

Code intelligence adds five layout adapters, canonical version identities, a
SQLite exact-symbol catalog, AST relations and deterministic signature diffs.
Vector retrieval remains responsible for semantic evidence; symbol lookup is
the exact path for qualified names. Repository Intake produces profiles and
conservative compatibility matrices but never executes or installs target code.

Writing V2 adds paragraph moves, separately sourced personal/venue profiles,
internal-source similarity risk and durable feedback. It does not claim legal
plagiarism detection. Evaluation metrics remain separate from generation.

Runtime governance lives under `/data/KnowledgeHub/{state,indexes}`. V1 data is
read as V1 until explicitly migrated to a different destination.

The V2 release boundary is represented by
`state/releases/v2_manifest.json`. Its offline validator checks strict release
metadata and repository config hashes without contacting runtime services.
Qdrant health, aliases and point counts are separate read-only observations so
a temporarily unavailable service cannot be mistaken for config drift. The
manifest records the completed pre-freeze implementation commit; the commit
that contains the manifest is the release commit, which avoids self-reference.

V2.0.1 closes the index-integrity acceptance gap with a read-only chain:

```text
source record -> domain SQLite state -> Chunk artifact -> Qdrant point
```

The validator checks active/tombstone consistency, content and Metadata hashes,
artifact naming, contiguous Chunk indexes, duplicate IDs, source provenance,
point counts and bidirectional point membership. `validate index code|writing`
checks Qdrant by default; `--offline` stops at local artifacts. `validate all`
includes both indexes. Frozen `rules-v1` Writing identity is resolved from
`document_id`, while newer `metadata.writing_id` values are checked when present.

V2.0.2 connects the central TaskStore to mutating Code and Writing CLI paths.
One idempotency key represents a logical operation, while an append-only
`attempts` table preserves every execution. Equivalent running tasks are
rejected before work starts. Resource locks are acquired in sorted order and
always released in reverse order; library locks serialize source mutation and
index locks serialize writes to one physical collection.

Completed operations may run again, preserving release checks and incremental
build semantics. Failed or partial reruns increment `retry_count`. A running
task older than the six-hour TTL is closed as `stale_task_recovered`, its stale
locks are removed and a retry attempt starts. Dry-run commands bypass TaskStore
so planning remains free of persistent state. The existing Literature pipeline
keeps its own frozen state machine.
