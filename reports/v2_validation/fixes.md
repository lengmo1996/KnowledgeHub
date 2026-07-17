# 有限修复记录

本轮只修复能够稳定复现、边界明确且可由回归测试覆盖的问题；没有扩展 V3 功能，也没有重写存储架构。所有修改当前均为工作区变更，未创建 commit、未 push。

## KH-V2-001：Candidate build 覆盖 canonical normalized manifest

- 严重程度：P1
- 复现：使用 `--candidate-collection` 和 `--limit` 构建 10-document candidate 后，正式 `transformers/5.13.1` normalized manifest 被替换为 10 条记录。
- 根因：normalized 输出路径只以 library/version 命名，没有 candidate namespace。
- 修改：`CodeBuildService.build` 增加经过清洗的 `normalized_namespace`；CLI candidate build 写入 `.staging/normalized/<candidate>/...`。
- 回归测试：`test_candidate_build_keeps_canonical_normalized_manifest_unchanged`。
- 运行验证：1-document candidate 生成 staged manifest；canonical manifest SHA-256 前后均为 `1ac21fe1...`。
- 结果：已关闭。

## KH-V2-002：KeyboardInterrupt 遗留 running task

- 严重程度：P1
- 复现：构建过程中 Ctrl-C，锁由 `finally` 释放，但 task/attempt 保持 `running`。
- 根因：`TaskExecutor` 只捕获 `Exception`，不捕获 `KeyboardInterrupt` 所属的 `BaseException`。
- 修改：捕获 `BaseException`，在重新抛出前将运行任务标为 `failed`，错误中保留异常类型；锁仍由原有 `finally` 释放。
- 回归测试：`test_executor_interrupt_is_recorded_and_releases_locks`。
- 结果：已关闭。

## KH-V2-005：Live evaluation 使用答案标签作为查询输入

- 严重程度：P1
- 复现：runner 将 `expected_symbol` 注入 exact-symbol evidence，并将 `expected_function` 用作 Writing 查询 filter。
- 根因：评估输入字段和评分标签没有隔离。
- 修改：新增 `_live_query_filters`，只允许显式 `library/version/symbol/section/writing_function/research_domain` 进入查询；删除 expected-symbol 手工命中注入；冻结 Code fixtures 增加显式 `symbol` 输入。
- 回归测试：`test_live_query_filters_never_use_expected_answers_as_inputs`。
- 运行验证：修复后重新运行 24 条 live V2 评估，11 个组均完成；报告指标不再含 oracle 注入。
- 结果：已关闭。修复前 live 指标不可作为验收证据。

## KH-V2-006：统一 Code 查询未合并精确 SymbolIndex 结果

- 严重程度：P1
- 复现：`PreTrainedModel.from_pretrained` 已存在于 SymbolIndex，但统一查询仅把 symbol 当作向量 metadata filter，返回空 evidence。
- 根因：HubQueryService 未接入 exact symbol catalog。
- 修改：当调用方显式提供 library、version、symbol 时，从只读 SymbolIndex 合并精确命中，并返回 signature、path、line、commit、source URL 和 `exact_symbol_source` evidence role。
- 回归测试：`test_code_query_merges_user_requested_exact_symbol`。
- 运行验证：`transformers@5.13.1` 查询返回 `src/transformers/modeling_utils.py:3874`，confidence/score 1.0，GitHub commit URL 可追踪。
- 结果：已关闭。

## KH-V2-007：部署配置仍引用 0.1.0 image

- 严重程度：P1
- 复现：运行容器 OpenAPI 仅有 `/health` 和 `/search`，V2 `/knowledge/query` 返回 404。
- 根因：compose image tag 与源码版本漂移。
- 修改：`deploy/gpu/compose.yaml` image tag 从 `0.1.0` 更新到 `0.2.5`。
- 验证：成功构建 `knowledgehub-rag-app:0.2.5`，image SHA 前缀 `1a93055a`。
- 结果：代码侧修复完成，但部署未关闭。当前账号不能读取 `/etc/knowledgehub/rag.env`，未绕过权限或替换运行容器。

