# Writing-material 到期处置手册

当前入口为`knowledgehub writing-material retention {plan,quarantine,purge}`。它不启动常驻scheduler或任务队列；外部cron/systemd timer可以安全调用只读plan，并对单个`ready` run执行后续命令。所有子命令均要求RBAC `writing_material.retention_dispose`权限。

## 两阶段处置

```bash
knowledgehub writing-material retention plan --run-id RUN_ID
knowledgehub writing-material retention plan-cache-scope --run-id RUN_ID
knowledgehub writing-material retention migrate-cache-scope --run-id RUN_ID --yes
knowledgehub writing-material retention purge-cache-scope --run-id RUN_ID --yes
knowledgehub writing-material retention plan-release-retirement --run-id RUN_ID
knowledgehub writing-material retention decommission-release --run-id RUN_ID --yes
knowledgehub writing-material retention quarantine --run-id RUN_ID --yes
knowledgehub writing-material retention purge --run-id RUN_ID --yes
```

1. `plan`只读解析approval retention、扫描run权限、计算逐文件SHA-256 inventory，并检查candidate/release引用和provider cache。报告为fingerprinted `writing-material-retention-plan-v1`，明确`writes_performed=false`、`index_modified=false`、`llm_called=false`。
2. `quarantine`仅接受已到期、无引用且无未分区provider cache的run。它先写0600 immutable intent，再用同文件系统原子rename把run移到0700 quarantine，最后写0600 receipt。缺少`--yes`拒绝。
3. quarantine默认保留30天。`purge`重新校验receipt fingerprint和完整inventory；宽限期未结束、内容漂移或路径异常均拒绝。成功后使用受root约束的安全删除，并把receipt更新为`purged`。
4. 如果进程在rename后、receipt写入前中断，再次执行同一quarantine命令会用intent和inventory恢复receipt；不会重新移动或覆盖其他run。purge重复执行也是幂等的。

## Provider cache scope

新LLM cache entry在首次atomic write时保存run scope和scope fingerprint；另一run命中相同cache时追加scope，不修改response。历史unscoped cache使用保守迁移：全部绑定到一个获批legacy run，避免无法反推失败/空响应时漏删。

`migrate-cache-scope`先写versioned intent，再逐项幂等更新，最后写receipt；必须与extraction共用derive lock。到期后先执行`purge-cache-scope`：只有当前run独占的entry会删除，多run共享entry只移除当前scope。任一unscoped、invalid或scope fingerprint漂移均fail closed。

## Released run退役

`plan-release-retirement`只在run到期后解析绑定的candidate/release清单并检查Qdrant；未到期返回`not_due`且不访问索引。它要求每份清单fingerprint有效、每个physical collection只属于目标run、fallback健康且live alias/current state一致。

`decommission-release --yes`的固定顺序是：写intent；必要时把stable alias原子rollback到独立fallback；验证alias已离开目标集合；删除run独占physical collections；将本地candidate/release目录atomic rename到`retention/release-reference-quarantine/<run-id>/`；清除retired previous/staged promotion引用；写receipt。若run只是previous rollback target，不执行多余rollback。每次重试均复验collection inspection、目录inventory和owner集合，因此新owner或内容漂移不会被旧intent绕过。

该操作同时要求RBAC `writing_material.retention_dispose`与`writing_material.release`。当前release-reference quarantine还不能由`retention purge`删除；Phase 14C完成独立grace/purge前必须保留。

## 阻断条件

以下情况不会自动删除：

- retention未声明、无法解析或尚未到期；
- run内存在symlink或private permission漂移；
- 任一index-candidate、release-candidate或release manifest仍绑定该run；
- 真实provider使用的共享LLM cache尚无逐run retention scope；
- intent、receipt、inventory或fingerprint漂移。
- live alias/current state漂移、fallback不健康或collection出现另一run owner；

阻断不是错误绕过。先完成所有索引副本的deindex、candidate/release引用解除和cache分区清除，再重新plan。不能先删除run并留下生产points或派生cache。

## 当前pilot证据

2026-07-20对run `20260719T064746Z-f99463512f16`执行真实只读plan：状态`not_due`，到期时间`2031-07-19T06:47:32.819105+00:00`，fingerprint `3506c5f5882cb2c1aa4936c27b9176174191bc9a0d93af8a2ae8c3892e7ada4d`，零写入。

初始cache dry-run为1281 total/1281 unscoped/0 invalid。Phase 14B1已迁移为1281 scoped-to-run/0 unscoped/invalid，scope与response fingerprint各0 mismatch；没有修改response或删除cache。使用到期时刻进行只读未来模拟，当前剩余动作是先purge已知cache scope并处理7个candidate/release引用。collection与alias处置由Phase 14B2完成。

Phase 14B2当前真实plan fingerprint为`b0e937cf0b1ab2579f2623fe93184d60faace418202dd0f8c3621029c64a295d`，状态`not_due`；没有切换alias、删除collection或移动7个真实目录。到期后的released-run退役路径已实现，完整无人值守协调与reference-quarantine purge留给Phase 14C。
