# Skill Integration Validation

- `code-debugging`：加载 failed Experiment、Environment、Config/Error、Failure history 与 Code evidence；high confidence。
- `research-result-analysis`：对齐 config hash、seed、environment、commit、metrics/runtime/parameters，明确单 seed 混杂因素。
- `research-decision-review`：读取 Decision、alternatives、contradicted Claim，要求多 seed 和真实数据重评。
- `writing-academic`：只返回 writing plan/pattern/source；强制 Fixture 标签和禁止复制来源句。
- 调用入口：`knowledgehub project skill ...`、MCP `knowledge_project_query` 和
  `knowledge_project_skill`；四项 CLI 实际调用均 exit 0，MCP 两项调用测试通过。
- MCP：17 个 strict closed-world schema 验证通过；项目工具只读、幂等，状态路径仅由服务端环境
  配置，未知字段和调用方路径注入均被拒绝。
