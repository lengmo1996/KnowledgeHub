# Writing RAG 验证

状态：**PASS_WITH_LIMITATIONS**

## 修复后只读审计

- 现有正式 manifest：5 papers、134 entries，来源/位置 provenance 缺失 0，重复 writing ID 0。
- section family 修复后，现有 134 条中 128 条可分类；未分类由 71 降至 6，论文标题中的 `Approach` 仍不会误判为 Method。
- 对相同 5 篇论文重新 dry-run：101 entries，严格 Figure/Table captions 0，title/front matter 0。
- 20-paper dry-run：459 entries、19 papers 有有效条目，provenance 缺失 0、重复 writing ID 0。
- 十个目标功能标签全部出现：background、motivation、research_gap、contribution_statement、method_overview、design_rationale、quantitative_comparison、result_interpretation、limitation、future_work。
- 十类冻结 classifier fixture 全部通过。

因此 KH-V2-016、017、018 已按源码、回归和真实只读 dry-run 证据关闭。

## 相似度与查询

- CLI/MCP 使用配置的 Qwen3 embedding；重排改写 cosine=0.9625，返回 high，semantic=`evaluated`。
- pattern-first 查询保留 source paper、section/location、usage notes 和 bounded excerpt。
- 来源相似度明确标注为内部材料风险，不冒充法律意义的查重结论。

## 正式索引边界

本轮没有构建、stage 或 promote Writing candidate。正式 Writing alias/manifest 仍是旧的 134 entries、rules-v1 数据；101-entry 清洗结果和新十类 taxonomy 只有 dry-run 证据。要让线上 Writing 检索实际使用新数据，仍需单独执行 candidate → validate → stage → promote，并在 promote 前对 43 条/十类评估回归。
