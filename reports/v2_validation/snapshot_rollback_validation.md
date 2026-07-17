# Snapshot / Rollback / 完整性

状态：**PASS_WITH_CRITICAL_LIMITATION**

## 成功路径

1. 创建正式 Code snapshot：1,118 points，checksum `c18d6352...`。
2. 构建并验证 109-point candidate。
3. Stage/promote：alias 从正式 collection 切到 candidate；candidate query 成功。
4. Alias rollback：恢复 `knowledgehub_code_qwen3_4b_1024_v1`。
5. Qdrant snapshot recovery：恢复 1,118 points。
6. 最终 `validate all`：Code 124 documents/1,118 chunks/1,118 points；Writing 134/134；0 errors。

## 发现的边界

Snapshot 只覆盖 Qdrant，不覆盖 SQLite state、chunk artifacts、normalized manifests 和 task state。受控中断后，单独恢复 Qdrant 仍产生 216 chunk-ID mismatch；本轮需根据 canonical Qdrant document IDs 清理 505 条部分 state/artifacts，并 bounded reindex 20 documents 才完全一致。

因此“查询 alias/向量恢复”已通过，但“跨 Qdrant+SQLite+artifact 的一键事务回滚”未通过。V3 前应扩展 snapshot manifest，冻结并校验所有本地 artifact hashes，或禁止任何 production direct build。

## P1-1 修复更新（2026-07-17）

实现已改为不可变 candidate release：Snapshot 2.1 绑定 Qdrant snapshot、release manifest 和 artifact fingerprint；恢复目标必须是新 physical collection，并复制、校验 SQLite/chunks/normalized artifacts。Legacy Qdrant-only snapshot 默认 fail closed。Promotion/rollback 增加 prepared → alias_switched → committed 事务日志和 `recover-promotion`。

Mock/fault-injection 已覆盖完整 cross-store recovery、alias 切换前失败 abort、alias 切换后中断恢复。随后在授权维护窗口完成真实 active-scope candidate 的 stage/promote/Snapshot 2.1/alias rollback/snapshot-to-new-candidate recovery：124 documents、1,118 chunks/points 全程一致，artifact fingerprint `83f4960e...`，最终 alias 回到原 physical collection且三库全绿。KH-V2-004 已关闭。
