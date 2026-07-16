# KnowledgeHub V2 second-round report

## Delivered

- Enabled the five formal Code sources: PyTorch, Transformers, Diffusers,
  Accelerate and Lightning. Corrected each repository layout include set.
- Added permission-gated on-demand version import and a stable-tag Release
  Watch. Watch state is notify-only and never downloads a release.
- Added populated candidate registration, explicit candidate build targeting,
  atomic Qdrant alias promotion, persisted active/previous pointers and
  confirmation-gated alias rollback. The original YAML collection is the
  fallback until a promotion succeeds.
- Expanded MCP from 9 to 13 strict tools with exact symbol inspect/compare,
  allowed-root repository inspection and explicit Writing feedback. Symbol DB
  access is SQLite read-only; feedback is correctly marked write/non-idempotent.
- Replaced regex PyProject scanning with structured PEP 621 parsing, added
  marker evaluation, captured `pip list` matching and static AST extraction of
  setup.py `_deps` lists. Repository code is never imported or executed.

## Real bounded acceptance

Official source synchronization pinned these versions and commits:

| Library | Version/tag | Commit | Bounded build |
| --- | --- | --- | --- |
| PyTorch | 2.11.0 / `v2.11.0` | `70d99e998b4955e0049d13a98d77ae1b14db1f45` | 20 docs / 132 chunks |
| Accelerate | 1.14.0 / `v1.14.0` | `beb0672aa8444ea7647aee056f624effe5996346` | 20 / 167 |
| Diffusers | 0.39.0 / `v0.39.0` | `a3608b512ed7248499a44c61d954965ed9bdae4d` | 20 / 162 |
| Lightning | 2.6.5 / `2.6.5` | `be98784a1a03581b7051a355ae1084fd352d7cea` | 20 / 216 |

Transformers retained its prior real 5.13.0/5.13.1/5.14.0 data. No complete
repository build was started. The Code physical collection now contains 1,106
points. A server snapshot with checksum
`e8d97071ca12f237bbe7b47f1c4be53b58e62f88a1d67b87a734c153e3f847b3`
was created before bootstrapping `knowledgehub_code_current` atomically to the
unchanged `knowledgehub_code_qwen3_4b_1024_v1` physical collection.

The first real Release Watch observed Accelerate 1.14.0, Diffusers 0.39.0,
Lightning 2.6.5, PyTorch 2.13.0 and Transformers 5.14.1. Every result used
`action=notify`, `auto_downloaded=false`; newer tags were not imported.

Two static repository Intake runs were recorded under
`/data/KnowledgeHub/reports`:

- KnowledgeHub: 19 declared dependencies, all declaration-compatible with
  `workstation-3090`;
- Diffusers 0.39.0 at the pinned commit: 54 dependencies, 10 likely-compatible,
  3 conflicts and 41 unknown. The conflicts are protobuf, Ruff and urllib3;
  because Diffusers' shared `_deps` includes dev/optional entries, these are
  review leads rather than runtime-failure claims.

## Verification and boundaries

Offline tests cover distinct candidate/previous collections, atomic alias
operations, confirmation gates, release pre-release filtering, import
permission, strict MCP schemas, exact symbol tools, allowed repository roots and
feedback persistence. Final verification completed with:

- 291 passing tests;
- Ruff passing;
- strict MyPy passing across 105 source files;
- `git diff --check` passing;
- runtime integrity passing for 7 source markers, 120 normalized documents and
  134 Writing entries;
- a real alias-backed Diffusers 0.39.0 hybrid query returning three official,
  commit-pinned hits with no degraded retrieval.

No third-party repository was modified, dependencies were not installed, and
third-party runtime tests were not executed. No full Code source or Literature
corpus was rebuilt. Venue/personal Writing profiles and human-scored gold sets
remain later-round work.
