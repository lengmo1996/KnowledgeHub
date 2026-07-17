# Code RAG 检索评估

状态：**PASS_WITH_LIMITATIONS**

## 评估口径

- 四个核心类别各 10 条：API usage、compatibility、debugging、source navigation，共 40 条。
- 另保留 repository adaptation 3 条，因此 live 报告总样本数为 43。
- runner 不使用 `expected_symbol` 或其他答案标签构造查询；只有 fixture 的显式输入字段进入检索。
- 冻结集 JSONL schema 全部通过，并新增“核心类别不得少于 10 条”的回归测试。

## Live V2 结果

| 组 | 样本 | Recall@10 | MRR | 正确版本率 | 正确符号率 | 来源完整率 | 平均延迟 |
|---|---:|---:|---:|---:|---:|---:|---:|
| API usage | 10 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.332 s |
| Compatibility | 10 | 0.600 | 0.600 | 1.000 | 1.000 | 0.600 | 0.277 s |
| Debugging | 10 | 0.900 | 0.900 | 1.000 | 1.000 | 0.900 | 0.266 s |
| Source navigation | 10 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.248 s |
| Repository adaptation | 3 | 0.667 | 0.667 | 0.667 | 1.000 | 0.667 | 0.252 s |

核心 40 条的期望证据类型命中为 35/40（87.5%）；正确版本率和正确符号率均为 100%，unsupported inference rate 为 0。完整机器报告见 `code_live_43.json`。

## 未命中解释

- Compatibility 的四条 `version_diff` 场景未命中正式索引；修复后的六份 diff 当前只做了 dry-run，未写入正式 alias。
- Debugging 的无显式 symbol 通用导入错误场景未得到期望 `source_code` evidence；九条精确 symbol 场景均命中。
- Repository adaptation 的通用 repository-profile 场景未命中；两个绑定真实 API symbol 的迁移场景命中。

这些结果通过当前每组 Recall@10 ≥ 0.5 的门槛，但正式 candidate 若纳入 diff/repository profile evidence，仍应重新跑同一 43 条集合确认提升。
