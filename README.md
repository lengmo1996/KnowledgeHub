# KnowledgeHub

KnowledgeHub provides a Zotero source and a unified downstream RAG pipeline.
The source incrementally mirrors metadata, resolves PDF attachments from a
local Nutstore/WebDAV mirror, and publishes deterministic snapshot/delta
manifests. The RAG layer consumes only that contract and implements Docling or
PyMuPDF parsing, canonical Parquet chunks, explicit single/dual GPU scheduling,
TEI embeddings, BM25, Qdrant RRF and optional Qwen3 reranking.

## Knowledge bases

KnowledgeHub now exposes three logically isolated knowledge bases while sharing
Chunk, embedding, Qdrant and retrieval implementations:

- `literature`: the existing Zotero/PDF pipeline and collection, unchanged;
- `code`: versioned official documentation, examples, source and releases;
- `writing`: provenance-preserving rhetorical patterns derived from Literature.

The default catalog is `configs/knowledgehub.yaml`. Runtime source data,
derived records, state and vectors remain under `/data/KnowledgeHub` and are
ignored by Git. Code and Writing use new collections; the existing
`zotero_papers_qwen3_4b_1024_v2` collection is never migrated or overwritten.

```bash
# Inspect configured libraries and capture the current Python environment.
knowledgehub source list
knowledgehub source inspect transformers
knowledgehub environment capture --name rag

# Preview one installed version, or omit --version to include configured adjacent versions.
knowledgehub sync code --library transformers --version installed --dry-run
knowledgehub sync code --library transformers
knowledgehub build code --library transformers --incremental

# Derive a bounded Writing set from existing parsed papers.
knowledgehub derive writing --limit 5 --dry-run
knowledgehub derive writing --limit 5

# Query independent collections.
knowledgehub query literature "retrieval augmented generation"
knowledgehub query code "Which API changed?" --library transformers --intent compatibility
knowledgehub query writing "introduce a research gap" --section Introduction --writing-function research_gap
```

Code sync uses only configured official repositories and the GitHub Releases
API. `GITHUB_TOKEN` is optional and read only from the environment. It never
appears in YAML, manifests or logs. Writing defaults to deterministic offline
rules; model-backed analyzers can implement the stable `WritingAnalyzer`
protocol without changing stored entries.

See [Code RAG](docs/code_rag.md), [Writing RAG](docs/writing_rag.md),
[data sources](docs/data_sources.md), and [Skill integration](docs/skill_integration.md).

## V2 release

V2 is frozen as package `0.2.0`. Validate its machine-readable release
manifest and committed configuration hashes without contacting runtime
services:

```bash
knowledgehub release validate
```

The final read-only runtime check observed Literature 190,131 points, Code
1,106 points and Writing 134 points, all green. See the
[V2 final report](docs/V2_FINAL_REPORT.md) and
[release operations guide](docs/v2_release.md) for scope, evidence, known
limits and optional operator checks.

## V2 governance and code intelligence

V2 keeps V1 collections and embedding settings while adding explicit schema
migrations, durable task/lock state, Qdrant snapshots, integrity validation,
five library-layout adapters, exact symbol relations, signature diffs,
repository intake, Writing feedback/similarity controls and grouped evaluation
fixtures. V2.4 adds paragraph-level moves, combined Writing filters, strictly
separated Venue/Personal profiles, task plans and feedback-aware ranking.

