# Zotero source operations

The Zotero source mirrors metadata and local PDF attachments into a stable
input contract for the rest of KnowledgeHub. It does not parse PDF text or run
chunking, embeddings, indexing, or retrieval.

## Trust and write boundaries

The source deliberately separates remote inputs, a disposable mirror, and the
durable runtime root:

| Channel | Purpose | Access |
| --- | --- | --- |
| Zotero Web API v3 | Metadata, versions, permissions, collections, and deletions | HTTPS GET only |
| Nutstore WebDAV | Paginated remote `<attachment_key>.zip` and `.prop` objects | HTTPS PROPFIND/GET only |
| `ZOTERO_WEBDAV_DIR` | Disposable local attachment mirror | Written by `refresh-cache`; read-only during source sync |
| `ZOTERO_DATA_DIR` | SQLite state, extraction cache, manifests, runs, and logs | Local read/write |

KnowledgeHub never calls the Zotero Local API, reads `zotero.sqlite`, requires
Zotero Desktop to be running, downloads PDFs from Zotero's API, or sends
create/update/delete requests. The cache refresh never sends a remote write
method. Attachment resolution never writes, renames, removes, or extracts in
place inside the local mirror.

An attachment is associated by its Zotero attachment item key. Titles, parent
keys, and PDF filenames are not used to infer archive identity. A single bad or
missing attachment is a committed document status; it does not roll back valid
metadata for the rest of the library.

## Configuration

The CLI merges built-in/`configs/default.yaml` defaults, then the explicitly
selected YAML file, then environment variables. The highest-precedence value
wins. A source YAML may contain a direct mapping (as in
`configs/sources/zotero.yaml`), a top-level `zotero` mapping, or a
`sources.zotero` mapping. KnowledgeHub does not load `.env` automatically.

| YAML field | Environment variable | Default | Notes |
| --- | --- | --- | --- |
| `api_key` | `ZOTERO_API_KEY` | none | Required secret; prefer the environment |
| `library_type` | `ZOTERO_LIBRARY_TYPE` | `user` | `user` or `group` |
| `library_id` | `ZOTERO_LIBRARY_ID` | none | Numeric; required for group, optional for user |
| `api_base_url` | `ZOTERO_API_BASE_URL` | `https://api.zotero.org` | HTTPS URL; alternate hosts are primarily for tests |
| `webdav_url` | `ZOTERO_WEBDAV_URL` | `https://dav.jianguoyun.com/dav/zotero/` | HTTPS collection URL ending in `/`; no embedded credentials/query |
| `webdav_username` | `ZOTERO_WEBDAV_USERNAME` | none | Required by `refresh-cache`; environment only in production |
| `webdav_password` | `ZOTERO_WEBDAV_PASSWORD` | none | Nutstore application password required by `refresh-cache` |
| `webdav_page_limit` | `ZOTERO_WEBDAV_PAGE_LIMIT` | `10000` | Positive cycle/runaway guard for PROPFIND pagination |
| `webdav_request_interval_seconds` | `ZOTERO_WEBDAV_REQUEST_INTERVAL_SECONDS` | `2.0` | Minimum interval between WebDAV request starts; `0` disables pacing |
| `webdav_retry_cooldown_seconds` | `ZOTERO_WEBDAV_RETRY_COOLDOWN_SECONDS` | `900` | Minimum whole-client cooldown after WebDAV HTTP 429/503 |
| `webdav_max_retry_delay_seconds` | `ZOTERO_WEBDAV_MAX_RETRY_DELAY_SECONDS` | `1800` | Maximum server-directed or exponential WebDAV retry delay |
| `webdav_adopt_existing` | `ZOTERO_WEBDAV_ADOPT_EXISTING` | `false` | Trust unindexed local regular files when remote size matches; intended for an out-of-band seed |
| `webdav_prune` | `ZOTERO_WEBDAV_PRUNE` | `true` | Delete supported local objects absent from the completed remote listing |
| `webdav_dir` | `ZOTERO_WEBDAV_DIR` | `/data/KnowledgeHub/zotero_cache` | Existing readable local mirror |
| `data_dir` | `ZOTERO_DATA_DIR` | `/data/KnowledgeHub/zotero` | Writable runtime root, separate from WebDAV |
| `http_timeout_seconds` | `ZOTERO_HTTP_TIMEOUT_SECONDS` | `30` | Positive request timeout |
| `max_retries` | `ZOTERO_MAX_RETRIES` | `5` | Per-request retry limit |
| `sync_max_retries` | `ZOTERO_SYNC_MAX_RETRIES` | `3` | Whole-round retry limit for version drift |
| `api_concurrency` | `ZOTERO_API_CONCURRENCY` | `2` | Validated at 1–4; v1 executes at most 2 concurrent requests |
| `zip_stability_interval_seconds` | `ZOTERO_ZIP_STABILITY_INTERVAL_SECONDS` | `10` | Delay between archive stat observations |
| `zip_stability_check_count` | `ZOTERO_ZIP_STABILITY_CHECK_COUNT` | `2` | Required identical size/mtime observations |
| `mapping_validation_sample_size` | `ZOTERO_MAPPING_VALIDATION_SAMPLE_SIZE` | `20` | Maximum sorted attachment-key samples |
| `attachment_scan_on_304` | `ZOTERO_ATTACHMENT_SCAN_ON_304` | `true` | Retry unresolved/stat-changed local archives on metadata 304 |
| `metadata_changes_require_chunking` | `ZOTERO_METADATA_CHANGES_REQUIRE_CHUNKING` | `false` | Opt-in downstream rechunk for metadata-only changes |
| `enable_streaming` | `ZOTERO_ENABLE_STREAMING` | `false` | Must remain false in v1; streaming is not implemented |
| `poll_interval_seconds` | `ZOTERO_POLL_INTERVAL_SECONDS` | `300` | Default foreground watch interval |
| `log_level` | `ZOTERO_LOG_LEVEL` | `INFO` | Python logging level |

