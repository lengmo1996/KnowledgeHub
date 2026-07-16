# Code RAG 检索评估

## 修复后可信 live V2 结果

24 条冻结样本、11 个组，0 failed groups。修复后 runner 不再将 `expected_symbol`/`expected_function` 用作查询输入；只有 fixture 的显式 `symbol` 字段进入请求。

| 组 | Recall@10 | MRR | 正确版本率 | 正确符号率 | 来源完整率 | 平均延迟 |
|---|---:|---:|---:|---:|---:|---:|
| API usage | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.048 s |
| Compatibility | 0.500 | 0.500 | 1.000 | 1.000 | 0.500 | 0.243 s |
| Debugging | 0.500 | 0.500 | 1.000 | 1.000 | 0.500 | 0.264 s |
| Repository adaptation | 0.667 | 0.667 | 0.667 | 1.000 | 0.667 | 0.242 s |
| Source navigation | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.279 s |

- Unsupported inference rate：0
- 8 路并发 CLI 查询：8/8 成功；单进程 wall time 1.75–2.20 s。
- 查询 envelope 包含 library/version/symbol/source type/path/URL/事实与 warnings。

限制：正式集仅 24 条，低于任务建议的 40 条；没有人工 conclusion accuracy；Compatibility/Debug/Repository recall 仍偏低。
