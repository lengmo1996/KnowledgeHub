# Code 同步、增量与故障恢复

状态：**PASS_WITH_LIMITATIONS**

## 无变化同步

对 `transformers@5.13.1` 连续运行两次真实同步：

- 两次均解析到 tag `v5.13.1`、commit `4626421...`；状态 `skipped`。
- 时间 3.02 s、3.41 s；source path、marker hash、8,132 files 和 127,937,482 bytes 不变。
- 同一 logical task 生成 attempt 3/4，均 completed，retry_count=0。
- normalized/index 未增加重复文档、chunk 或向量。

## Candidate 隔离

- 修复前：10-document candidate build 写入独立 109-point collection，但覆盖共享 canonical normalized manifest。
- 修复后：1-document candidate 写入 `/data/KnowledgeHub/code/.staging/normalized/knowledgehub_code_validation_fix_20260717/...`；canonical manifest SHA-256 前后均为 `1ac21fe1...`。

## 中断与恢复

受控 Ctrl-C 暴露生产 build 非原子：部分写入使正式 points 1,118→6,227、state 124→629，任务遗留 running。通过预创建快照、canonical Qdrant IDs、精确 state/artifact reconciliation 和 20-document bounded reindex 恢复到 124/1,118，最终 `validate all` 通过。

已修复：KeyboardInterrupt 现在写入 failed attempt 并释放 locks。未解决：直接对 production collection 构建仍是逐文档提交，不具备 Qdrant+SQLite+artifact 的单事务原子性；操作规避是强制 candidate→validate→promote。