Startup validates the API key, library type/ID, numeric ranges, HTTPS base URL,
directory separation, WebDAV readability, data-directory writability, timeout,
retry counts, stability settings, and concurrency. For a user library,
`/keys/current` supplies the user ID when `library_id` is omitted and must match
an explicitly configured ID. A group library always requires an ID and read
permission for that group.

Permission failures are distinguished as `invalid_api_key`,
`missing_library_permission`, `user_id_mismatch`, or
`unsupported_library_type`; WebDAV authentication failures use
`webdav_auth_error`, and transport failures use `network_error`. Logs never
include API/WebDAV secrets or sensitive headers.

## Remote synchronization

`sync_once(config, mode=...)` is the only remote synchronization service.
Manual sync, foreground watch, and the systemd timer call it rather than
maintaining separate state machines.

A normal incremental round performs these steps:

1. Acquire `ZOTERO_DATA_DIR/state/zotero.lock` with `flock` and record the PID,
   sync ID, and start time in the locked file.
2. Create a `sync_runs` audit record and load the last committed
   `library_version`.
3. Verify the key and target library with `/keys/current`.
4. Request changed item/collection versions with
   `since=<library_version>&format=versions`, then fetch full objects in batches
   of at most 50 keys.
5. Request `/deleted?since=<library_version>`; absence from a result is never
   treated as deletion.
6. Rebuild affected parent/attachment and collection projections, resolve
   affected archives, and build a candidate snapshot and delta.
7. Atomically publish candidate files and commit the new version last.

The first successful sync starts from version 0. `sync --full` also requests
the current object set from version 0 but does not clear state or infer deletes
from absence; only the deleted endpoint creates tombstones.

The first valid versions response fixes the round's
`target_library_version`. Every subsequent response and a final conditional
probe must agree via `Last-Modified-Version`. If the remote library changes
during the round, all candidates are discarded and the entire round is retried
up to `sync_max_retries`; an observed newer version is never simply written
into local state.

The client sends Zotero API v3 headers, keeps credentials in the
`Zotero-API-Key` header, and rejects pagination links that change origin.
`Backoff` is honored before `Retry-After`; otherwise retries use bounded,
jittered exponential delay. HTTP 429, 500, 502, 503, 504 and temporary
transport failures are retryable, while other 4xx responses are not repeatedly
retried.

