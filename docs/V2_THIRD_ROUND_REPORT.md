# KnowledgeHub V2 third-round report

## Scope

This round closes V2.3 Repository Adaptation before starting V2.4. It adds a
reusable evidence → edit → verification → log workflow and validates it on two
real public repositories without installing their dependency sets.

## Implementation

- Repository Intake now parses all bounded requirements files, Conda/Pip
  environment declarations, PEP 621, setup.cfg and static setup.py `_deps`.
- API Inventory resolves import aliases such as `import pytorch_lightning as
  pl`, and records call keywords, inheritance, monkey patches and explicit
  version assumptions. It is capped at 5,000 Python files with an explicit
  truncation field.
- Profiles include CI, Docker, configuration systems, target-hardware hints and
  custom native extensions. Compatibility reports include affected APIs,
  adaptation suggestions, risks, verification steps and unknown items.
- Evidence packages freeze upstream commit, environment, source hashes,
  excerpts, exact symbol source, source URL, strategy, confidence and warnings
  before a change.
- Change records retain before/after hashes and bounded Git patches.
- Verification records retain command, exit code, scope, redacted output and
  output hash. KnowledgeHub records checks but never executes arbitrary target
  commands.
- Debug-log parsing separates project/dependency frames and extracts bounded
  query terms while marking log content untrusted.

## Real repositories

Before adaptation, the real symbol catalog was expanded with PyTorch 2.11.0
(60,520 symbols / 250,298 relations) and Lightning 2.6.5 (6,063 / 16,503).

### cloneofsimo/lora

- Upstream commit: `d84074b3e3496f1cfa8a3f49b8b9972ef463b483`.
- Issue: deprecated `torch.cuda.amp.autocast()` in a Diffusers training project.
- Evidence: PyTorch 2.11.0 exact
  `torch.amp.autocast_mode.autocast` source at commit
  `70d99e998b4955e0049d13a98d77ae1b14db1f45`.
- Change: `torch.autocast(device_type="cuda")`.
- Passed: affected-file `py_compile`; PyTorch 2.11 context-manager API smoke.
- Intake: 10 declared dependencies and 38 external API libraries; one bounded
  declaration was likely-compatible and nine unversioned/missing rows remained
  unknown.
- Boundary: the sandbox exposed no CUDA device, and no model/data/dependency
  installation or training was run.

### state-spaces/s4

- Upstream commit: `e757cef57d89e448c413de7325ed5601aceaac13`.
- Issue: debug config retained Lightning 1-era `gpus`, `weights_summary`,
  `progress_bar_refresh_rate` and `terminate_on_nan` keys.
- Evidence: Lightning 2.6.5 exact `Trainer.__init__` source at commit
  `be98784a1a03581b7051a355ae1084fd352d7cea`.
- Change: use `accelerator`, `devices`, `enable_model_summary`; remove unsupported
  no-op/legacy settings.
- Passed: `train.py` `py_compile`; merged 16-key trainer configuration checked
  against the pinned Lightning AST signature.
- Intake: 29 declared dependencies and 103 external API libraries. All 29 rows
  remain unknown because the repository declarations are unbounded rather than
  being misreported as confirmed compatibility.
- Boundary: Hydra/Lightning are not installed in the workstation environment;
  full compose, data, custom CUDA kernel and training checks were not run.

Persistent isolated repositories live under `/data/KnowledgeHub/adaptations`;
evidence packages, patches and logs live under
`/data/KnowledgeHub/reports/v23/{cloneofsimo--lora,state-spaces--s4}`. These
runtime files are excluded from Git.

## Verification

- 295 KnowledgeHub tests passed.
- Ruff passed.
- Strict MyPy passed across 106 source files.
- `git diff --check` passed.
- Ten evaluation JSONL files containing 13 rows parsed successfully.
- Runtime integrity remained valid for 7 source markers, 120 normalized Code
  documents and 134 Writing entries.
- A real S4 traceback was parsed into one project frame, one dependency frame,
  `TypeError`, keyword `gpus` and bounded query terms, with
  `trusted_as_instruction=false`.
- Both external repository diffs pass `git diff --check`; no external
  dependency set was installed and no external commit/push was created.
