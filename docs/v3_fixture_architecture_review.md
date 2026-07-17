# KnowledgeHub V3 Fixture 架构审查

日期：2026-07-17

## 1. V2 验收状态

`reports/v2_validation/final_report.md` 保留的是初轮历史 `FAIL`，不能单独作为当前门禁结论。该文件顶部的后续说明与 `remaining_risk_remediation.md` 均确认：KH-V2-001～018 已全部关闭，362 项测试、Ruff、strict MyPy 通过，Search API 0.2.5 和不可变 candidate 发布/恢复已完成运行验证。因此本轮采用的当前状态为 **PASS（存在已记录的正式发布边界）**，允许进入 V3 Fixture。

当前无未解决 P0/P1。Code/Writing 的新质量改进尚未切换正式 alias；这属于发布边界，不是数据一致性故障。本轮不得依赖尚未发布的新索引内容，也不得切换正式 alias。

## 2. 可复用能力

- 顶层 `knowledgehub` CLI 及其子命令分发模式；
- `core.atomic` 的原子 JSON 写入和 `core.hashing` 的内容哈希；
- V2 Environment Profile 的脱敏、包版本和项目文件指纹思路；
- `HubQueryRequest` / evidence envelope 的三知识库统一查询契约；
- candidate/alias 的隔离原则、只读 Symbol Index 和来源追踪字段；
- 现有 pytest、Ruff、strict MyPy 质量门；
- Writing 的 pattern-first 输出约束和 Skill/MCP 只读入口约定。

## 3. V3 缺失能力

仓库目前没有项目级 Workspace Registry、Experiment/Failure/Decision/Claim 记录、按任务裁剪的 Project Context Builder、项目级查询入口或 Fixture 专属清理协议。现有 Environment Profile 面向正式 Code 数据根，不能直接用作 Fixture Registry。

## 4. 推荐最小实现

新增 `knowledgehub.project` 包，提供版本化 dataclass Schema、独立文件 Registry、Context Builder、Fixture Knowledge Router、项目级 Skill 服务和受限清理。新增 CLI 子命令 `workspace`、`project`、`fixture`，继续使用现有顶层入口，不建立平行可执行文件。

模拟项目使用 NumPy、小型确定性合成二分类数据和两种 fusion；它只验证闭环，不实现训练调度、模型服务或实验向量化。

## 5. 数据隔离方案

- 源码 Fixture：`fixtures/v3/fixture_vision_project/`；
- 运行状态：`state/fixtures/<workspace_id>/`；
- 报告：`reports/v3_fixture/`；
- 所有记录固定 `workspace_type/environment_type/experiment_type=fixture`、`data_scope=test`；
- Literature/Code/Writing 通过 `fixture-*` namespace 的本地只读小型证据路由验证接口，不写 Qdrant，不读取或复制用户正式文献；
- Workspace 仅保存稳定 ID、相对路径、namespace、collection/filter 引用；
- 清理只允许已验证的 Fixture 根目录，默认 dry-run，拒绝符号链接、路径穿越和非 Fixture Workspace。

## 6. Workspace Schema

`Workspace` 包含身份、类型、状态、研究问题/假设、Repository 引用、Environment 引用、Knowledge Scope、时间戳和 Schema Version。Registry 的唯一键为 `workspace_id`；列表默认排除 Fixture，必须显式 `include_fixtures`。

校验检查 Schema、ID、相对 Repository 路径、引用 Profile、fixture namespace、缺失资源和重复引用。第一轮不引入 Workspace 间引用，因此循环引用在结构上被禁止。

## 7. Experiment、Decision、Failure 与 Claim Schema

- Experiment：不可变 ID/run ID、状态、命令、config hash、seed、git/environment、metrics、artifact 引用、failure/retry 关系；
- Failure：根因置信状态、症状、证据、修复实验和状态；
- Decision：alternatives、基于实验指标的 evidence、rationale、consequence 和 revisit condition；
- Claim：状态、scope、limitations 及指向 Experiment JSON Pointer 的证据。带数值的 Claim 必须由记录生成，不能接受游离数值。

记录文件采用一记录一 JSON；相同 ID+规范化内容重复写入返回 unchanged，不同内容拒绝覆盖。失败重试必须使用新 Experiment ID 并通过 `retry_of` 关联。

## 8. Context Builder 方案

支持 `project_overview`、`code_debugging`、`experiment_analysis`、`decision_review`、`academic_writing`。Builder 先按任务选择记录类型，再应用时间、Experiment、source type、记录数与字符预算；默认排除原始日志、论文片段和源码正文。预算截断必须写入 warnings。

## 9. Skill 接入方案

遵循现有 `docs/skill_integration.md` 的只读入口原则，在项目包实现等价的结构化 Skill 服务，并由 `knowledgehub project skill` 调用：`code-debugging`、`research-result-analysis`、`research-decision-review`、`writing-academic`。服务只读取 Fixture Registry 和本地 Fixture evidence，输出来源、警告、可支持/不可支持结论，不直接修改代码或生成无证据数值。

## 10. 修改和新增文件清单

计划新增：

- `src/knowledgehub/project/{models,registry,context,knowledge,skills,fixture}.py`；
- `src/knowledgehub/cli/project.py` 并扩展 `cli/main.py`；
- `fixtures/v3/fixture_vision_project/` 的代码、配置、测试和本地知识证据；
- `tests/project/` 单元、集成和端到端测试；
- `scripts/run_v3_fixture.py`；
- `reports/v3_fixture/` 要求的验证报告。

## 11. 风险

- 本地 Fixture RAG 只能验证路由、过滤、来源与预算，不能证明正式向量检索质量；
- 单次合成数据/seed 不支持科研泛化结论；
- 主仓库未将 Fixture 建成独立 Git 仓库，Experiment 只能追踪主仓库 commit 与 dirty 状态；
- 文件 Registry 适合小规模单机项目，不适合多用户并发写入；原子替换降低损坏风险，但不提供分布式事务。

## 12. 本轮不实施

多项目 Dashboard、Dataset RAG、自动训练调度、多 Agent、分布式队列、用户环境修改、实验日志向量化、论文自动生成/投稿、Release 长期监控和多用户权限系统均不实施。

