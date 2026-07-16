# KnowledgeHub V2 运行验证与稳定性验收报告

## 1. 执行摘要

- 最终状态：**FAIL**
- 是否建议进入 V3：**否**
- 验证基准：branch `main`，commit `114d62c3d441def676647c8708ce094cae739e9d`
- 当前数据状态：已恢复且完整性验证全绿；Literature、Code、Writing 三个知识库均可通过 CLI/MCP 本地路径独立查询。
- 判定失败的直接原因：真实中断证明 production Code build 会跨 Qdrant、SQLite 和文件 artifacts 部分提交；现有 snapshot 只覆盖 Qdrant，无法一键原子恢复整个正式索引。任务硬条件“索引更新失败不会损坏当前索引”未满足。
- 另一项核心阻塞：运行中的 Search API 仍为 `0.1.0`，没有 V2 `/knowledge/query`；`0.2.5` image 已构建，但当前账号无权读取受保护的 env file，未执行部署切换。

本轮没有发现 Literature 数据被覆盖、知识库间向量污染、敏感凭据写入报告或 Git。事故中的正式 Code 状态已经恢复为基线，但恢复需要人工跨存储对账，不能把“最终恢复成功”视作原子回滚通过。

## 2. 验证环境

| 项目 | 值 |
|---|---|
| Branch / Commit | `main` / `114d62c3d441def676647c8708ce094cae739e9d` |
| OS | Ubuntu 22.04.5 LTS, kernel 6.8.0-134-generic |
| Python | 3.12.13, conda `$CONDA_PREFIX`（绝对路径已脱敏） |
| KnowledgeHub | source 0.2.5；editable metadata 0.1.0（漂移） |
| GPU | 2 × NVIDIA GeForce RTX 3090, 24 GiB |
| CUDA | system toolkit 12.1；Torch build 13.0；driver 580.159.03 |
| 主要包 | torch 2.11.0+cu130；transformers 5.13.1；qdrant-client 1.18.0；FastAPI 0.139.0；MCP 1.28.1 |
| Embedding | Qwen/Qwen3-Embedding-4B，revision `5cf2132...`，1,024 dimensions |
| Vector store | Qdrant 1.18.2 |
| 空间 | root 357 GiB available；`/data` 1.2 TiB available |

测试数据包括：现有 Zotero Literature 索引；PyTorch、Transformers、Lightning 等 5 个 Code library/4 个明确版本；Transformers 5.13.0/5.13.1 跨版本符号；2 个已登记真实仓库；3 篇隔离 Writing candidate；24 条冻结 live evaluation 样本；3 个 Debug fixtures。

## 3. 功能验证结果

| 范围 | 结果 | 运行证据与结论 |
|---|---|---|
| Repository / environment | PASS | 起始 worktree clean；记录 branch、commit、入口、配置、服务、GPU、磁盘和脱敏凭据状态。 |
| Existing tests | PASS | 修复后 347 passed；Ruff、strict MyPy、release validation、diff check 均通过。 |
| Literature RAG | PASS_WITH_LIMITATIONS | 190,131 points green；真实主题查询和来源追踪成功；与 Code/Writing 隔离。Top-3 中有 1 个 References chunk，属 P3 排名问题。 |
| Code sync / idempotency | PASS_WITH_LIMITATIONS | `transformers@5.13.1` 连续两次真实同步均 skipped，3.02/3.41 s，8,132 files、bytes、marker 和索引计数不变。Candidate manifest 隔离缺陷已修复。 |
| Multi-library / multi-version | PASS | 5 libraries 共存；Transformers 5.13.0/5.13.1 document/symbol ID 不冲突，指定版本查询有效；环境 profile 已脱敏保存。 |
| AST / Symbol Index | PASS | 202,897 symbols、1,045,496 relations、0 duplicate IDs；短名/FQN exact lookup 正确，包含 path、line、version、commit URL。 |
| Version Diff | PASS_WITH_LIMITATIONS | `_LazyAutoMapping.register` 类型签名变化被识别为 `signature_changed`，unchanged 对照无误报；有两版源码证据。未真实覆盖 moved/renamed/deprecated/removed 全状态。 |
| Code retrieval | PASS_WITH_LIMITATIONS | 修复 oracle leakage 后运行 24 条 live 样本；API/source navigation Recall@10=1.0，compatibility/debug=0.5，repository adaptation=0.667；来源可追踪。正式集小于建议 40 条。 |
| Repository adaptation | PASS_WITH_LIMITATIONS | 两个真实登记仓库 profile/compatibility evidence 可验证；分别覆盖 AMP API 迁移和 Lightning 配置迁移；`py_compile` 与 GPU autocast smoke 通过。未安装旧项目完整依赖或运行全量训练。 |
| Debug workflow | PASS_WITH_LIMITATIONS | 3 个 API/runtime/project-config fixture 完成 traceback 归属分类，没有把项目配置错误统一归因于第三方库；未做外部 issue/PR 在线采集。 |
| Writing RAG | PASS_WITH_LIMITATIONS | 隔离 3-paper derive 生成 87 entries；重复运行 87/87 skipped；正式 134/134 不变；来源追踪率 1.0。高相似改写漏报、Method family 误判仍存在，且没有覆盖任务列出的全部 10 类标签。 |
| CLI / MCP | PASS_WITH_LIMITATIONS | MCP doctor/validate 通过，15 tools，两个 listener healthz 200；8 路跨库只读查询 8/8 成功。为保护设备 token，未执行真实鉴权 HTTP tool invocation。 |
| Search API | FAIL | 运行的 0.1.0 image 健康且鉴权正常，但 V2 `/knowledge/query` 404；0.2.5 image 已构建，因 `/etc/knowledgehub/rag.env` 权限边界未部署。 |
| Snapshot / rollback | FAIL | Alias candidate promote/rollback 和 Qdrant snapshot recovery 成功；跨 Qdrant/SQLite/artifacts/manifest 恢复不原子，需要人工 reconciliation。 |
| Performance baseline | PASS_WITH_LIMITATIONS | 已采集同步、candidate build、Writing derive、查询、并发、RSS 和常驻 GPU 显存；未采集完整阶段级 GPU/内存时间序列。 |