## 回归汇总

- 全量 pytest：347 passed，0 failed，0 skipped，13.52 s。
- Ruff：passed。
- strict MyPy：112 source files passed。
- Release manifest validation：passed。
- 最终运行完整性：Literature 190,131 points green；Code 124 documents/1,118 chunks/1,118 points green；Writing 134/134 green。

## 未直接修复

- Qdrant、SQLite、chunk artifacts、normalized manifests 和 task state 的跨存储原子 snapshot/rollback：需要调整构建与发布架构，超出有限修复范围。
- Writing 高相似改写检测和 Method section family 误判：保留为 P2，分别需要语义相似度层和更严格的 section classifier。
- Literature References chunk 排名：P3，不影响隔离或来源追踪。

## P1-1 后续修复：不可变 Candidate Release

- 所有非 dry-run Code build 现在必须绑定全新 candidate collection；configured production collection 和稳定 alias 均被拒绝。
- Candidate 同时隔离 physical Qdrant collection、SQLite、chunks、runs 和 normalized manifests，统一存放在 `code/releases/code/<candidate>`。
- Candidate collection 名称已存在时 fail closed；release 完成后不可再次写入。
- 新增 `index validate-candidate`，逐项比对 normalized documents、SQLite active IDs、chunk artifacts 和 Qdrant point IDs，并冻结 SHA-256 artifact fingerprint。
- 仅 `build code --all` 且无 limit/version/incremental/prune 的 validated release 可 stage；bounded smoke candidate 永远不能 promote。
- `index stage` 会立即重新验证 Qdrant 和本地 artifacts；point count 变化或 hash 变化均拒绝。
- Snapshot 2.1 绑定 release manifest；恢复只能写入全新 collection，并复制、重新验证本地 artifacts。旧 Qdrant-only snapshot 默认拒绝恢复。
- Alias/pointer promote 和 rollback 使用持久事务日志；切换前失败自动 abort，alias 已切换但 pointer 未写时可由 `recover-promotion` 完成收敛。
- 实机验证 candidate `knowledgehub_code_atomic_validation_20260717_01`：1 document、61 chunks/points、4 个本地 artifacts、cross-store validation green、fingerprint `80cbb793...`。
- 实机后正式 alias 仍指向 `knowledgehub_code_qwen3_4b_1024_v1`，正式 Code 保持 124 documents/1,118 points green。
- 维护演练状态：已完成 active-scope candidate 的真实 stage/promote/Snapshot 2.1/alias rollback/snapshot recovery；124 documents、1,118 points 和 artifact fingerprint 全程一致，最终 alias 回到原 physical collection。KH-V2-003/KH-V2-004 已关闭。
- `build code --all` 的 source-wide 语义会把 124-document active scope 扩大到 17,437 documents；该 candidate 已安全中断并保持 inactive。新增 `bootstrap-candidate` 作为等价维护发布入口，source expansion 必须显式 `--allow-source-expansion`。

## KH-V2-008：Writing 语义相似度未执行

- CLI 与 MCP 的 source-similarity audit 现在通过 Writing RAG 配置创建 embedding pool，分批向量化候选和来源，并使用 cosine similarity。
- 输出增加 semantic backend 的 model、revision、dimension；端点失败时直接失败，不把未执行伪装为已执行。
- 回归测试 `test_embedding_similarity_detects_reordered_paraphrase`：重排改写返回 high，semantic=`evaluated`。
- 结果：已关闭。

## KH-V2-009：Section family 子串误判

- 新增共享的保守 section heading classifier，去除编号、统一中英文标题，只对完整已知标题分类。
- `A Practical Approach to Small Data Learning` 不再命中 Method；`3. Methods` 仍正确命中。
- 查询过滤和 Venue profile section 选择共用同一规范化逻辑。
- 结果：已关闭。

