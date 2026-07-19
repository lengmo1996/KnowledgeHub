# Writing-material 到期处置手册

当前入口为`knowledgehub writing-material retention {plan,quarantine,purge}`。它不启动常驻scheduler或任务队列；外部cron/systemd timer可以安全调用只读plan，并对单个`ready` run执行后续命令。所有子命令均要求RBAC `writing_material.retention_dispose`权限。

## 两阶段处置

```bash
knowledgehub writing-material retention plan --run-id RUN_ID
knowledgehub writing-material retention quarantine --run-id RUN_ID --yes
knowledgehub writing-material retention purge --run-id RUN_ID --yes
```

1. `plan`只读解析approval retention、扫描run权限、计算逐文件SHA-256 inventory，并检查candidate/release引用和provider cache。报告为fingerprinted `writing-material-retention-plan-v1`，明确`writes_performed=false`、`index_modified=false`、`llm_called=false`。
2. `quarantine`仅接受已到期、无引用且无未分区provider cache的run。它先写0600 immutable intent，再用同文件系统原子rename把run移到0700 quarantine，最后写0600 receipt。缺少`--yes`拒绝。
3. quarantine默认保留30天。`purge`重新校验receipt fingerprint和完整inventory；宽限期未结束、内容漂移或路径异常均拒绝。成功后使用受root约束的安全删除，并把receipt更新为`purged`。
4. 如果进程在rename后、receipt写入前中断，再次执行同一quarantine命令会用intent和inventory恢复receipt；不会重新移动或覆盖其他run。purge重复执行也是幂等的。

## 阻断条件

以下情况不会自动删除：

- retention未声明、无法解析或尚未到期；
- run内存在symlink或private permission漂移；
- 任一index-candidate、release-candidate或release manifest仍绑定该run；
- 真实provider使用的共享LLM cache尚无逐run retention scope；
- intent、receipt、inventory或fingerprint漂移。

阻断不是错误绕过。先完成所有索引副本的deindex、candidate/release引用解除和cache分区清除，再重新plan。不能先删除run并留下生产points或派生cache。

## 当前pilot证据

2026-07-20对run `20260719T064746Z-f99463512f16`执行真实只读plan：状态`not_due`，到期时间`2031-07-19T06:47:32.819105+00:00`，fingerprint `3506c5f5882cb2c1aa4936c27b9176174191bc9a0d93af8a2ae8c3892e7ada4d`，零写入。

使用到期时刻进行只读未来模拟，状态为`blocked`：发现7个candidate/release引用、23个run文件，并检测到provider cache未按run分区。Phase 14A因此没有删除当前run、cache或任何索引；这些依赖由Phase 14B处理。
