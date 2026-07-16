# Repository adaptation workflow

```bash
knowledgehub repository analyze /path/to/repository --environment current
```

Intake reads declarations, entry/training/inference scripts, tests and Python
imports/calls without executing repository code. It writes a repository profile,
API inventory, compatibility matrix and report under `reports/<repository>`.

Matrix status is declaration-based: missing ranges are `unknown`, satisfied
ranges are only `likely_compatible`, and mismatches are `conflict`. Before a
Codex edit, retrieve current/target symbol and release evidence, record affected
files and confidence, then run static checks, tests and a bounded execution.
Repository Intake itself never modifies code or installs dependencies.
