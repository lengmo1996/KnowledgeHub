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
- 未显式确认时三个操作都拒绝；
- promotion 前应另行创建 active snapshot，并保留 candidate manifest、accepted manifest hash 和 point-count 报告。

自动测试只用 fake backend/client 验证 134 + 3 = 137 的 clone-and-merge、snapshot URI、CLI dry-run 零写入和 confirmation gates。实现过程中没有连接本机 Qdrant、创建真实 snapshot 或修改 active/candidate/alias。
