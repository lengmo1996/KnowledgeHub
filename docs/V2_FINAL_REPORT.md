# KnowledgeHub V2 最终交付报告

## 结论

KnowledgeHub V2 已完成冻结，包版本为 `0.2.0`。V2 在不迁移、不覆盖
Literature collection 的前提下，交付了多知识库治理、Code Intelligence、
Writing V2、分组评估和统一证据接口。冻结清单位于
`state/releases/v2_manifest.json`，可通过离线命令
`knowledgehub release validate` 验证。

最终只读在线检查确认三个发布 collection 均为 `green`：Literature
190,131 points、Code 1,106 points、Writing 134 points；Code 稳定别名
`knowledgehub_code_current` 指向预期物理 collection。

## 架构决定

- V2 是 V1 之上的增量层，不原地改写 V1 JSONL，不自动迁移 Qdrant。
- Literature 保持原 Zotero Manifest、Pipeline SQLite、解析产物、CLI/MCP
  默认值和 `zotero_papers_qwen3_4b_1024_v2` collection。
- Code 与 Writing 使用独立 collection、增量状态和运行目录，同时复用
  Chunk、Embedding、SparseEncoder、Qdrant 和检索实现。
- V2 Schema 使用严格 `2.0` envelope；稳定 Skill 证据接口使用扁平的
  `query_result@2.0` / `knowledge_evidence` 合约。
- 精确符号查询由 SQLite Symbol Catalog 负责，语义证据继续由向量检索
  负责；版本差异将官方证据和系统推断分离。
- Writing 原文不可被分析器覆盖。活动索引仍为 `rules-v1`；`rules-v2`
  只作为候选完成验证，未自动发布。
- Qdrant 候选发布通过稳定 alias 原子切换；快照、回滚、清理和物理删除
  均需要显式确认。

## 交付范围

V2 分阶段完成：

1. V2.1：Schema、迁移、任务/锁、快照、完整性校验与 V1 冻结基线。
2. V2.1.1：候选 collection、稳定 alias、显式 promote/rollback。
3. V2.2：五类库布局适配、版本规范化、符号/关系目录和签名差异。
4. V2.3：Repository Intake、兼容性矩阵、证据先行的适配工作流。
5. V2.4：Writing paragraph moves、Venue/Personal profile 分离、相似度风险、
   feedback 和任务规划。
6. Evaluation：11 个任务组、23 个样本、V1/V2 离线与在线对比门槛。
7. Integration：统一证据 envelope、查询预算、15 个 MCP tools、HTTP/CLI
   增量接口、同步计划、Release Watch 与安全清理。
8. Release freeze：版本 `0.2.0`、发布清单、离线校验器和最终运维交接。

## 冻结数据与来源

源码快照固定为 7 个 library/version/commit 组合：Accelerate 1.14.0、
Diffusers 0.39.0、Lightning 2.6.5、PyTorch 2.11.0，以及 Transformers
5.13.0、5.13.1、5.14.0。完整 commit、符号与关系计数记录在 V2 manifest。

跨域完整性检查通过：7 个 source markers、120 个 normalized Code
documents、134 个 Writing entries，0 errors。Transformers 5.13.1 的旧
checkout 已按用户显式命令安全清理，回收 61,520,885 bytes；当前 checkout、
Literature 和活动索引均未触碰，审计 plan ID 为
`96b3bde9ead488131e2b4c53`。

## 接口

原 `knowledgehub zotero`、`knowledgehub rag`、`knowledgehub mcp` 和旧
`rag_search` 默认行为保持兼容。V2 主要入口包括：

```bash
knowledgehub release validate
knowledgehub validate all
knowledgehub symbol inspect transformers 5.13.1 PreTrainedModel
knowledgehub symbol compare transformers 5.13.0 5.13.1 <symbol>
knowledgehub repository analyze /path/to/repository --environment current
knowledgehub writing-v2 task strengthen_argument "make evidence explicit"
knowledgehub query code "How is a pretrained model loaded?" \
  --library transformers --version 5.13.1 --evidence-envelope
```

MCP 共冻结 15 个 tools，包括 `knowledge_query`、精确符号/版本比较、
Repository 分析、Writing task，以及保持兼容的 `rag_search`、文档/Chunk
读取和 facet 工具。HTTP 新增 `POST /knowledge/query`，旧 `/search` 不变。

## 测试与评估

- 完整测试：321 passed，0 failed。
- Ruff、strict MyPy（110 个源文件）和 `git diff --check` 全部通过。
- 11 个评估 fixture 文件，共 23 个样本；离线和在线 V2 均为 0 failed
  groups，V1→V2 regression gates 全部通过。
- 最终运行时只读验证：三个发布 collection 均为 green，Code alias 正确。
- 发布清单校验不访问网络、模型或 Qdrant，避免把服务可用性混入确定性
  代码/配置完整性检查。

## 已知限制

- Writing 活动索引仍是 134 条 `rules-v1`；没有用户草稿，因此不冻结
  Personal profile。
- 评估集规模较小，尚无完整人工标注；live fixture 中通用 debugging
  recall 为 0.5，repository adaptation recall 为 0.666667。
- Code 检索尚未实现语言偏好排序，英文问题可能命中本地化的官方文档。
- Release Watch 只检查与报告，不自动下载；项目不启动后台 scheduler，
  不自动同步九个库，也不自动处理全部论文。
- 相似度风险是内部来源复用提示，不是法律意义的抄袭判定。

## 后续建议

后续工作应以新候选 collection 开展：扩大人工标注评估集、增加 Code
语言排序与 hard-negative、改善 debugging/adaptation recall，并在人工抽检
通过后单独决定是否发布 `rules-v2` Writing 索引。任何全库同步、全论文
派生、自动调度或物理清理都应继续保持配置驱动和显式授权。

详细架构与操作说明见 `docs/v2_architecture.md`、`docs/v2_migration.md` 和
`docs/v2_release.md`；各阶段证据保留在对应 `V2_*_ROUND_REPORT.md`。
