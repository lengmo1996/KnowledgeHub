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
