# P2/P3 与剩余运行风险修复记录

时间：2026-07-17 12:04 +08:00

## 当前结论

| 项目 | 状态 | 证据 |
|---|---|---|
| KH-V2-001～013 | closed | P1 原子发布演练、Search API 0.2.5、Writing semantic、Literature 排名、metadata/MCP 均已验证 |
| KH-V2-014 Search API input validation | closed | 新 0.2.5 镜像对非法 mode/filter 均返回 422，重启后复测仍通过 |
| KH-V2-015 Version Diff discovery | closed | 真实默认 dry-run 含 introduced=2、modified=3、signature_changed=1，共 6 docs/18 chunks |
| KH-V2-016 Writing subsection family | closed | 现有 manifest 分类率 63/134 → 128/134；标题误判回归通过 |
| KH-V2-017 Writing material quality | closed | 同 5-paper dry-run 为 101 entries，caption/front matter 均为 0 |
| KH-V2-018 Writing V2 taxonomy | closed | 20-paper/459-entry dry-run 覆盖目标 10/10 类；十类 fixture 通过 |

## 质量门

- 全量 pytest：362 passed，0 failed。
- Ruff：passed。
- strict MyPy：114 source files passed。
- Code live：核心 40 条期望证据类型命中 35/40；版本和符号命中均 100%；全量 43 条报告已固化。
- Search API 当前运行态：health/auth/OpenAPI/三库查询/8 路并发/容器重启恢复全部通过。

## 正式状态边界

- 正式 Code alias 未切换；Version Diff 修复只做 dry-run，正式检索仍缺四条新 diff evidence。
- 正式 Writing alias 未切换；清洗后的 101-entry 五篇结果与十类 taxonomy 尚未发布。
- 最终只读回读：Code alias `knowledgehub_code_current` 仍指向原始 `knowledgehub_code_qwen3_4b_1024_v1`；Writing 仍查询 `knowledgehub_writing_qwen3_4b_1024_v1`，无 staged candidate。
- KH-V2-001～018 已全部关闭，V2 稳定性阻塞项清零。Writing/Code 新派生数据尚未发布属于明确的发布边界，不是当前正式 alias 的一致性故障；如决定上线这些质量改进，应分别走 candidate 发布并重跑 43 条 Code + Writing 十类 live 回归。
