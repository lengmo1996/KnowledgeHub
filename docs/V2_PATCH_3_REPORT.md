# KnowledgeHub V2.0.3 completion report

## Outcome

V2.0.3 closes the remaining safely executable V2 gaps without rebuilding or
mutating Literature. The implementation commit is
`a2dcdbb0993c43ebe48dfe5577d65f45d9d35ca4`; the release commit contains this
report and `state/releases/v2_0_3_manifest.json`.

## Implemented

- TaskExecutor renews every owned lock during execution. Renewal is atomic
  across the lock set, live leases prevent stale recovery, and lease loss fails
  the task closed.
- Static dependency capture reads fixed source markers and records declaration,
  optional/development/build and catalog scope. `setup.py` is parsed as AST and
  never executed. `_deps` is catalog evidence rather than a runtime claim.
- `validate dependencies` checks schema, content hash and marker
  tag/commit/source-path consistency; `validate all` includes this check.
- `build diff` aligns two pinned Symbol Catalog versions, creates bounded
  incremental `version_diff` documents and records annotation/return changes,
  source locations, both commits, compare URL and evidence role.
- Compatibility routing prioritizes version-diff evidence without presenting a
  system-derived diff as official release prose.
- `repository validate` audits evidence snapshots, before/after hashes, saved
  patches, verification status and upstream commit without executing the
  target repository.

## Real bounded evidence

- Transformers 5.13.0 and 5.13.1 symbol catalogs were rebuilt from fixed
  commits. A bounded 4-document/12-Chunk source-diff set was indexed; an
  idempotent update changed one document and kept the point count stable.
- `_LazyAutoMapping.register` correctly reports the `key` annotation widening
  from `type[PreTrainedConfig]` to `type[PreTrainedConfig] | str`, linked to
  commits `6af945f436d85f2b0c5dff9b14feccd27b1d470b` and
  `4626421dc6b741a329300682a6408246ee465490`.
- Five dependency manifests passed validation: PyTorch 2.11.0 (23 records),
  Transformers 5.13.1 (86), Diffusers 0.39.0 (54), Accelerate 1.14.0 (7) and
  Lightning 2.6.5 (3).
- Existing fixed-commit adaptation sessions for cloneofsimo/lora and
  state-spaces/s4 both passed the new audit. Each has one evidence package, one
  scoped change and two passed bounded verifications.
- Writing retains one Venue Profile, `NeurIPS-selected`, with 62 samples. The
  active Writing index was not rebuilt.

## Verification

- `341 passed`; Ruff checks, strict MyPy over 112 source files and
  `git diff --check` passed.
- `knowledgehub validate all` passed 7 source markers, 124 Code normalized
  records, 5 dependency manifests, 134 Writing entries, all local artifacts
  and bidirectional Qdrant membership.
- Final Qdrant state was green: Literature 190,131 points, Code 1,118 points
  (124 documents) and Writing 134 points (134 documents).
- Offline and live V2 evaluation passed all 11 groups and 24 public samples.
  The live candidate passed every configured gate against the previous V2 live
  baseline, including the new pinned version-diff case.
- The TaskStore ended with zero running tasks and zero residual locks.

## External-input boundaries

- A Personal Writing Profile was not fabricated because no user-owned draft
  corpus was supplied. The command is ready for explicit `--draft` inputs.
- User acceptance metrics have no denominator until users submit feedback;
  missing labels remain excluded rather than counted as rejection.
- The source-diff run is deliberately bounded to four changed symbols, not a
  claim of exhaustive Transformers compatibility coverage.
- Dependency manifests are static declaration/catalog evidence; they do not
  resolve an environment or prove that optional/catalog packages are installed.
- The two adaptation checks do not claim full training, datasets, custom CUDA
  kernels or GPU execution. Their recorded unresolved risks remain intact.
- No scheduler, automatic five-library sync, all-paper Writing derivation,
  physical cleanup, index promotion or Git push was performed.
