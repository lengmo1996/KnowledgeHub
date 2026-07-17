# Cross-RAG Validation

- Literature：3 条 CC0 人工 Fixture note，覆盖 fusion comparison、ablation scope、capacity。
- Code：3 条带 source/version/symbol location 的 Fixture 源码证据，覆盖 projection、NaN、seed/split。
- Writing：3 条 pattern-first 证据，覆盖 comparison、limitation、hypothesis。
- 项目查询：experiment_analysis 同时路由 Code 与 Writing，并附 Workspace Context、Experiment、来源、版本和 warnings。
- 安全：Router 拒绝任何非 `fixture-` namespace；没有写 Qdrant、正式 Collection 或 alias。
- 限制：本轮只验证接口/路由/过滤/来源；没有对正式 V2 在线 RAG 进行写入或质量验收。
