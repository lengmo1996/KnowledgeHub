# V1 to V2 migration

V2 never rewrites a V1 JSONL in place. Use:

```bash
python migrations/v1_to_v2.py normalized_document v1.jsonl v2.jsonl
```

The destination contains `{schema_name, schema_version, data}` envelopes.
Readers reject unknown names, missing required fields and versions other than
the registered `2.0`. Index collections are not migrated automatically; create
a Qdrant snapshot before a candidate rebuild and retain the V1 collection.

Rollback is confirmation-gated:

```bash
knowledgehub index snapshot code
knowledgehub index list-snapshots code
knowledgehub index rollback code <snapshot-id> --yes
```

Test recovery on a non-production collection before relying on a server-level
snapshot path in a deployment with a different Qdrant storage layout.

V2 Integration adds `knowledge_query` and `/knowledge/query` without changing
the raw `/search` or `rag_search` response. Existing Skills may migrate
incrementally. Prefer the evidence envelope when the caller needs a bounded,
source-labelled context; keep raw search only for clients that already perform
their own evidence normalization.

`query_result@2.0` is the only externally flattened V2 schema: required evidence
fields remain at the response top level so Skills do not need to unwrap `data`.
`SchemaRegistry` validates this explicit form and normalizes it to an internal
`SchemaEnvelope`; other V2 schemas still require the standard `data` object.

Package `0.2.0` is the frozen V2 boundary. Validate a checkout before operating
on runtime state:

```bash
knowledgehub release validate
knowledgehub validate all
```

The release check is offline and verifies committed config digests. It does not
promote, rebuild, roll back or clean a collection. See `docs/v2_release.md` for
the frozen point counts and optional read-only runtime checks.
