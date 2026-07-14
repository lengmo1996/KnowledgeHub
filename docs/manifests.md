# Zotero manifest contract (schema v1)

Manifests are the only public handoff from the Zotero source to PDF parsing,
chunking, embedding, and indexing. Consumers must not query the source SQLite
schema or inspect WebDAV ZIPs directly.

The source publishes:

```text
<data_dir>/manifests/
├── documents.jsonl               # current document snapshot
├── collections.json              # current normalized collection hierarchy
├── summary.json                  # current publication summary
├── delta-catalog.jsonl           # ordered delta integrity control plane
└── deltas/<sync_id>.jsonl        # explicit operations for one successful run
<data_dir>/runs/<sync_id>/summary.json
```

All files are UTF-8. JSON uses canonical key ordering and fixed separators;
arrays with set semantics are normalized before serialization. JSONL records
are ordered by `document_id` and every line is independently valid JSON.

The delta catalog is published for every successful run, including metadata
304 runs with an empty delta. Each entry contains a monotonic sequence,
predecessor sync ID, source versions, relative path, SHA-256 and row count.
Incremental consumers validate the entire chain and never infer ordering from
filenames or mtimes. A missing entry, changed hash, predecessor mismatch or
version gap requires snapshot reconciliation. This is control-plane metadata
for the existing delta contract, not a second document manifest.
Candidate files are flushed, fsynced, and atomically published. Consumers
therefore see the previous complete file or the new complete file, never a
partially written snapshot/delta.

## Document identity and snapshot

One API PDF attachment with a valid parent item produces one schema-v1
document, even when its local archive is not ready. The stable ID is:

```text
zotero:<library_type>:<library_id>:<parent_item_key>:<attachment_key>:0
```

The final component is `pdf_index`; it is always `0` in v1. Non-PDF child
items remain in the raw object mirror but do not create fake PDF documents.
Orphan attachments are validation errors.

`documents.jsonl` is the current, non-deleted document set sorted by
`document_id`. A representative ready record is:

```json
{
  "schema_version": 1,
  "document_id": "zotero:user:123456:PARENT01:ABC123:0",
  "source": "zotero",
  "library_type": "user",
  "library_id": "123456",
  "library_version": 789,
  "item_key": "PARENT01",
  "item_version": 120,
  "item_type": "journalArticle",
  "attachment_key": "ABC123",
  "attachment_version": 121,
  "pdf_index": 0,
  "title": "Paper title",
  "creators": [
    {
      "creator_type": "author",
      "first_name": "A",
      "last_name": "B"
    },
    {
      "creator_type": "author",
      "name": "Example Research Consortium"
    }
  ],
  "abstract": "",
  "publication_title": "",
  "date": "2026-01-02",
  "year": 2026,
  "doi": "10.1234/example",
  "url": "https://example.test/paper",
  "language": "en",
  "rights": "",
  "relations": {},
  "tags": ["infrared", "vision"],
  "collections": [
    {
      "key": "COLL01",
      "name": "Object Detection",
      "path": "Research/Infrared Vision/Object Detection"
    }
  ],
  "mime_type": "application/pdf",
  "attachment": {
    "backend": "nutstore_webdav",
    "archive_path": "/data/KnowledgeHub/zotero_cache/ABC123.zip",
    "prop_path": "/data/KnowledgeHub/zotero_cache/ABC123.prop",
    "prop_exists": true,
    "archive_sha256": "<sha256>",
    "archive_size_bytes": 123456,
    "archive_mtime_ns": 123456789,
    "pdf_path": "/data/KnowledgeHub/zotero/extracted/ABC123/paper.pdf",
    "pdf_sha256": "<sha256>",
    "pdf_size_bytes": 120000
  },
  "metadata_fingerprint": "<sha256>",
  "content_fingerprint": "<pdf-sha256>",
  "document_fingerprint": "<sha256>",
  "status": "ready",
  "status_detail": null,
  "updated_at": "2026-07-14T08:00:00Z"
}
```

The `creators` array preserves Zotero's semantic order. A person uses
`first_name`/`last_name`; an institutional creator uses `name`. Tags are
deduplicated and sorted. Collections are sorted by path then key and contain
the normalized display name and full hierarchy, rather than only opaque keys.

Non-ready PDF attachments still appear with a status such as
`missing_archive`, `unstable_archive`, `invalid_archive`, `missing_pdf`,
`ambiguous_attachment`, `unsupported_attachment`, or `mapping_unverified`.
Unavailable `archive_path`, hashes, sizes, `pdf_path`, and `pdf_sha256` are JSON
`null`; consumers must never interpret a stale or invented path.

Explicit Zotero deletions are absent from the current snapshot and remain
auditable as SQLite tombstones and delta `delete` operations.

## Collection snapshot

`collections.json` contains a versioned source/library envelope and the
normalized live collection records in stable path/key order. Each document
embeds the relevant `key`, `name`, and `path` subset for its parent item:

```json
{
  "schema_version": 1,
  "source": "zotero",
  "library_type": "user",
  "library_id": "123456",
  "library_version": 789,
  "collections": [
    {
      "key": "COLL01",
      "name": "Object Detection",
      "path": "Research/Infrared Vision/Object Detection",
      "parent_key": "COLL00"
    }
  ]
}
```

