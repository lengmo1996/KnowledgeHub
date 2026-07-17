# Claim–Evidence Validation

| Claim | Status | Evidence |
|---|---|---|
| concat validation accuracy 高于 addition | contradicted | exp-002/003 `/metrics/validation_accuracy`；实际 0.958333 < 0.979167 |
| concat projection 参数更多 | supported | exp-002/003 `/metrics/parameter_count`；145 > 67 |
| 结果不可泛化到真实视觉数据 | supported | exp-002 `/dataset/type` + fixture-lit-002 |

所有数值由 Experiment Record 动态生成或通过 JSON Pointer 解析；每条 Claim 均有 scope 和 limitation。Writing Skill 读取 Claim Record，不生成游离指标。