## 4. 关键指标

### 数据与完整性

| Knowledge base | 文档/条目 | Chunks / vectors | 最终状态 |
|---|---:|---:|---|
| Literature | 3,574 source documents；3,497 ready；77 missing attachments | 190,131 | green |
| Code | 124 normalized/state/artifacts | 1,118 / 1,118 | green |
| Writing | 134 derived/state/artifacts | 134 / 134 | green |

- Code Symbol Index：202,897 symbols；1,045,496 relations；0 duplicate symbol IDs。
- 多版本：Transformers 5.13.0 为 68,156 symbols，5.13.1 为 68,158 symbols；旧版本未被覆盖。
- 同步幂等：2/2 no-change sync skipped；文件、bytes、manifest 与索引计数无变化。
- Writing 幂等：第二次隔离 derive 87/87 skipped，0 indexed。

### 测试与检索

- 全量测试通过率：347/347（100%），0 failed，0 skipped；13.52 s；峰值 RSS 约 877 MiB。
- Code live evaluation（24 samples）：
  - API usage：Recall@10/MRR/正确版本/正确符号/来源完整率均 1.0。
  - Source navigation：上述指标均 1.0。
  - Compatibility：Recall@10/MRR 0.5，正确版本/符号 1.0，来源完整率 0.5。
  - Debug：Recall@10/MRR 0.5，正确版本/符号 1.0，来源完整率 0.5。
  - Repository adaptation：Recall@10/MRR/正确版本/来源完整率 0.667，正确符号 1.0。
- Writing live evaluation：function recall 1.0；source traceability 1.0；duplicate material ratio 0；wrong-domain rate 0。该指标不等价于任务要求的十类人工分类准确率。
- Unsupported inference rate：0。

### 延迟与资源

- Literature 单查询内部 total 0.113 s；dense 0.039 s、Qdrant 0.074 s。
- Code 分组平均延迟 0.242–1.048 s；Writing pattern retrieval 平均 0.239 s。
- 8 路并发 CLI：8/8 成功；每个进程 wall 1.75–2.20 s（含启动开销）。
- 3-paper Writing derive：首次 6.76 s，重复 2.11 s。
- 10-document Code candidate：6.18 s，109 chunks。

## 5. 已修复问题

| ID | 严重度 | 根因 | 有限修复与验证 | 结果 |
|---|---|---|---|---|
| KH-V2-001 | P1 | Candidate 与正式 normalized manifest 共用路径 | Candidate namespace 写入 `.staging`；新增回归测试；canonical hash 实测不变 | closed |
| KH-V2-002 | P1 | TaskExecutor 未捕获 KeyboardInterrupt | 中断时写 failed attempt 并释放锁；新增回归测试 | closed |
| KH-V2-005 | P1 | 评估 runner 将 expected labels 注入查询 | 输入/评分字段隔离，删除 oracle exact hit；24 条 live 集重跑 | closed |
| KH-V2-006 | P1 | 统一查询未合并 SymbolIndex | 显式 symbol 请求合并 exact source hit；返回路径/行号/commit URL；新增回归测试 | closed |
| KH-V2-007（代码侧） | P1 | Compose image tag 漂移 | tag 更新为 0.2.5，image 构建成功 | deployment open |

详细复现、修改和测试见 `fixes.md`。本轮未创建 fix commit，因此 failure records 的 `fix_commit` 均为 null。

## 6. 未解决问题

