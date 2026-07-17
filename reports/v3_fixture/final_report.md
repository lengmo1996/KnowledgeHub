# KnowledgeHub V3 Fixture Final Report

## 1. 最终状态

**PASS**

V3 Fixture 的 Workspace→Environment→Experiment→Failure/Decision→Context→Cross-RAG→Claim→Skill→Isolation/Cleanup 闭环已实际完成。跨库阶段按任务要求只验证隔离 Fixture 的接口和路由，不把模拟数据写入正式向量 Collection；项目查询和四个 Skill 已同时接入 CLI/service 与现有 MCP registry。

下一阶段状态：**PILOT_DEFERRED_NO_REAL_PROJECT**。架构具备进入受控 Pilot 的条件，
但当前没有真实项目，且 `129272a` 的 Registry/Router 仍保持 Fixture-only 安全边界。
真实项目出现后必须按
[`docs/guides/REAL_PROJECT_PILOT.zh-CN.md`](../../docs/guides/REAL_PROJECT_PILOT.zh-CN.md)
逐 Gate 执行；不得直接把真实仓库指向 Fixture Registry。

## 2. V2 准入

当前 V2 为 **PASS**：历史初轮报告保留 `FAIL` 审计，但顶部后续说明及 `remaining_risk_remediation.md` 确认 KH-V2-001～018 全关闭、362 tests/Ruff/MyPy 和运行服务验证通过。V3 未依赖未发布的 Code/Writing candidate 数据。

## 3. 完成内容

- Workspace CRUD/validate/export/archive 与 Fixture-only cleanup；
- NumPy 合成视觉 Fixture、CPU Environment Profile；
- 5 个 Experiment、running→terminal transition events、结构化 metrics/artifacts；
- 1 个 confirmed/resolved Failure 与修复重试；
- 1 个基于实际结果的 accepted Decision；
- 五任务 Context Builder 与预算；
- Fixture Literature/Code/Writing 路由和项目查询；
- 3 个 Claim–Evidence 记录；
- 4 个项目级只读 Skill 入口；
- MCP `knowledge_project_query` / `knowledge_project_skill`，共 17 个 strict tools；
- Workspace 粒度 `flock` 写保护与竞争 writer fail-closed；
- 幂等、隔离、dry-run/真实清理和清理后重建。

## 4. 实验摘要

| Experiment | Status | Config | Main Metric | Runtime | Linked Record |
|---|---|---|---:|---:|---|
| exp-001 | completed | baseline | val acc 0.916667 | 0.014336 s | baseline |
| exp-002 | completed | fusion_add | val acc 0.979167 | 0.018429 s | decision/claims |
| exp-003 | completed | fusion_concat | val acc 0.958333 | 0.015299 s | decision/claims |
| exp-004 | failed | failure_nan | controlled FloatingPointError | — | fixture-failure-001 |
| exp-005 | completed | failure_fix | val acc 0.937500 | 0.014963 s | retry_of exp-004 |

运行时间会随机器波动；记录中的 JSON 是权威值。

## 5. Failure 与 Decision

`fixture-failure-001` 的明确根因是 `inject_nan=true`，由 exp-004 日志与源码证据确认，exp-005 关闭注入后成功。原失败未被覆盖。

`fixture-decision-001` 选择 addition，因为本次 matched run 中它的验证准确率更高且参数更少。该决策仅适用于 Fixture，新增 seed 或真实项目时必须重评。

## 6. Claim–Evidence

- “concat 性能更高”与实际 0.958333 < 0.979167 冲突，状态 contradicted；
- “concat 参数更多”由 145 > 67 支持；
- “不可泛化到真实数据”由 synthetic dataset 配置和 Fixture 文献 note 支持。

## 7. 问题与剩余风险

| Severity | 问题 | 处理/验证 | Remaining risk |
|---|---|---|---|
| closed | 文件 Registry 竞争写入 | Workspace 粒度 `flock`、持锁元数据、竞争写 fail-closed 测试 | 仅单机锁，不是分布式事务 |
| accepted boundary | Fixture Router 不写正式向量 RAG | namespace/source/version 路由测试 | 按本轮要求只证明接口，不代表正式检索质量 |
| closed | 项目 Skill MCP 接入 | 2 个 strict MCP tools、只读注解、路径注入拒绝与调用测试 | 运行中 MCP 服务需在发布时重启加载新代码 |
| P3 | 单 seed、合成数据、CPU timing noise | Claim/Skill 强制限制声明 | 不可形成科研结论 |

## 8. 架构判断

1. Workspace Schema 与单机并发保护足以开始受控只读真实项目 Pilot。
2. Experiment Schema 足够覆盖当前小型运行，并通过 transition event 保存启动快照；复杂训练仍需资源/heartbeat/取消原因。
3. 关键关系（retry/failure/decision/claim/evidence）已具备；未来需要 dataset version 与 parent run。
4. Context Builder 可裁剪且有预算；Workspace 基础元数据极大时仍有基础体积风险。
5. Fixture 隔离可靠：namespace、目录、默认查询、删除目标四层保护已验证。
6. 真实 Pilot 前只需为目标项目做显式授权、独立状态根和正式只读 RAG scope 映射；不得复用 Fixture namespace。

## 9. 是否可接入真实科研项目

**架构评估允许进入受控真实项目 Pilot，但本轮按用户要求延期。** Pilot 必须只读、
非生产、使用独立状态根并由用户显式选择目标仓库；不得自动训练、清理或修改用户仓库。
当前公开 CLI 的 Registry 和 Router 仍是 Fixture-only，因此在真实项目出现后要先完成手册中的
“真实 Pilot 支持启用”代码门禁，再创建 project Workspace。该结论不是允许直接把现有
Fixture Registry 指向用户研究目录。

## 10. 未实际执行或无法验证

- 未写入或查询正式 Qdrant Fixture Collection；
- 未重启当前运行中的 MCP HTTP 服务或执行远程鉴权调用；源码 registry/schema 与本地调用已验证；
- 未验证跨主机/分布式并发；单机 `flock` 竞争已验证；
- 未使用真实论文、真实研究仓库、真实数据、GPU 或网络；
- 未证明 Fixture 结果对任何真实视觉任务有效。

## 11. 延期 Pilot 的可执行交接

真实项目建立后的完整顺序、复制即用的预检/服务/Intake/RAG/MCP 命令、代码门禁、停止条件、
验收表和归档步骤已写入
[`docs/guides/REAL_PROJECT_PILOT.zh-CN.md`](../../docs/guides/REAL_PROJECT_PILOT.zh-CN.md)。
在没有真实项目期间不再创建替代性“真实”数据，也不扩大 Fixture 的权限边界。