```bash
knowledgehub validate all
knowledgehub index snapshot code
knowledgehub build code --library diffusers --version 0.39.0 --limit 20 --candidate-collection knowledgehub_code_candidate_20260716
knowledgehub index stage code knowledgehub_code_candidate_20260716
knowledgehub index promote code --yes
knowledgehub index rollback-alias code --yes
knowledgehub symbol build transformers 5.13.1
knowledgehub symbol compare transformers 5.13.0 5.13.1 PreTrainedModel.from_pretrained
knowledgehub query code "why did this API fail?" --symbol SomeClass.method --explain-plan
knowledgehub repository analyze /path/to/repo --environment workstation-3090 --output-root /data/KnowledgeHub/reports
knowledgehub repository evidence /path/to/repo --issue "Trainer rejects gpus" \
  --environment workstation-3090 --file configs/trainer/debug.yaml \
  --library lightning --version 2.6.5 --symbol Trainer.__init__ \
  --strategy "migrate to accelerator/devices" --confidence 0.95
# After Codex edits and runs bounded checks, record rather than re-execute them:
knowledgehub repository record-change /path/to/repo --file configs/trainer/debug.yaml \
  --reason "match Lightning 2 Trainer" --evidence-id <id>
knowledgehub repository record-verification /path/to/repo --name syntax \
  --command "python -m py_compile train.py" --exit-code 0 --output "passed"
knowledgehub repository finalize /path/to/repo --risk "full training not run"
knowledgehub writing-v2 similarity "candidate paragraph"
knowledgehub writing-v2 profile venue NeurIPS-selected --paper-id <paper-id> \
  --section Introduction --section Method --section Experiment
knowledgehub writing-v2 profile personal my-drafts --draft manuscript.md
knowledgehub writing-v2 task strengthen_argument "make evidence explicit" \
  --text "The method is better." --section Experiment
knowledgehub evaluate run --mode offline --profile v2 --output /tmp/eval-v2.json
knowledgehub evaluate run --mode live --profile v2 --output /tmp/eval-live-v2.json
knowledgehub query code "How is a pretrained model loaded?" \
  --library transformers --version 5.13.1 --evidence-envelope --max-tokens 2000
knowledgehub sync plan --trigger periodic --library transformers --interval-hours 24
knowledgehub clean source --library transformers --version 5.13.1
knowledgehub prune unreferenced --knowledge-base all
knowledgehub sync releases --all --dry-run
knowledgehub sync version --library diffusers --version 0.37.0 --dry-run
```

Snapshot and alias rollback are never implicit and require `--yes`. Candidate
promotion switches a stable Qdrant alias atomically and retains the prior
physical collection. Repository analysis
does not execute code or install dependencies. Evidence creation must precede
edits; verification commands are executed by the operator/Codex and only their
results are recorded by KnowledgeHub. See [V2 architecture](docs/v2_architecture.md)
and [V2 migration](docs/v2_migration.md).

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

