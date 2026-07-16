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
