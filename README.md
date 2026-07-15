# KnowledgeHub

KnowledgeHub provides a Zotero source and a unified downstream RAG pipeline.
The source incrementally mirrors metadata, resolves PDF attachments from a
local Nutstore/WebDAV mirror, and publishes deterministic snapshot/delta
manifests. The RAG layer consumes only that contract and implements Docling or
PyMuPDF parsing, canonical Parquet chunks, explicit single/dual GPU scheduling,
TEI embeddings, BM25, Qdrant RRF and optional Qwen3 reranking.

The source consumes two independent inputs:

- Zotero Web API v3 supplies metadata, relationships, collections, versions,
  and explicit deletion events. The client exposes GET operations only.
- Nutstore WebDAV supplies attachment archives. `zotero refresh-cache` follows
  every Nutstore `Link: rel="next"` page into `ZOTERO_WEBDAV_DIR`; the attachment
  resolver then opens `<attachment_key>.zip` and `.prop` files read-only.

SQLite state, extracted PDFs, manifests, run summaries, and logs are written
only beneath `ZOTERO_DATA_DIR`. KnowledgeHub does not read `zotero.sqlite`, use
the Zotero Desktop local API, or download attachment contents from the Web API.

## Install

The workstation environment is the conda environment `rag`:

```bash
conda activate rag
python -m pip install -e '.[rag,dev]'
knowledgehub --config configs/rag/default.yaml rag doctor --dry-run
```

See `docs/guides/BUILD_ZOTERO_RAG_DUAL_3090.zh-CN.md` for the bounded 1/20/100
document workflow, Compose profiles and recovery steps. The pipeline never
automatically starts full-library embedding or OCR.

Python 3.10 through 3.12 is supported.

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

For development checks, install the development extra:

```bash
.venv/bin/pip install -e '.[dev]'
```

Runtime data belongs outside the Git checkout. Create the two roots with
permissions appropriate for the account that will run KnowledgeHub; that
account needs write access to the disposable WebDAV cache and data root.

```text
/data/KnowledgeHub/zotero_cache/ # disposable paginated WebDAV mirror
/data/KnowledgeHub/zotero/   # KnowledgeHub-owned, writable state
```

## Configure

Start from [`configs/sources/zotero.yaml`](configs/sources/zotero.yaml) and
provide the API key and Nutstore application credentials in the process
environment. `.env` files are not loaded automatically;
[`.env.example`](.env.example) is only a list of supported variables.

```bash
export ZOTERO_API_KEY='replace-with-a-read-capable-key'
export ZOTERO_LIBRARY_TYPE=user
export ZOTERO_WEBDAV_USERNAME='your-nutstore-account'
export ZOTERO_WEBDAV_PASSWORD='your-nutstore-application-password'
# ZOTERO_LIBRARY_ID may be omitted for a user library.
```

For a group library, set `ZOTERO_LIBRARY_TYPE=group` and the numeric
`ZOTERO_LIBRARY_ID`. Configuration precedence is:

1. environment variables;
2. the explicitly selected Zotero YAML file;
3. built-in and `configs/default.yaml` defaults.

The `/keys/current` check verifies the key owner and target-library read
permission before synchronization. Secrets are passed only in the
`Zotero-API-Key` header or WebDAV Basic authentication and are redacted from
logs and CLI output.

## Run

The installed `knowledgehub` command and `python -m knowledgehub` expose the
same CLI. All commands print a JSON summary to stdout; diagnostics go to
stderr. Exit codes are `0` for success, `1` for a runtime or validation
failure, `2` for invalid arguments/configuration, and `3` when the sync lock is
already held.

```bash
# Fully enumerate every Nutstore WebDAV page and incrementally refresh the
# local mirror. Pruning affects only local ZIP/PROP files after a full listing.
knowledgehub --config configs/sources/zotero.yaml zotero refresh-cache

# Verify configuration, paths, and API access without syncing.
knowledgehub --config configs/sources/zotero.yaml zotero doctor

# Incremental sync using the last successfully committed library version.
knowledgehub --config configs/sources/zotero.yaml zotero sync --once

# Fetch the current remote object set from version 0 without deleting local
# records merely because they were absent from the response.
knowledgehub --config configs/sources/zotero.yaml zotero sync --full

# Re-resolve local archives without changing the Zotero library version.
knowledgehub --config configs/sources/zotero.yaml zotero resolve-attachments

# Bounded, stable-key-order rescan for smoke tests or rate-limited mounts.
knowledgehub --config configs/sources/zotero.yaml zotero resolve-attachments --limit 20

# Inspect state and validate SQLite, relationships, files, hashes, and manifests.
knowledgehub --config configs/sources/zotero.yaml zotero status
knowledgehub --config configs/sources/zotero.yaml zotero validate

# Poll in the foreground. Production deployments should prefer the timer below.
knowledgehub --config configs/sources/zotero.yaml zotero watch --interval 300

# Preview a local rebuild; --yes is required to perform the replacement.
knowledgehub --config configs/sources/zotero.yaml zotero rebuild
knowledgehub --config configs/sources/zotero.yaml zotero rebuild --yes
```

