# KnowledgeHub V2 first-round report

## Completed

- Froze V1 at commit `a15ae0e311e66b7f35cc03c214fa8776344b2d48`
  with 278 passing tests and point-count/config manifests.
- Added strict 2.0 schema envelopes and a side-by-side V1 migration script.
- Added unified six-state tasks, idempotency keys, retry counts, expiring
  library/index locks and force unlock.
- Created a real Qdrant Code snapshot: 429 points with checksum and a local
  V2 snapshot manifest. No rollback was executed.
- Added source/normalized/Writing integrity validation; the real runtime check
  passed 3 source markers, 40 versioned normalized documents and 134 Writing
  entries.
- Added canonical stable/pre-release/local-build/nightly/branch/commit versions
  and adapters for PyTorch, Transformers, Diffusers, Accelerate and Lightning.
- Captured `workstation-3090` as a V2 profile with Python, packages, CUDA 13.0
  and two 24 GB RTX 3090 GPUs.
- Built real Transformers 5.13.0 and 5.13.1 symbol catalogs. The two catalogs
  contain 68,156/68,158 symbols and 389,342/389,353 AST relations.
- Exact comparison of `PreTrainedModel.from_pretrained` classified it as
  unchanged between 5.13.0 and 5.13.1 with source/call/import evidence.
- Added deterministic signature diffs, debuggable query plans, repository
  profiles/API inventories/conservative compatibility reports, paragraph moves,
  separately sourced Writing profiles, internal-source similarity risk,
  feedback adjustments and grouped evaluation fixtures/metrics.
- Expanded top-level CLI with `index`, `task`, `validate`, `symbol`,
  `repository`, and `writing-v2` while retaining every V1 command.

## Verification

- Full regression: 287 passed.
- Ruff: passed.
- Strict MyPy: passed across 104 source files.
- `git diff --check`: passed.
- Zotero remains Web API/WebDAV/Manifest based; Desktop Local API was not used.

## Explicitly deferred from later V2 rounds

- Real synchronized/indexed acceptance for PyTorch, Diffusers, Accelerate and
  Lightning; adapters are implemented and offline-tested, but only Transformers
  has real runtime data.
- On-demand query-triggered version import and Release Watch scheduling.
- Candidate-collection atomic promotion. Server snapshots and confirmed
  recovery are implemented, but V1 per-document builds still write their
  domain collection directly.
- Two external repository code modifications and runtime verification. Intake
  and reports are implemented; no third-party repository was modified or its
  dependencies installed in this round.
- User-selected venue/personal profile extraction over dozens of papers and
  human-scored evaluation gold sets.
- Additional MCP tools for symbol inspection, repository intake and feedback;
  the V1 nine-tool MCP surface remains compatible.