### A 304 response

When Zotero returns `304 Not Modified`, metadata JSON and an unchanged snapshot
are not rewritten and no metadata delta is invented. The run is still recorded
as successful and receives a valid, empty delta plus a run summary.

With `attachment_scan_on_304=true`, the resolver also retries attachments that
are not ready and archives whose size/mtime changed. It does not rehash or
extract every unchanged archive.

### Local attachment rescan

`resolve-attachments` reads current attachment items from SQLite and performs
an explicit local rescan without fetching all metadata. It recalculates archive
hashes, reuses the same resolver/document/manifest pipeline, emits a delta for
actual changes, and does not modify `library_version`. Use it when Nutstore
finishes syncing after metadata arrived, a missing/unstable archive becomes
available, or an archive is replaced under the same key.

Use `--limit N` for a stable attachment-key-ordered bounded run, or repeat
`--attachment-key KEY` to select specific eligible PDF attachments. Documents
outside a bounded selection retain their previous resolution state. This is
useful for smoke tests and large local mirrors; repeating a bounded run is
idempotent.

The production input is a real local mirror, not the `/data/Nutstore` FUSE
mount and not rclone's incomplete first page. Refresh it with:

```bash
knowledgehub --config configs/sources/zotero.yaml zotero refresh-cache
```

The command sends `PROPFIND` with `Depth: 1`, parses each DAV multistatus body,
and follows same-origin, same-collection `Link: ...; rel="next"` URLs containing
the Nutstore `mk` marker until no next link remains. Cycles, conflicting links,
cross-origin URLs, malformed XML, unsafe filenames, and page-limit overflow
fail the run. Only direct `<attachment_key>.zip` and `.prop` children enter the
mirror.

All PROPFIND, GET, and retry request starts are paced by
`webdav_request_interval_seconds` (2 seconds by default). HTTP 429 and 503
responses impose at least `webdav_retry_cooldown_seconds` of whole-client
cooldown before retrying. `Backoff` and `Retry-After` are still honored up to
`webdav_max_retry_delay_seconds`. Request duration and retry backoff count
toward the start interval, so a longer response or backoff does not add a
redundant delay.

An out-of-band cache seed copied from another machine can be adopted with
`--adopt-existing` or `webdav_adopt_existing: true`. Adoption is limited to
previously unindexed, non-symlink regular files whose local size exactly matches
the WebDAV listing. Files with known but changed remote metadata are always
downloaded. Because WebDAV exposes no portable content checksum, adoption is an
explicit trust decision; use it only when the seed came from the same object
set. The final summary reports adopted objects separately from downloaded,
resumed, and unchanged objects.

Set `webdav_prune: false` (or pass `--no-prune`) when the local cache contains
objects copied from another machine that are intentionally absent from the
current WebDAV listing. The normal mirror default remains pruning enabled.

After listing, `refresh-cache` logs the total remote-object and page counts.
Each completed object then emits `current/total` progress plus cumulative
`downloaded`, `resumed`, and `unchanged` counts. These progress records use
stderr logging; the final machine-readable summary remains a single JSON value
on stdout.

Downloads stream to mode-0600 sibling temporary files, validate the advertised
size, fsync, and atomically replace their destination. A local index records
remote size, ETag, and modification time so later runs skip unchanged files.
After every successful download, a separate mode-0600 progress index is
atomically checkpointed. An interrupted run therefore resumes files whose
checkpointed remote metadata still matches the next complete listing and whose
local file is still a regular file of the advertised size. The success output
reports these as `resumed`; changed, missing, or invalid files are downloaded
again. A later listing or download error reports `checkpointed_objects` and the
progress-index path when resumable work exists. The authoritative index is
committed—and stale local ZIP/PROP files are pruned—only after a complete
listing and all required downloads succeed, then the progress index is removed.
`--no-prune` keeps stale local files for diagnostics. Neither mode can delete a
remote object.

`deploy/systemd/knowledgehub-zotero-cache-refresh.service` runs this command.
`knowledgehub-zotero-sync.service` requires it, so the initial service start
fills the complete mirror and later starts download only changed objects. A
separate cache lock rejects overlapping refresh processes.

