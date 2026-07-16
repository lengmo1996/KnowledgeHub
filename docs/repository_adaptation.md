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
`pip list` values.

Matrix status is declaration-based: missing ranges are `unknown`, satisfied
ranges are only `likely_compatible`, and mismatches are `conflict`. Before a
Codex edit, retrieve current/target symbol and release evidence, record affected
files and confidence, then run static checks, tests and a bounded execution.
Repository Intake itself never modifies code or installs dependencies.
For synced official sources, report identity includes library, exact version
and commit. A `conflict` from a shared dependency list may describe a dev or
optional extra and must be scoped before changing code.
