# Writing-material release 合并与发布手册

当前实现入口是 `knowledgehub.writing_rag.release.WritingMaterialReleaseService`，CLI 为 `knowledgehub writing-material release {build,stage,promote,rollback}`。它把 candidate build 与 alias promotion 分成独立操作；任何 build 都不会 stage 或 promotion。

## 前置条件

- extraction run 为 `success`；
- source revalidation 通过；
- accepted-v2 snapshot 为 `complete`，pending 为 0；
- active 和 candidate 都是物理 collection 名，candidate 尚不存在；
- backend 能创建 active snapshot，并恢复到新的 candidate；
- merge 回调只向 candidate 写 accepted strategy/template/phrase，且不 prune clone 中的 active records。

## Build 顺序

1. `dry_run=True`：只读 active schema/point count 和 accepted manifest，计算预期 candidate count；不 snapshot、不 restore、不 merge。
2. 创建 active snapshot。
3. 将 snapshot 恢复到全新 candidate；验证 green、point count 和 dense/sparse schema 与 active 完全一致。
4. 向 candidate 合并 accepted derived assets。
5. 验证 `candidate_points = active_points + accepted_derived_count`，schema 不变、merge 无 failure。
6. 写 `writing-material-release-v1` manifest，状态 `validated`、`promotion_performed=false`。

具体 backend 使用 Qdrant collection snapshot/recover。snapshot 源从 promotion state 的 `current.active_collection` 解析；尚无 promotion state 时使用 Hub 配置中的物理 collection fallback。服务拒绝把稳定 alias 当成 snapshot 源或 candidate。merge 使用 `IncrementalChunkIndexer(require_new_collection=False)`，因为 collection 已由 snapshot restore 创建；它只 upsert accepted derived assets，`prune=false`。

任一步失败都不得 stage。失败 candidate 保留供诊断，不自动删除或覆盖。

## Stage、promotion 与 rollback

- `stage(manifest, confirmed=True)` 再次验证 manifest fingerprint 后才调用仓库现有 promotion backend；
- `promote(fallback, confirmed=True)` 才允许移动 `knowledgehub_writing_current` alias；
- `rollback(confirmed=True)` 使用现有 alias transaction 回到 previous collection；
- `rollback --dry-run`只读验证磁盘promotion state、live alias、active/previous collection、point/schema、release manifest和snapshot availability，绝不调用alias switch或snapshot restore；
- 未显式确认时三个操作都拒绝；
- promotion 前应另行创建 active snapshot，并保留 candidate manifest、accepted manifest hash 和 point-count 报告。

## Rollback readiness

```bash
knowledgehub writing-material release rollback --dry-run
```

`writing-material-rollback-readiness-v1`只有在以下条件全部成立时返回`ready=true`：

- current promotion state为active，active/previous均为安全、不同的物理collection；
- live `knowledgehub_writing_current`实际指向current active；
- 两个collection均green、points有效且dense/sparse schema相同；
- active points与promotion state一致；
- active release manifest位于writing-material release root、fingerprint有效且绑定current active；
- manifest中的snapshot绑定previous collection，并报告Qdrant中是否仍可用。

snapshot缺失只给warning，因为真实rollback首先切回仍存在的previous collection；alias漂移、collection缺失/schema漂移、point drift或manifest错误均返回`blocked`。`--dry-run`与`--yes`组合会被拒绝。

2026-07-19真实只读演练返回`ready=true`、fingerprint `33f4505b05b97d75d113d6d6abf718009f4d9b79b68817c6effbfb215b7adc3f`：active quality-v2为1107 points，previous v1为134 points，二者green且schema一致；release manifest和原snapshot有效。唯一warning是previous collection早于writing-material release manifest。演练后alias/current/transaction均未变化，`rollback_performed=false`、`writes_performed=false`。

自动测试使用fake backend/client验证134+3=137 clone-and-merge、snapshot URI、ready与alias-drift blocked报告、CLI dry-run零写入及confirmation gates。真实演练只读取本机Qdrant和磁盘状态；没有创建snapshot、恢复collection或修改active/candidate/alias。