| ID | 严重度 | 问题与影响 | 临时规避 | 推荐处理版本 |
|---|---|---|---|---|
| KH-V2-004 / KH-V2-003 architecture | P1（曾触发 P0 事故） | Production build 跨 Qdrant、SQLite、artifacts、manifest 非原子；Qdrant-only snapshot 不能恢复完整状态 | 禁止 direct production build；强制 candidate → validate → alias promote；变更前同时冻结数据库和 artifact manifests | V2 blocker |
| KH-V2-007 | P1 | 运行 Search API 缺少 V2 unified endpoint | 当前使用 CLI/MCP；由具备 env-file 权限的运维者部署已构建 0.2.5 image 并做 smoke test | V2 blocker |
| KH-V2-008 | P2 | Writing 高相似改写漏报，semantic layer 未执行 | 将结果标注为 lexical internal similarity，不作为语义查重结论 | V2 quality |
| KH-V2-009 | P2 | Method filter 的 substring heuristic 会误收标题/作者材料 | 人工核对 section/location；查询时缩小 writing function/domain | V2 quality |
| KH-V2-010 | P3 | Literature References chunk 偶尔进入 Top-K | 使用 section filter 或后处理排除 bibliography | Backlog |
| metadata drift | P3 | source 0.2.5 与 editable package/MCP status 0.1.0 不一致 | 以 source commit 和 tool registry 为准 | V2 release hygiene |

P0 状态说明：KH-V2-003 事故已恢复，当前没有仍处于损坏状态的 P0；但其根因没有由架构修复关闭，已降级为明确的 V2 P1 blocker。按硬性验收条件，仍必须判 `FAIL`。

## 7. 实际执行的主要命令

完整命令、参数和安全说明见 `commands.md`。主要入口包括：

```bash
knowledgehub validate all
knowledgehub sync code --library transformers --version 5.13.1
knowledgehub environment capture --output /data/KnowledgeHub/code/state/environments/v2-validation-20260717.json
knowledgehub build code --library transformers --version 5.13.1 --candidate-collection <candidate>
knowledgehub derive writing --config reports/v2_validation/fixtures/knowledgehub_validation.yaml
knowledgehub query --knowledge-base literature|code|writing ...
knowledgehub code symbol inspect ...
knowledgehub code version compare ...
knowledgehub repository validate ...
knowledgehub snapshot create ...
knowledgehub snapshot recover ...
knowledgehub mcp doctor
knowledgehub mcp validate
pytest -q
ruff check .
mypy --strict src/knowledgehub
```

生产恢复过程中还使用了报告 fixtures 中的精确 reconciliation/reindex 脚本；这些脚本只针对本次已知 document IDs 和冻结 manifest，不应替代正式 recovery 命令。

## 8. 未实际验证或受环境限制的部分

- Search API 0.2.5 容器切换和部署后 `/knowledge/query` smoke：缺少 `/etc/knowledgehub/rag.env` 读取权限。
- 真实鉴权 MCP HTTP tool invocation：没有读取或输出用户设备 token；只验证 healthz、doctor、tool registry 和 22 条协议/鉴权测试。
- 推荐的 40 条 Code 人工评估集：现有冻结集为 24 条；没有临时扩充或硬编码样本。
- Writing 推荐 10–30 篇：为保护现有文献状态，只在隔离 collection 中实际派生 3 篇；正式索引已有 134 entries，但未重新处理整个 Zotero 库。
- Writing 十类功能的逐类人工抽查：活动 taxonomy 不含全部目标标签，未伪报覆盖。
- 目标仓库完整依赖安装、单元测试和训练：只执行现有 adaptation evidence validate、`py_compile` 和 GPU autocast smoke，避免污染当前环境或运行大规模训练。
- Version Diff 的 moved、renamed、deprecated、removed、introduced 全状态真实样本：只验证真实 signature_changed 和 unchanged 对照。
- Issue/PR 在线证据采集、Zotero 全量重同步、完整性能阶段时间序列：未执行。

## 9. V3 准入结论

**不建议进入 V3。** Literature、Code、Writing 的基本独立运行、一个以上真实仓库适配、Debug fixture、Writing 派生、真实失败样本均已形成；主要剩余问题仍包含“索引更新非原子”和“统一 API 部署漂移”，不符合 V3 准入要求。

进入 V3 前至少完成：

1. 将 Code 构建发布路径收敛为不可绕过的 candidate → integrity validate → atomic alias promote；snapshot manifest 同时覆盖 Qdrant snapshot ID、SQLite checksum、normalized/chunk artifact hashes、task state，并自动演练一键恢复。
2. 由授权运维账号部署 0.2.5 Search API，验证三知识库 `/knowledge/query`、来源、错误状态和重启后行为。
3. 扩充并人工审阅至少 40 条 Code query set，重点提升 compatibility/debug/repository recall，保持评估输入与答案标签隔离。
4. 修复 Writing section classifier 和语义相似度层，用 10–30 篇隔离 collection 覆盖目标十类并人工抽查。
5. 清理 package metadata 版本漂移，完成 release smoke。

以上 V2 blocker 关闭并重新验收后，V3 的首个建议模块是**只读 Project Workspace context/catalog**：先统一仓库、环境 profile、依赖和证据引用，不直接引入自动修改或实验调度。