The repository includes example oneshot services, an hourly source timer,
and a daily incremental RAG timer under `deploy/systemd/`. They are examples
only: installation is never performed by the package or CLI. The complete
step-by-step Chinese deployment procedure is in
[the dual-3090 build guide](docs/guides/BUILD_ZOTERO_RAG_DUAL_3090.zh-CN.md#安装并启用-systemd-定时同步).

The workstation examples use the requested `rag` conda environment. Before
copying the units, adapt the user/group and these absolute paths if your
checkout differs:

- checkout: `/home/lengmo/KnowledgeHub`;
- conda launcher: `/home/lengmo/anaconda3/bin/conda` with environment `rag`;
- configuration: `/etc/knowledgehub/zotero.yaml`;
- environment files: `/etc/knowledgehub/zotero.env` and
  `/etc/knowledgehub/rag.env`;
- read-only local attachment mirror: `/data/KnowledgeHub/zotero_cache`;
- writable data root: `/data/KnowledgeHub/zotero`.

The secret environment files should be owned by root with mode `0600`.
`zotero.env` must contain `ZOTERO_API_KEY`, `ZOTERO_WEBDAV_USERNAME`, and
`ZOTERO_WEBDAV_PASSWORD`. An offline-only `rag.env` may be empty, but the
authenticated reranker and Search API require independent, locally generated
`KH_RERANKER_API_KEY` and `KH_SEARCH_API_KEY` values. Generate each with
`openssl rand -hex 32`; these are not keys obtained from Zotero, Qwen, or a
cloud provider. Do not put secrets in YAML or any unit.
After reviewing the files, an administrator can install them explicitly:

```bash
sudo install -d -o root -g lengmo -m 0750 /etc/knowledgehub
sudo install -o root -g lengmo -m 0640 \
  configs/sources/zotero.yaml /etc/knowledgehub/zotero.yaml
sudo install -o root -g root -m 0600 \
  ~/.config/knowledgehub/zotero.env /etc/knowledgehub/zotero.env
sudo touch /etc/knowledgehub/rag.env
sudo chown root:root /etc/knowledgehub/rag.env
sudo chmod 0600 /etc/knowledgehub/rag.env
sudo install -o root -g root -m 0644 deploy/systemd/knowledgehub-zotero-cache-refresh.service /etc/systemd/system/
sudo install -o root -g root -m 0644 deploy/systemd/knowledgehub-zotero-sync.service /etc/systemd/system/
sudo install -o root -g root -m 0644 deploy/systemd/knowledgehub-zotero-sync.timer /etc/systemd/system/
sudo install -o root -g root -m 0644 deploy/systemd/knowledgehub-zotero-rag-incremental.service /etc/systemd/system/
sudo install -o root -g root -m 0644 deploy/systemd/knowledgehub-zotero-rag-incremental.timer /etc/systemd/system/
sudo install -o root -g root -m 0644 deploy/systemd/knowledgehub-rag-core.service /etc/systemd/system/
sudo install -o root -g root -m 0644 deploy/systemd/knowledgehub-rag-search-api.service /etc/systemd/system/
sudo install -o root -g root -m 0644 deploy/systemd/knowledgehub-rag-online.service /etc/systemd/system/
sudo install -o root -g root -m 0644 deploy/systemd/knowledgehub-rag-embed-dual.service /etc/systemd/system/
sudo install -d -o root -g root -m 0755 /usr/local/libexec
sudo install -o root -g root -m 0755 \
  deploy/systemd/knowledgehub-rag-incremental-run \
  deploy/systemd/knowledgehub-rag-incremental-with-retries \
  /usr/local/libexec/
sudo systemd-analyze verify \
  /etc/systemd/system/knowledgehub-zotero-cache-refresh.service \
  /etc/systemd/system/knowledgehub-zotero-sync.service \
  /etc/systemd/system/knowledgehub-zotero-sync.timer \
  /etc/systemd/system/knowledgehub-zotero-rag-incremental.service \
  /etc/systemd/system/knowledgehub-zotero-rag-incremental.timer \
  /etc/systemd/system/knowledgehub-rag-core.service \
  /etc/systemd/system/knowledgehub-rag-search-api.service \
  /etc/systemd/system/knowledgehub-rag-online.service \
  /etc/systemd/system/knowledgehub-rag-embed-dual.service
sudo systemctl daemon-reload
sudo systemctl enable --now \
  knowledgehub-rag-core.service \
  knowledgehub-rag-search-api.service
sudo systemctl enable --now \
  knowledgehub-zotero-sync.timer \
  knowledgehub-zotero-rag-incremental.timer
systemctl is-enabled knowledgehub-zotero-sync.timer knowledgehub-zotero-rag-incremental.timer
systemctl is-active knowledgehub-zotero-sync.timer knowledgehub-zotero-rag-incremental.timer
systemctl list-timers --all knowledgehub-zotero-sync.timer knowledgehub-zotero-rag-incremental.timer
```

An unrelated diagnostic such as
`/lib/systemd/system/snapd.service: ... Unknown key name 'RestartMode'` comes
from the distribution's snapd unit, not these KnowledgeHub units. Diagnostics
that name a `knowledgehub-*` unit must be fixed before enabling it.

`enable` makes both timers start with `timers.target` at boot; `--now` starts
waiting immediately. The oneshot services are not enabled themselves. Both
timers use `Persistent=true`, so a schedule missed while the host was down is
run once after the next activation.

The boot policy starts the low-VRAM core and CPU-only Search API. Qdrant and
Search API use `restart: unless-stopped`; the core unit waits for Qdrant before
the Search API starts. GPU embeddings and rerankers explicitly use
`restart: "no"`. Their interactive systemd workload units are static (no
`[Install]`) and must be started manually:

```bash
sudo systemctl start knowledgehub-rag-online.service
sudo systemctl stop knowledgehub-rag-online.service
sudo systemctl start knowledgehub-rag-embed-dual.service
sudo systemctl stop knowledgehub-rag-embed-dual.service
```

The two interactive GPU workload units conflict, so switching profiles
releases the first workload before claiming VRAM for the second. The scheduled
incremental RAG service independently inspects `nvidia-smi`, chooses dual,
single GPU 0, or single GPU 1 from the available VRAM, starts temporary
embedding containers, and releases only those containers afterward. A matching
embedding container that is already running is reused and left running, so its
own allocation is not mistaken for an unrelated busy GPU. Reuse requires a
successful health check and completely skips `docker compose up`, preventing
configuration reconciliation from recreating a healthy container. A failed
attempt is retried after four hours, at most twice (three total attempts).

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