Malformed hierarchy is explicit instead of nondeterministic: cycles use a
`[cycle]/<key>/<name>` fallback and missing parents use
`[missing:<parent-key>]/<name>`. Validation reports both conditions.

## Delta records

Every successful remote sync or local attachment rescan publishes exactly one
`manifests/deltas/<sync_id>.jsonl`. A metadata 304 with no attachment changes
publishes an empty (zero-record) JSONL file and a run summary; it does not
invent an update.

A document appears at most once in a delta. Records are sorted by
`document_id`. Consumers do not need to diff snapshots.

An `upsert` includes the current snapshot record:

```json
{
  "schema_version": 1,
  "sync_id": "20260714T080000Z-01234567",
  "operation": "upsert",
  "document_id": "zotero:user:123456:PARENT01:ABC123:0",
  "previous_fingerprint": "<old-sha256-or-null>",
  "current_fingerprint": "<new-sha256>",
  "metadata_changed": true,
  "content_changed": false,
  "chunk_required": false,
  "reason": "metadata_changed",
  "manifest_record": {"schema_version": 1, "document_id": "..."}
}
```

A `delete` does not carry a manifest record:

```json
{
  "schema_version": 1,
  "sync_id": "20260714T080000Z-01234567",
  "operation": "delete",
  "document_id": "zotero:user:123456:PARENT01:ABC123:0",
  "previous_fingerprint": "<old-sha256>",
  "current_fingerprint": null,
  "metadata_changed": false,
  "content_changed": false,
  "chunk_required": false,
  "reason": "zotero_attachment_deleted"
}
```

`operation` is `upsert` or `delete`. Supported reasons and default chunk policy
are:

| Reason | Meaning | `chunk_required` |
| --- | --- | --- |
| `new_document` | A PDF attachment first entered the snapshot | `status == "ready"` |
| `attachment_became_available` | A non-ready attachment now has a usable PDF | `true` |
| `content_changed` | The selected PDF SHA-256 changed | `status == "ready"` |
| `attachment_replaced` | Archive replacement requires refreshed PDF processing | `status == "ready"` |
| `metadata_changed` | Fingerprinted metadata changed | `false` by default |
| `collection_changed` | Normalized collection membership/path changed | `false` by default |
| `attachment_missing` | A formerly available attachment is missing | `false` |
| `attachment_became_invalid` | A formerly usable attachment is now invalid/non-ready | `false` |
| `zotero_item_deleted` | Zotero explicitly deleted the parent item | `false` |
| `zotero_attachment_deleted` | Zotero explicitly deleted the attachment item | `false` |

Setting `metadata_changes_require_chunking=true` changes the
`metadata_changed` case to require chunking when the resulting document is
ready. `collection_changed` remains false. The option is useful only if
downstream chunk text incorporates ordinary document metadata; it is disabled
by default.

When multiple conditions apply, the single reason is selected in this strict
priority order:

1. explicit Zotero parent/attachment deletion;
2. new document;
3. attachment became available, missing, or invalid;
4. PDF content changed;
5. archive replaced;
6. collection changed;
7. metadata changed.

`metadata_changed` and `content_changed` remain explicit booleans even when a
higher-priority reason is selected.

## Fingerprints

Fingerprints are lowercase SHA-256 values computed from canonical UTF-8 JSON,
except that `content_fingerprint` is directly the selected PDF SHA-256. File
mtime is never a content fingerprint.

### Metadata fingerprint

`metadata_fingerprint` covers exactly the normalized fields that can affect
downstream document metadata:

- title, creators, abstract, publication title, date/year;
- DOI, URL, language, and rights;
- Zotero item type and relations;
- deduplicated/sorted tags;
- normalized collection references sorted by path/key.

Creator order is preserved. DOI whitespace is removed and letters are folded
to lowercase. Null/empty representation, JSON keys, separators, and UTF-8
encoding are fixed. API JSON key order, SQLite row order, duplicate tags,
timestamps, sync IDs, and file mtimes do not participate.

### Content and document fingerprints

`content_fingerprint` is the verified PDF SHA-256 for a ready attachment and
JSON `null` when a PDF is unavailable.

`document_fingerprint` is the SHA-256 of canonical JSON containing:

- manifest schema version;
- `metadata_fingerprint`;
- `content_fingerprint`;
- the normalized status that affects downstream eligibility.

It excludes `updated_at`, run/sync identifiers, and incidental serialization
order. Thus identical semantic inputs reproduce identical fingerprints. An
archive replacement whose selected PDF bytes are identical can still produce
an `attachment_replaced` operational reason without pretending the PDF hash
changed.

## Consumer guidance

For each delta, consumers should:

1. process records in file order and make operations idempotent by
   `document_id` plus `current_fingerprint`;
2. on `upsert`, store the supplied `manifest_record`; only open `pdf_path` when
   `status == "ready"`;
3. run PDF parsing/chunking only when `chunk_required` is true;
4. on `delete`, remove downstream representations for that `document_id` while
   retaining any consumer-specific audit trail;
5. checkpoint the `sync_id` only after the complete delta succeeds.

Attachment-only rescans can produce deltas while `library_version` remains
unchanged, so a consumer must checkpoint sync IDs rather than use Zotero's
library version as the sole delta identity.
