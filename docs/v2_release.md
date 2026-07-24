# KnowledgeHub V2 release freeze

KnowledgeHub V2.0.6 is the current frozen patch. The machine-readable source of
truth is `state/releases/v2_0_6_manifest.json`; it records the pre-freeze
implementation commit, configuration hashes, pinned upstream commits, index
evidence, interface counts, evaluation gates, dependency/source-diff evidence,
V3 Workspace and real-project admission boundaries, Writing Materials
governance, feedback-integrity evidence and known limits.
The Git commit containing the manifest is the release commit, avoiding a
self-referential commit hash inside the same file.

The original V2.0.0 boundary remains immutable in
`state/releases/v2_manifest.json`. Pass that path explicitly to validate the
historical release.

V2.0.1 also remains immutable in `state/releases/v2_0_1_manifest.json`.

V2.0.2 also remains immutable in `state/releases/v2_0_2_manifest.json`.

V2.0.3 also remains immutable in `state/releases/v2_0_3_manifest.json`.

V2.0.4 also remains immutable in `state/releases/v2_0_4_manifest.json`.

V2.0.5 also remains immutable in `state/releases/v2_0_5_manifest.json`.

## Deterministic validation

The release validator reads only the manifest and repository files. It does
not contact Qdrant, GitHub, Zotero or a model and does not repair state.

```bash
knowledgehub release validate
knowledgehub release validate state/releases/v2_manifest.json \
  --repository-root /path/to/KnowledgeHub
```

The command fails when the schema is incompatible, a required field is
missing, a source/config digest is malformed, a config has drifted, an index
evidence record is incomplete, a test gate is not green or a repository path
escapes the selected root. Runtime availability remains a separately recorded
observation.

## Frozen runtime

The final read-only check used the configured Qdrant endpoint
`http://127.0.0.1:6333` and observed:

| Knowledge base | Physical collection | Points | State |
|---|---|---:|---|
| Literature | `zotero_papers_qwen3_4b_1024_v2` | 190,131 | green |
| Code | `knowledgehub_code_current` | 1,118 | green |
| Writing | `knowledgehub_writing_current` | 1,107 | green |

`knowledgehub_code_current` points to the 1,118-point Code collection.
`knowledgehub_writing_current` points to the previously promoted, validated
Writing Materials `quality_v2` release. The V2.0.6 freeze did not rebuild,
promote, roll back or write any collection.

## Operator checks

These commands are read-only except for starting an already configured local
Qdrant container in the optional first command:

```bash
# Only if Qdrant is not already running.
docker compose -f deploy/qdrant/compose.yaml --profile core up -d qdrant

knowledgehub release validate
knowledgehub validate all
knowledgehub validate dependencies --offline
knowledgehub index alias-status code
knowledgehub evaluate run --mode offline --profile v2 --output /tmp/kh-v2-offline.json
knowledgehub evaluate run --mode live --profile v2 --output /tmp/kh-v2-live.json
knowledgehub repository validate /path/to/repository --output-root /data/KnowledgeHub/reports
```

Do not use `build`, `derive`, `index promote`, `rollback`, `clean --execute` or
`prune --execute` merely to validate a release. Those commands change derived
or runtime state and require a separate operational decision.

From V2.0.1, `validate all` performs read-only Qdrant membership checks in
addition to local source/state/artifact checks. Use `validate all --offline`
when Qdrant is intentionally stopped. An offline result includes
`qdrant_not_checked`; it verifies local integrity but does not claim collection
point consistency.

## Rollback and compatibility

V1 data and collection names remain valid. Stable query defaults still select
Literature; `/search` and `rag_search` retain their prior request/response
contracts. V2 evidence consumers can use `knowledge_query`,
`POST /knowledge/query` or CLI `--evidence-envelope`, all returning
`query_result@2.0`. Snapshot and alias rollback remain explicit and
confirmation-gated as documented in `docs/v2_migration.md`.
