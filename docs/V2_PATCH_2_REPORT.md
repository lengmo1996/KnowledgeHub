# KnowledgeHub V2.0.2 task-governance report

## Scope

V2.0.2 connects the existing unified `TaskStore` to real mutating CLI paths:

- Code source synchronization;
- Release Watch;
- explicitly permitted on-demand version import;
- Code index build;
- Writing derivation and index build.

It does not replace the frozen Literature pipeline state machine, start a
scheduler, grant download permission, rebuild an index or change query
contracts. Permission-denied operations and all dry-runs remain untracked
because no mutation task starts.

## Lifecycle

Each logical task records task type, knowledge base, library/version, input and
output manifests, start/end time, terminal status, error summary, retry count
and canonical result JSON. The append-only `attempts` table preserves repeated
runs without preventing future Release checks or incremental builds.

The lifecycle supports `completed`, `partial`, `failed` and interruption
recovery. Failed or partial logical tasks increment `retry_count` on the next
attempt. An equivalent running idempotency key is rejected. A running task older
than its six-hour TTL is closed with `stale_task_recovered`, its stale locks are
removed and the next attempt is recorded as a retry.

## Locks

- `library:<name>` prevents source sync/build and Release operations for one
  library from racing.
- `index:code:<collection>` serializes Code collection writes.
- `derive:writing` protects the shared derived Writing manifest.
- `index:writing:<collection>` serializes Writing collection writes.

Multiple locks are acquired in sorted order and released in reverse order.
Failed acquisition closes the contender as failed while preserving the owner.
Forced unlock remains an explicit operator command.

## Interfaces

```bash
knowledgehub task list
knowledgehub task inspect <task-id>
knowledgehub task unlock <lock-key> --force
```

`task list` omits stored result JSON to keep output bounded. `task inspect`
returns the logical task, canonical result and ordered attempt history.
`KH_STATE_ROOT` can select a non-default task-state root for tests or an
operator-managed deployment.

## Verification

- 333 tests passed; Ruff, strict MyPy across 110 source files and
  `git diff --check` passed.
- Tests cover repeat attempts, failed retry count, equivalent-task conflict,
  resource-lock conflict, stale-task recovery, old SQLite schema migration and
  all five CLI task routes.
- Real Code sync pinned Transformers 5.13.1 at commit
  `4626421dc6b741a329300682a6408246ee465490`; the existing checkout was
  idempotently skipped, 30 Release records were refreshed, task
  `0995c198-2876-47e7-8660-63c18a4b0536` completed with one attempt and no
  residual lock.
- Real Code sync/build and Writing derive dry-runs created no TaskStore file.
- No Qdrant build, promotion, deletion or Literature mutation was performed.

## Known boundary

The six-hour TTL has no heartbeat in this patch. Current production commands
are bounded below that window; a future long-running full build should add lock
renewal before increasing workload size. Direct Python service calls are not
implicitly wrapped—the durable lifecycle is enforced by the supported CLI
orchestration paths.

The implementation commit is `709ef262b6186ddd8a58e87ab8b2721c5d55fa27`.
The separate release commit contains `state/releases/v2_0_2_manifest.json`, so
the manifest pins implementation without a self-referential Git hash.
