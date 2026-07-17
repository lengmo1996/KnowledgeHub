# Test Results

- V3 + Fixture 定向测试：15 passed（Schema、Registry、Validation、状态机/事件、幂等、Decision/Failure/Claim、Context、Router、Skill、隔离、清理、重建）。
- 全仓库最终 pytest：378 passed，0 failed。
- Fixture 自身：2 passed。
- Ruff：All checks passed。
- strict MyPy：122 source files passed，0 issues。
- MCP schema validate：status=ok，17 tools；项目 MCP 查询、Skill、未知字段拒绝均通过。
- 网络/GPU/Zotero：均未使用。
