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
| 初轮修复后全量 | 347 passed | 13.52 s | 877 MiB |
| P1-1 原子发布修复及维护演练后 | **353 passed** | **13.85 s** | **877 MiB** |
| P2/P3 质量修复后 | **356 passed** | **11.26 s** | 未重新采集 |
| 扩展评估集及 KH-V2-014～018 修复后 | **362 passed** | **11.44 s** | 未重新采集 |

- Ruff：passed
- strict MyPy：114 source files passed
- `git diff --check`：passed
- Release manifest validation：passed（5 config hashes）
- 失败测试：0
- skipped：0

初轮新增 4 条回归测试：candidate manifest 隔离、KeyboardInterrupt 任务终态、评估输入/答案隔离、统一查询 exact symbol 合并。P1-1 后继续增加 direct-production guard、fresh collection gate、跨存储 release 校验、完整 snapshot candidate recovery、promotion 中断恢复及失败 abort 覆盖。

P2/P3 修复新增 3 条回归：真实 embedding 语义相似度、严格 section heading 分类、Literature bibliography 稳定降权与显式查询例外。

后续新增：Search API 非法 mode/filter 422、Version Diff introduced/removed 默认发现、Writing subsection/caption/front-matter/十类 taxonomy，以及 Code 核心评估集每组至少 10 条。43 条 Code live 报告成功生成。
