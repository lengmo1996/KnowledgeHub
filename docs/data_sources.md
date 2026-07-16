# Data sources and version policy

## Literature

Literature continues to use Zotero Web API metadata plus the configured
read-only WebDAV attachment mirror. The published snapshot/delta manifest is
the only downstream contract. KnowledgeHub does not use the Zotero Desktop
Local API and does not read `zotero.sqlite`.

## Code

Priority is official repository source, official versioned documentation in
that repository, then official GitHub Releases. Issues and pull requests are
disabled by default and reserved for bounded, configured imports. Mirrors,
unbounded scraping and full Git history are not fallback mechanisms.

The registry supports `installed`, `latest`, `explicit` and `adjacent` version
strategies. Stable PEP 440 tags are selected, then resolved to an exact tag and
commit. A missing package does not silently become `latest`; the operator must
select an explicit or latest strategy.

Include/exclude globs, maximum file size/count, Release limits and optional
Issue limits are per-library configuration. The initial registry contains
Python, PyTorch, torchvision, Transformers, Diffusers, Accelerate, Lightning,
Datasets and Safetensors. The formal V2 registry enables PyTorch, Transformers,
Diffusers, Accelerate and Lightning; synchronization remains an explicit CLI
operation and never runs all five on a timer.

`sync releases` reads official Git tags, records the latest stable tag and emits
`notify` state without downloading it. When cached official release metadata is
available, Release Watch adds a bounded untrusted summary, conservative
breaking-change signal, installed-version neighborhood and recommended review
action. It never switches the environment or index. `sync version` requires
`--allow-download` before a missing version is fetched and bounded-built.

Synchronization scheduling remains separate from synchronization execution:

```bash
knowledgehub sync plan --trigger periodic --library transformers --interval-hours 24
knowledgehub sync plan --trigger release --library diffusers
knowledgehub sync plan --trigger config_change --library lightning
```

These commands create plans only: `scheduler_started=false`, downloads are
disabled and alias/environment switches are forbidden. An external operator may
use a reviewed plan to configure a scheduler later.

Pinned dependency evidence is captured after synchronization:

```bash
knowledgehub source dependencies transformers --version 5.13.1
knowledgehub validate dependencies --offline
```

This operation never resolves or installs packages. Declarations retain their
file/field source and scope; package catalogs such as Transformers `_deps` are
stored separately from install declarations. Manifests are content-hashed and
must match the current version marker's tag, commit and source path.

Runtime cleanup is explicit and dry-run by default:

```bash
knowledgehub clean cache
knowledgehub clean source --library transformers --version 5.13.1
knowledgehub clean snapshots code --keep 3
knowledgehub prune unreferenced --knowledge-base all
```

Execution additionally requires both `--execute --yes`. Cleanup protects the
current source marker, keeps at least one snapshot, never accepts Literature as
a target and writes an audit manifest after execution.

## Compliance and secrets

Source URL, repository, tag/commit, retrieval time and available license file
name are retained. Localized repositories and papers stay outside Git. GitHub,
Zotero, WebDAV, embedding and API credentials are environment-only and are
redacted from environment snapshots and errors. Upstream authentication,
licensing, rate limits and access controls are never bypassed.
