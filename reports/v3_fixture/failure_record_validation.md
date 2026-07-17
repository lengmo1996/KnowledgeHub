# Failure Record Validation

- Failure：`fixture-failure-001`，`numerical_error`，状态 resolved。
- 触发：`failure_nan.yaml` 的 `inject_nan=true` 在 epoch 1 后稳定抛出 `FloatingPointError`。
- 根因：明确的 Fixture 故障注入；`root_cause_status=confirmed`，不是推测。
- 证据：exp-004、traceback log、`fixture-code-002`、修复 exp-005。
- 修复：关闭注入，保持 seed/data/optimizer；exp-005 完成且不覆盖 exp-004。
- Debug Skill：返回相同根因、验证步骤、修复建议与 high confidence。
