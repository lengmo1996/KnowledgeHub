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
