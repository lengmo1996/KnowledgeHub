# P1-1 非原子索引修复记录

状态：**CLOSED / PASS**

## 安全契约

1. 非 dry-run Code build 必须使用全新 physical candidate collection。
2. Candidate 的 normalized、SQLite、chunks、runs 和 Qdrant 不与 active release 共用写路径。
3. Candidate build 完成后不可变；重复名称和二次写入均拒绝。
4. Candidate 必须通过 document/chunk/point ID、metadata、count、Qdrant status 和 artifact SHA-256 校验。
5. Bounded candidate 只能 smoke，不能 stage；维护发布使用 active-scope clone。`--all` 扩展全部本地源数据时必须显式 `--allow-source-expansion` 才可 promotion。
6. Snapshot recovery 只能恢复到新 candidate；不得覆盖 source collection 或 stable alias。
7. Alias 与本地 release pointer 通过持久 transaction journal 收敛。

## 代码范围

- `governance/releases.py`：CandidateReleaseLayout、release lifecycle、cross-store validator、artifact fingerprint。
- `code_rag/build.py`：服务层 direct-write guard、release immutability、candidate-only indexer。
- `indexing/qdrant.py` / `incremental.py`：首次构建要求 collection 不存在。
- `governance/snapshots.py`：Snapshot 2.1、new-target recovery、release artifact copy/validation、promotion transaction recovery。
- `cli/hub.py` / `cli/v2.py`：`bootstrap-candidate`、`validate-candidate`、source-expansion gate、release-gated stage、safe rollback、`recover-promotion`。
- `code_rag/maintenance.py`：on-demand version import 只同步，不再隐式写正式索引。
- `hub/config.py`：promotion 后查询 collection 与本地 immutable release data_dir 成对解析。

## 自动验证

- 全量 pytest：353 passed，0 failed，0 skipped，13.85 s，max RSS 898,196 KiB。
- Ruff：passed。
- strict MyPy：113 source files passed。
- 关键故障测试：
  - direct non-candidate build 拒绝且不创建 production data dir；
  - 已存在 candidate collection 拒绝；
  - validated release 二次写入拒绝；
  - 本地 artifact/Qdrant point IDs 精确一致；
  - artifact tamper 后 stage 前校验失败；
  - alias switch 前失败恢复为 aborted；
  - alias switch 后、pointer 写入前中断恢复为 committed；
  - snapshot 恢复为新 candidate 并重新通过 cross-store validation。

## 实机 Candidate 验证

执行：

```bash
knowledgehub build code --library transformers --version 5.13.1 --limit 1 \
  --candidate-collection knowledgehub_code_atomic_validation_20260717_01
knowledgehub index validate-candidate code knowledgehub_code_atomic_validation_20260717_01
knowledgehub index alias-status code
knowledgehub validate all
```

结果：

- Candidate：1 normalized/state/artifact document，61 chunks，61 Qdrant points，green。
- Artifact fingerprint：`80cbb79334aab2d910b5ecab56f3a7ac931a11746c495e14ca156e1efff37c7d`。
- `promotion_eligible=false`，不能被 stage。
- Stable alias 未变化，仍解析到 `knowledgehub_code_qwen3_4b_1024_v1`。
- 正式 Code：124 documents、1,118 chunks/points，green。
- 正式 Writing：134 documents/points，green。

## 真实维护窗口关闭演练

第一次 `build code --all` dry-run 显示会把 active 的 124 documents 扩大到 17,437 documents/266,691 chunks。实际构建在约 11,057 candidate points 时安全中断；任务和 release 均标记 failed，正式 alias/124/1,118 完全未变。随后增加显式 source-expansion gate，并使用 active-scope bootstrap 完成等价发布演练。

执行结果：

1. `bootstrap-candidate` 从 `knowledgehub_code_qwen3_4b_1024_v1` clone 出 `knowledgehub_code_release_20260717_maintenance_02`。
2. Candidate：124 documents、1,118 chunks/points、source fingerprint `26f5a811...`、artifact fingerprint `83f4960e...`，cross-store validation green。
3. Stage 后再次校验 count/hash；真实 promote 成功，transaction committed。
4. Promote 后 `validate all` 全绿；`PreTrainedModel.from_pretrained` 精确查询返回 `modeling_utils.py:3874` 和 pinned commit URL。
5. Snapshot 2.1：ID `20260716T192046-knowledgehub_code_release_20260717_maintenance_02-8688692131812382-2026-07-16-19-20-46.snapshot`，1,118 points，`cross_store_complete=true`，checksum `4cd673e4...`。
6. `rollback-alias` 成功恢复原 physical collection；正式三库再次全绿。
7. Snapshot 恢复到新 candidate `knowledgehub_code_recovery_20260717_maintenance_03`；124/1,118、normalized/SQLite/chunks/Qdrant 和 artifact fingerprint 全部一致。
8. Recovery candidate 未 stage；最终 alias 仍指向原 `knowledgehub_code_qwen3_4b_1024_v1`。

KH-V2-003、KH-V2-004 和 P1-1 已关闭。当前剩余 V2 blocker 是 Search API 部署 KH-V2-007，与本索引原子性修复无关。
