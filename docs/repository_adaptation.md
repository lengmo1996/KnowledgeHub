# Repository adaptation workflow

```bash
knowledgehub repository analyze /path/to/repository --environment current \
  --output-root /data/KnowledgeHub/reports
```

Intake reads declarations, entry/training/inference scripts, tests and Python
imports/calls without executing repository code. It writes a repository profile,
API inventory, compatibility matrix and report under the selected output root.
PEP 621 dependencies are read with `tomllib`; static `_deps` lists in
`setup.py` are parsed with AST without importing or executing setup code.
Environment matching uses the captured package subset plus sanitized
`pip list` values. AST inventory is deterministically capped at 5,000 Python
files and reports whether the cap truncated the repository.

Matrix status is declaration-based: missing ranges are `unknown`, satisfied
ranges are only `likely_compatible`, and mismatches are `conflict`. Before a
Codex edit, retrieve current/target symbol and release evidence, record affected
files and confidence, then run static checks, tests and a bounded execution.
Repository Intake itself never modifies code or installs dependencies.
For synced official sources, report identity includes library, exact version
and commit. A `conflict` from a shared dependency list may describe a dev or
optional extra and must be scoped before changing code.

## Evidence-first adaptation

Before editing, freeze the issue, target environment, affected source hashes
and exact versioned Code evidence:

```bash
knowledgehub repository evidence /path/to/repository \
  --issue "Trainer rejects legacy gpus" \
  --environment workstation-3090 \
  --file configs/trainer/debug.yaml \
  --library lightning --version 2.6.5 --symbol Trainer.__init__ \
  --strategy "use accelerator/devices" --confidence 0.98
```

When library/version/symbol are provided, the command reads the existing Symbol
SQLite in read-only mode, extracts the exact line-bounded source from the pinned
commit and records its GitHub URL. An optional `--query` adds bounded vector
evidence; an empty vector result never replaces exact evidence with unrelated
hits.

After Codex makes the scoped edit, record the Git diff and before/after hashes:

```bash
knowledgehub repository record-change /path/to/repository \
  --file configs/trainer/debug.yaml --reason "match Lightning 2 Trainer" \
  --old-api gpus --new-api accelerator/devices --evidence-id <id>
```

KnowledgeHub deliberately does not execute arbitrary target-repository
commands. Run trusted checks explicitly, then record command, exit status,
bounded/redacted output and output hash:

```bash
knowledgehub repository record-verification /path/to/repository \
  --name "Trainer config contract" --command "..." --exit-code 0 \
  --output "16 keys accepted" --scope static-integration
knowledgehub repository finalize /path/to/repository \
  --risk "full GPU training was not run"
```

`adaptation.json`, `evidence/*.json`, `patches/*.diff` and
`adaptation_log.md` are idempotent runtime artifacts. Evidence cannot be
silently replaced after a change is recorded.

Audit a completed session without modifying it:

```bash
knowledgehub repository validate /path/to/repository \
  --output-root /data/KnowledgeHub/reports
```

The audit compares the recorded upstream commit, evidence snapshots,
before/after hashes and stored patch to the pinned Git worktree, and checks
verification status consistency. It does not rerun target-repository commands.

## Debug logs

```bash
knowledgehub repository debug-log /path/to/repository --log-file traceback.txt
```

The parser separates project, dependency and external frames; extracts the
exception and unexpected keywords; and emits Code-query terms. User log text is
always marked `trusted_as_instruction=false`.
