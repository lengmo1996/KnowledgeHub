# V3 Fixture Architecture Review

- 阶段目标：在不改动正式数据的前提下选择最小 V3 架构。
- 已检查：V2 final/failures/fixes/remediation、CLI、Environment、Hub Query、Schema、Manifest、Migration、MCP/Skill 文档与测试布局。
- V2 当前状态：**PASS**。历史 `final_report.md` 的初轮 `FAIL` 已由同文件顶部说明和 `remaining_risk_remediation.md` 后续证据取代；KH-V2-001～018 全部关闭。
- 实现：`knowledgehub.project` 包、现有 CLI 下的 `workspace/project/fixture` 子命令、文件 Registry、任务型 Context Builder。
- 隔离：源码、状态、报告分别位于 `fixtures/v3`、`state/fixtures`、`reports/v3_fixture`；不写任何正式 collection/alias。
- 风险：文件 Registry 只适合单机小规模；本地 Fixture RAG 证明路由与追踪，不证明正式向量检索质量。
- 未实施：Dashboard、Dataset RAG、调度、多 Agent、队列、自动论文及多用户权限。

完整审查见 `docs/v3_fixture_architecture_review.md`。
