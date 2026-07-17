# Isolation, Idempotency and Cleanup

- 标识：Workspace/Environment/Experiment 均为 fixture，data_scope=test。
- 列表隔离：普通 Workspace 查询不返回 Fixture；必须显式 include。
- 索引隔离：全部知识证据是本地 JSONL namespace；正式 Literature/Code/Writing 未写入。
- 幂等：第二次运行 Workspace、Environment、5 Experiment、Failure、Decision、3 Claim 全部 unchanged。
- 清理 dry-run：精确列出 Fixture 文件，`repositories_deleted=false`、`shared_knowledge_bases_deleted=false`。
- 真实清理：已删除一次 `state/fixtures/fixture-vision-project`，Manifest 保留；随后完整重建并 `valid=true`。
- 保护：非 Fixture、路径逃逸、Registry 根目录与共享知识库均不可作为清理目标。
- 并发：所有 Workspace/Environment/Record/transition/cleanup 写操作复用 Linux `flock`；竞争
  writer 返回带持锁元数据的 `LockBusyError`，不会交错覆盖文件。
