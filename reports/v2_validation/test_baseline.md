# 测试基线

| 组 | 结果 | 时间 | 峰值 RSS |
|---|---:|---:|---:|
| core/deploy/sources | 220 passed | 5.54 s | 115 MiB |
| rag（含 Literature pipeline） | 29 passed | 5.31 s | 849 MiB |
| Code multi-RAG | 16 passed | 2.81 s | 647 MiB |
| Writing multi-RAG | 3 passed | 0.89 s | 113 MiB |
| V2/schema/workflow | 53 passed | 4.43 s | 172 MiB |
| MCP/CLI/API | 22 passed | 3.76 s | 163 MiB |
| 修复前全量 | 343 passed | 13.11 s | 877 MiB |
| 修复后全量 | **347 passed** | **13.52 s** | **877 MiB** |

- Ruff：passed
- strict MyPy：112 source files passed
- `git diff --check`：passed
- Release manifest validation：passed（5 config hashes）
- 失败测试：0
- skipped：0

修复后新增 4 条回归测试：candidate manifest 隔离、KeyboardInterrupt 任务终态、评估输入/答案隔离、统一查询 exact symbol 合并。
