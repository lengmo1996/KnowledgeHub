# Workspace Validation

- 阶段目标：创建、读取、列出、校验、导出、归档和受限清理 Workspace。
- 实现：Schema 3.0、唯一 ID、fixture/test 标记、Repository/Environment/Knowledge 引用和原子 JSON Registry。
- 结果：`fixture-vision-project` 校验 `valid=true`，Repository 与 `fixture-cpu` 存在，三个 namespace 均以 `fixture-` 开头。
- 隔离：默认 `workspace list` 返回空；显式 `--include-fixtures` 才返回该 Workspace。
- 测试：重复 create 为 unchanged；archive/export、缺失资源、正式 namespace 拒绝、路径包含性均有测试。
- 未实施：Workspace 间引用，因此循环引用在 Schema 上不可表达。