The source manifest must not be the sole input to this refresh. It is produced
after attachment resolution, and Zotero API metadata cannot reveal a ZIP that
was replaced in WebDAV without a metadata change. The complete paginated remote
listing is the authoritative transfer detector; the delta catalog remains the
downstream exactly-once control plane. After the mirror refresh, a normal
incremental source run detects changed archive stat/hash values, publishes the
resulting document delta, and leaves `library_version` governed only by the
Zotero API.

If Nutstore returns `BlockedTemporarily`, the refresh service fails and the
dependent source sync does not start. Allow a cooldown and restart the service;
already downloaded local files are retained.

## Archive mapping and safe extraction

The primary archive paths are:

```text
<webdav_dir>/<attachment_key>.zip
<webdav_dir>/<attachment_key>.prop
```

Only when the flat ZIP is absent does the resolver consider the historical
`<webdav_dir>/<attachment_key>/*.zip` form; more than one nested candidate is
ambiguous. Source files and candidates must not be symbolic links.

Before first formal attachment ingestion, mapping validation examines up to
`mapping_validation_sample_size` available PDF attachments in sorted key order.
Every sample must either have exactly one ZIP entry matching the API filename
or have exactly one PDF entry. At least one sample and a 100% pass rate are
required. A library change or WebDAV realpath change invalidates the saved
validation. If validation fails, metadata still commits but all PDF documents
remain `mapping_unverified`.

New or stat-changed ZIPs are observed as a batch until size and `mtime_ns` are
stable for the configured count. A missing `.prop` is treated as
`unstable_archive`, since the mirror may still be writing. The resolver then:

1. checks `zipfile.is_zipfile()` and CRCs with `testzip()`;
2. preflights every member and rejects `..`, absolute paths, Windows drives,
   backslash escapes, symlinks, devices, FIFOs, and other special files;
3. lists ordinary PDF candidates without calling uncontrolled `extractall()`;
4. selects a unique exact API-filename match, or the ZIP's only PDF;
5. marks multiple non-unique candidates `ambiguous_attachment` rather than
   choosing the first sorted file;
6. extracts the selected PDF beneath the sync's private staging directory,
   hashes and fsyncs it, then publishes `extracted/<attachment_key>/` through
   the same durable intent and backup/restore protocol as the manifests.

An unchanged archive hash is not re-extracted when the published PDF exists
and its recomputed hash matches SQLite. If a replacement archive is corrupt,
the old cache can remain for recovery, but the current document no longer
references that stale PDF.

Document statuses are:

| Status | Meaning |
| --- | --- |
| `ready` | Metadata and a verified local PDF are available |
| `metadata_only` | Metadata is usable but no consumable PDF is currently selected |
| `missing_archive` | No archive for the attachment key exists |
| `unstable_archive` | ZIP/`.prop` may still be synchronizing |
| `invalid_archive` | ZIP structure, CRC, path, or member type is unsafe/invalid |
| `missing_pdf` | Valid archive contains no PDF |
| `ambiguous_attachment` | Multiple PDFs cannot be uniquely matched |
| `unsupported_attachment` | MIME type or link mode is unsupported |
| `mapping_unverified` | Attachment-key-to-archive mapping has not passed sampling |
| `error` | Another classified resolver failure occurred |

Non-ready documents carry null `pdf_path`, `pdf_sha256`, and other unavailable
file attributes; a stale path is never fabricated.

## Collections, deletion, and state

Collection paths are deterministic root-to-leaf strings. A three-color graph
walk prevents malformed parents from looping forever. A cycle degrades to a
path rooted at `[cycle]/<key>/<name>` and a missing parent to
`[missing:<key>]/<name>`; both are reported by validation.

Deleted parent items remove every associated document from the snapshot and
emit `zotero_item_deleted`; deleted attachments affect only their own document
and emit `zotero_attachment_deleted`. SQLite keeps the tombstone. WebDAV ZIPs,
`.prop` files, and extracted audit caches are not physically removed.