## KH-V2-010：Literature References 排名

- 普通 Literature 查询先取 prefetch 候选，再将 metadata 明确标记为 References/Bibliography 的结果稳定移动到内容段之后。
- 显式查询 references/citations/bibliography/参考文献时保留原始排名。
- 响应出现降权时增加 `bibliography_sections_demoted` warning。
- 结果：已关闭。

## 运行环境复核完成

- Search API 已重建并 force-recreate 为 `knowledgehub-rag-app:0.2.5`；health=200、未鉴权=401、OpenAPI 含 `/knowledge/query`，Literature/Code/Writing 均返回 200 和来源。KH-V2-007 已关闭。
- Literature 运行查询返回 `bibliography_sections_demoted`，Top-3 均为内容章节。KH-V2-010 运行态复核通过。
- Writing 使用 Qwen3-Embedding-4B 的真实重排改写测试 cosine=0.9625，返回 high 且 semantic=`evaluated`。KH-V2-008 运行态复核通过。
- editable metadata、源码及 MCP service 均为 0.2.5；LAN/Tailscale listener 均 active/running。KH-V2-013 已关闭。

## KH-V2-014：Search API 非法输入返回 500

- `knowledge_base`、`return_mode`、`mode`、`reranker_profile` 和 `fallback_policy` 改为显式 Literal 枚举。
- route 将 service 层 `ValueError` 统一映射为 HTTP 422；未知 filter 同样返回客户端错误。
- 回归测试和 Pydantic/route 直接调用通过。
- 用户完成镜像重建后，运行态非法 mode/未知 filter 均返回 422；8 路并发 8/8 成功，容器重启后 health=200 且 422 行为保持。
- 结果：已关闭。

## KH-V2-015：默认 Version Diff 漏掉 introduced/removed

- `SymbolIndex.changed_pairs` 从 shared-name inner join 改为有界 UNION，分别覆盖 shared/changed、removed 和 introduced。
- pair 两侧允许为空，并按 symbol ID 回读完整记录。
- Transformers 5.13.0→5.13.1 真实默认 dry-run：introduced=2、modified=3、signature_changed=1，6 documents/18 chunks。
- 结果：已关闭；未修改正式 Code index。

## KH-V2-016～018：Writing subsection、材料清洗与十类 taxonomy

- section family 支持学术编号和受限的描述性 subsection 前后缀，同时继续拒绝论文标题子串误判。
- derive 在分析前排除严格编号 caption、短 Latin material、source-title section 和 front matter。
- analyzer 新增/拆分 background、quantitative_comparison、limitation，并让 evaluation runner 使用共享 section classifier。
- 5-paper read-only re-derive：134→101 entries，caption=0、front matter=0；20-paper dry-run 459 entries，十个目标类 10/10。
- 结果：三项已关闭；正式 Writing index 未改，需后续 candidate 发布。

## Code 评估集扩充

- API usage、compatibility、debugging、source navigation 各 10 条，共 40 条核心样本；另有 3 条 repository adaptation。
- 新增核心组最少 10 条的回归保护。
- Live V2：核心证据类型命中 35/40，版本/符号均 100%；全量报告见 `code_live_43.json`。

## 最新回归汇总

- 全量 pytest：362 passed。
- Ruff：passed。
- strict MyPy：114 source files passed。

## 最终运行态复核

- Search API image `knowledgehub-rag-app:0.2.5`，image ID `sha256:aa301e48...`，最终 running/healthy。
- 鉴权 health=200、未鉴权=401、OpenAPI 含 `/knowledge/query`。
- Literature/Code/Writing 查询均为 200 且各有来源。
- 非法 mode=422、未知 filter=422。
- 8 路并发 8/8=200；重启后 health=200、非法 mode=422。
- KH-V2-001～018 全部关闭。
