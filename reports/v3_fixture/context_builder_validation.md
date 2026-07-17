# Context Builder Validation

- 支持：project_overview、code_debugging、experiment_analysis、decision_review、academic_writing。
- 选择：Debug 只保留 Code scope、目标失败 Experiment/Failure；Writing 删除源码路径正文，仅保留实验摘要、Claim、Decision 和 pattern scope。
- 预算：max records、max characters、days、experiment IDs、source types 以及 raw log/paper fragment 开关；默认两项均 false。
- 运行结果：五种 CLI 调用均 exit 0；小预算产生明确 `*_truncated_by_*_budget` warning。
- 限制：字符预算低于基础 Workspace/Environment 元数据大小时只能告警，不能破坏 Schema 来硬截字符串。