The state database enables foreign keys, WAL, a busy timeout, full synchronous
writes, and `PRAGMA user_version` migrations. A data directory is bound to one
library type/ID and refuses reuse for another library. Raw API objects are
stored as canonical JSON in SQLite.

## Publication and crash recovery

A file system and SQLite cannot share a native atomic transaction. The source
therefore uses an explicit recovery protocol:

- manifests and extraction changes are written and fsynced in staging;
- a durable publish intent records the sync/version and backup paths;
- candidates replace targets inside the final database transaction;
- object, deletion, relation, attachment, and document changes commit together,
  with the successful `library_version` updated last;
- at the next `sync`, `resolve-attachments`, or confirmed rebuild, an intent
  whose sync/version did not commit restores backups; an intent that did
  commit finishes cleanup.

Metadata/manifest/version failures mark the run failed, keep the prior snapshot
available, and leave the previous successful library version unchanged. The
short started/failed audit record is intentionally committed independently.

Every destructive local operation first resolves its target and proves it is
beneath `ZOTERO_DATA_DIR`. The WebDAV root is not an eligible cleanup target.

## CLI and validation

Place global `--config` before the `zotero` command. If it is omitted, the CLI
uses the repository source configuration when available and then applies
environment overrides.

```bash
knowledgehub --config /etc/knowledgehub/zotero.yaml zotero doctor
knowledgehub --config /etc/knowledgehub/zotero.yaml zotero sync --once
knowledgehub --config /etc/knowledgehub/zotero.yaml zotero sync --full
knowledgehub --config /etc/knowledgehub/zotero.yaml zotero resolve-attachments
knowledgehub --config /etc/knowledgehub/zotero.yaml zotero resolve-attachments --limit 20
knowledgehub --config /etc/knowledgehub/zotero.yaml zotero resolve-attachments --attachment-key ABCD1234
knowledgehub --config /etc/knowledgehub/zotero.yaml zotero validate
knowledgehub --config /etc/knowledgehub/zotero.yaml zotero status
knowledgehub --config /etc/knowledgehub/zotero.yaml zotero watch --interval 300
knowledgehub --config /etc/knowledgehub/zotero.yaml zotero rebuild
knowledgehub --config /etc/knowledgehub/zotero.yaml zotero rebuild --yes
```

`doctor` checks configuration, permissions, and remote access. `status` reports
the latest state/run without syncing. `watch` uses a monotonic interval, invokes
`sync_once()` each time, and exits cleanly on SIGINT/SIGTERM at a safe boundary.
Production deployments should normally use the provided systemd timer. See the
[step-by-step systemd deployment guide](../guides/BUILD_ZOTERO_RAG_DUAL_3090.zh-CN.md#安装并启用-systemd-定时同步)
for installing `/etc/knowledgehub/zotero.yaml`, the two environment files, all
five units, and both timers. Enabling the timers attaches them to
`timers.target`, so they start at boot; the oneshot services themselves do not
need to be enabled.

`rebuild` is dry-run by default. `--yes` constructs and validates replacement
state beneath the data root before using the same recoverable publication
protocol; it never modifies Zotero or the WebDAV source.

`validate` returns nonzero and actionable diagnostics when it finds problems.
It checks:

- SQLite schema/library state and database-to-snapshot document membership;
- resolvable object parents and collection cycles/missing parents;
- attachment-key mapping and permissions/path boundaries;
- ready PDF existence and recomputed SHA-256;
- snapshot schema v1, unique/sorted document IDs, deterministic fingerprints;
- delta schema v1, document references, operation shape, and stable ordering;
- WebDAV read-only expectations and data-root writability;
- an interrupted publish intent that requires recovery.

Because `refresh-cache` and source sync are separate locked processes,
KnowledgeHub can prove that the resolver opened source files read-only and
detect stat changes while processing. The systemd dependency keeps the mirror
writer from overlapping the normal source resolver.

## Streaming status

Zotero Streaming API support is intentionally absent in v1. Enabling it yields
an explicit unsupported-feature diagnostic; no placeholder WebSocket state
machine bypasses deleted-object handling, locking, version checks, or manifest
publication. Use `watch` or the hourly systemd timer.
