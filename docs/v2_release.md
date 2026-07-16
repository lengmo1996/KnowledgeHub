# KnowledgeHub V2 release freeze

KnowledgeHub V2.0.2 is the current frozen patch. The machine-readable source of
truth is `state/releases/v2_0_2_manifest.json`; it records the pre-freeze
implementation commit, configuration hashes, pinned upstream commits, index
evidence, interface counts, evaluation gates, cleanup audit and known limits.
The Git commit containing the manifest is the release commit, avoiding a
self-referential commit hash inside the same file.

The original V2.0.0 boundary remains immutable in
`state/releases/v2_manifest.json`. Pass that path explicitly to validate the
historical release.

V2.0.1 also remains immutable in `state/releases/v2_0_1_manifest.json`.

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
| Code | `knowledgehub_code_qwen3_4b_1024_v1` | 1,106 | green |
| Writing | `knowledgehub_writing_qwen3_4b_1024_v1` | 134 | green |

`knowledgehub_code_current` points to the Code collection. The V2 freeze did
not rebuild, migrate or write the Literature collection. Writing remains the
134-entry `rules-v1` active index; the `rules-v2` candidate was intentionally
not published.

## Operator checks

These commands are read-only except for starting an already configured local
Qdrant container in the optional first command:

```bash
# Only if Qdrant is not already running.
docker compose -f deploy/qdrant/compose.yaml --profile core up -d qdrant

knowledgehub release validate
knowledgehub validate all
knowledgehub index alias-status code
knowledgehub evaluate run --mode offline --profile v2 --output /tmp/kh-v2-offline.json
knowledgehub evaluate run --mode live --profile v2 --output /tmp/kh-v2-live.json
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