All synchronization modes call the same `sync_once()` service. A process lock
at `state/zotero.lock` prevents manual, watch, and timer runs from modifying the
same data directory concurrently. `ZOTERO_ENABLE_STREAMING=true` is rejected
with an explicit diagnostic in v1: streaming is not implemented. Use polling
or the systemd timer. Cache refresh has a separate lock in
`ZOTERO_WEBDAV_DIR` so overlapping refresh commands fail safely.

## Runtime layout

On first use, the source initializes the following tree under
`ZOTERO_DATA_DIR`:

```text
/data/KnowledgeHub/zotero/
├── state/
│   └── zotero.sqlite3
├── raw/
├── extracted/
│   └── <attachment_key>/
├── manifests/
│   ├── documents.jsonl
│   ├── collections.json
│   ├── summary.json
│   └── deltas/<sync_id>.jsonl
├── runs/<sync_id>/summary.json
└── logs/
```

The complete normalized Zotero response is stored in SQLite `raw_json`; `raw/`
is reserved for future exports and is not a second source of truth. Deleted
items remain as SQLite tombstones and leave the current snapshot. Existing
extracted files are retained for audit rather than deleted when Zotero reports
a deletion.

Successful publication uses staged, fsynced files, a durable publish intent,
backups, and a final SQLite transaction that commits `library_version` last.
At the next mutating source operation (`sync`, `resolve-attachments`, or a
confirmed rebuild), an interrupted publish is completed or restored according
to its committed sync/version. This is a recovery protocol across SQLite and
the file system, not a claim that those two resources support one native
transaction.

See [Zotero source operations](docs/sources/zotero.md) for synchronization,
attachment safety, recovery, validation, and troubleshooting. See the
[manifest contract](docs/manifests.md) for snapshot/delta schemas,
fingerprints, and `chunk_required` behavior.

## systemd timer example

The repository includes an example oneshot service and 10-minute timer under
`deploy/systemd/`. They are examples only: installation is never performed by
the package or CLI.

The workstation examples use the requested `rag` conda environment. Before
copying the units, adapt the user/group and these absolute paths if your
checkout differs:

- checkout: `/home/lengmo/KnowledgeHub`;
- conda launcher: `/home/lengmo/anaconda3/bin/conda` with environment `rag`;
- configuration: `/etc/knowledgehub/zotero.yaml`;
- environment file: `/etc/knowledgehub/zotero.env`;
- read-only local attachment mirror: `/data/KnowledgeHub/zotero_cache`;
- writable data root: `/data/KnowledgeHub/zotero`.

The environment file should be owned by the service account or root, have mode
`0600`, and contain `ZOTERO_API_KEY`, `ZOTERO_WEBDAV_USERNAME`, and
`ZOTERO_WEBDAV_PASSWORD`; do not put secrets in YAML or either unit.
After reviewing the files, an administrator can install them explicitly:

```bash
sudo install -d -m 0750 /etc/knowledgehub
sudo install -m 0640 configs/sources/zotero.yaml /etc/knowledgehub/zotero.yaml
sudo install -m 0600 .env.example /etc/knowledgehub/zotero.env
sudoedit /etc/knowledgehub/zotero.env
sudo install -m 0644 deploy/systemd/knowledgehub-zotero-cache-refresh.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/knowledgehub-zotero-sync.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/knowledgehub-zotero-sync.timer /etc/systemd/system/
systemd-analyze verify /etc/systemd/system/knowledgehub-zotero-cache-refresh.service \
  /etc/systemd/system/knowledgehub-zotero-sync.service \
  /etc/systemd/system/knowledgehub-zotero-sync.timer
sudo systemctl daemon-reload
sudo systemctl enable --now knowledgehub-zotero-sync.timer
systemctl list-timers knowledgehub-zotero-sync.timer
journalctl -u knowledgehub-zotero-sync.service
```

The refresh service combines `ProtectSystem=strict` with the cache as its only
writable data path. The dependent source service sees that cache read-only and
writes only `ZOTERO_DATA_DIR`. Both commands independently enforce that the two
roots do not overlap.

The sync unit requires
`knowledgehub-zotero-cache-refresh.service`. That prerequisite runs
`zotero refresh-cache`, follows Nutstore's WebDAV `rel="next"` markers until
exhausted, and only then prunes stale local ZIP/PROP files. The first invocation
populates the mirror and checkpoints each completed download so an interrupted
initial refresh can resume safely. Later invocations reuse the authoritative
remote-property index and download only changed objects. Direct source syncs
assume the mirror has already been refreshed.

## Develop and verify

Tests use a mocked Zotero transport, generated ZIP/minimal-PDF fixtures, and
temporary SQLite/data directories; no key, desktop process, live network, or
real WebDAV directory is required.

```bash
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/mypy src
.venv/bin/pytest
```

Run `systemd-analyze verify` after adapting/deploying the units. The source and
RAG examples invoke the same installed CLI through
`conda run -n rag --no-capture-output`; they never activate a shell environment
or embed a secret in the unit.
