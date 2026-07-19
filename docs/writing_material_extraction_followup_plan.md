# Zotero 写作素材提取后续实施计划

- 基线审计：`docs/writing_material_extraction_implementation_audit.md`
- 原则：只列未完成或需修正工作；先 provenance/exact-span/schema/dry-run，再扩量；正式索引前必须有人审 gate
- 当前状态：Phase 1-5、Phase 6A、Phase 6A.1-6A.7、Phase 6B.1–6B.1.15 contract hardening、当前30篇真实 extraction、Phase 6B.2全量审核和Phase 6B.3隔离candidate检索验收均已完成。run `20260719T064746Z-f99463512f16` 为30/30、0失败；2496条hash-bound decision全部accepted，complete snapshot为pending=0。新隔离collection `knowledgehub_writing_material_candidate_20260719_f99463512f16` accepted-only写入973/973 derived assets，生产Writing保持134 points；8条中英文sparse用例得到recall@5=0.75、MRR=0.75、source-join=1.0、duplicate=0，最终全部pilot gates通过并返回`eligible_for_manual_expansion_decision`。Phase 6仅余工作项9的用户扩量决定，尚未扩量、promotion或修改生产索引

## Phase 1：状态与 schema 安全收口（P0）

### 输入

- 当前 `evidence-v1` / `writing-material-v1` / run manifest / review event；
- 现有 fake fixture 与真实 partial run 的只读统计；
- 已确认的 span failure、abstraction failure 和 interrupted checkpoint 路径。

### 工作

1. [x] 为从磁盘重读的 evidence、strategy、template、phrase 增加完整 closed-world validator，供 review/validate/index 共同调用。
2. [x] 明确 run 状态 gate：`running` 禁止 review/apply/index；`partial` 默认禁止 index。本阶段未实现 waiver。
3. [x] 修正 document outcome：span/quality failure 不得无条件落为 `success`；区分 `success`、`partial`、`failed` 并确保 retry 能重新处理失败部分。
4. [x] abstraction 失败前 checkpoint 已验证 evidence，使 evidence 与失败状态均可恢复。
5. [x] 增加 failed/partial/changed/retry、manifest status、stored artifact corruption、exact-span 边界和 dry-run 回归测试；inactive 行为继续由既有测试覆盖。

### 输出

- 统一 artifact validation API；
- 明确且可重试的 extraction state machine；
- review/index 前置状态 gate；
- 对应单元、集成和 CLI dry-run 测试。

### 验收标准

- [x] 任意必需字段缺失、未知字段、enum/version/reference 不合法均在 review/index 前拒绝；
- [x] 模拟 interrupted run 无法 review/apply/index；
- [x] 模拟一个有效 span + abstraction failure 时 evidence 可恢复且 document 可 retry；
- [x] 模拟全部 span rejection 时 document 不能成为普通 `unchanged success`；
- [x] dry-run 保证不创建 candidate 输出；既有 extraction dry-run 测试继续验证零 state/cache/run/TaskStore 写入；
- [x] 全仓 pytest、Ruff、mypy 通过。

### 影响

- Schema：可能只增加 validator，不必立即升级；若改变 document status 枚举/state table，则需要 state migration。
- 重新处理：只重试 failed/partial 文档；不应全量重跑成功文档。
- 索引：禁止写正式索引；现有 candidate 不自动修改。

### 实施记录（2026-07-18）

实际修改：

- `src/knowledgehub/writing_rag/materials.py`：新增 `validate_stored_record()` 及 evidence/material/trace/source-span 的 v1 closed-world 校验；
- `src/knowledgehub/writing_rag/extract.py`：修正 partial/failed outcome、失败 evidence checkpoint、failed retry 和 classification cache refresh；
- `src/knowledgehub/writing_rag/review.py`：增加 completed-run gate、磁盘 artifact 重校验、manifest/trace version 一致性校验及 successful-run-only candidate gate；
- `tests/writing_material/test_extract_review.py`、`test_materials.py`、`test_provider_and_dedup.py`：补齐 corrupt artifact、状态恢复、版本失效、exact-span、provider retry 和 dry-run 覆盖；
- 本计划、基线审计与 `docs/design/ZOTERO_WRITING_MATERIAL_PIPELINE.zh-CN.md`：记录实际状态和设计偏差，同时保留审计历史矩阵。

设计与审计偏差：

- 未升级 asset schema v2；保留 `evidence-v1` / `writing-material-v1`，在读盘边界增加严格校验，避免为尚未决定的 `processed_at`、`extractor_version` 和 review projection 提前迁移。
- `partial` run 允许生成审核材料和 accepted snapshot，以便恢复已验证 evidence；它的 validation status 保持为 `partial`，`index_eligible=false`，且没有 waiver 路径。
- failed retry 显式跳过旧 classification cache，防止同一个 schema 合法但 exact-span 非法的缓存响应被永久复用。
- 现存真实 run `20260717T142521Z-f6bf64b6b314` 经只读验证为 `partial`、`index_eligible=false`；其 516 evidence、138 strategy、128 template、163 phrase 均通过新增读盘校验。candidate dry-run 被 gate 拒绝，未创建新 collection。

验证结果：

- `/home/lengmo/anaconda3/envs/rag/bin/python -m pytest -q tests/writing_material`：32 passed；
- `/home/lengmo/anaconda3/envs/rag/bin/python -m pytest -q`：410 passed；
- `/home/lengmo/anaconda3/envs/rag/bin/python -m ruff check .`：通过；
- `/home/lengmo/anaconda3/envs/rag/bin/python -m mypy src`：127 个 source files 通过；
- 未调用真实 LLM，未处理完整 Zotero 库，未修改生产/候选索引、现有数据或缓存，未增加依赖。

未解决事项：asset schema v2 字段、完整审核 decision 策略和临时 pilot artifacts 归档仍保留在“用户决策门”；CLI dispatch 级 resume/retry E2E 仍属于 Phase 4，不因本阶段服务层 dry-run 覆盖而提前标记完成。

## Phase 2：provenance 与 exact-span 边界加固（P0/P1）

### 输入

- 经人工核对的少量 Docling 真实 fixtures；
- Unicode normalization、换行、重复文本、跨 segment、连字符、ligature、跨页和 OCR 样本；
- 现有 Literature chunk Parquet contract。

### 工作

1. [x] 建立不含大段版权正文的最小真实 provenance fixture corpus。
2. [x] 明确 Docling `charspan` 在支持版本中的语义，并对版本漂移做 fail-closed 检查。
3. [x] 为重复文本、Unicode、换行、跨页和 segment gap 增加 exact-span 测试；任何 fuzzy/normalization 结果不能直接成为 evidence。
4. [x] 实现可选的 Literature chunk -> paragraph/sentence/source-span map；无法可靠 join 时保存 `not_available` 原因而非猜测。
5. [x] 评估 PyMuPDF/OCR：PyMuPDF 继续拒绝；Docling OCR 只有满足同一 item-level contract 才可进入，不提供低置信度降级。

### 输出

- provenance compatibility matrix 与 fixture；
- exact-span 边界测试；
- 可版本化 chunk-map contract 或明确的 unsupported 状态。

### 验收标准

- [x] 所有支持样本能稳定复现 paragraph/sentence/page/char mapping；
- [x] 所有 normalization-only、歧义或缺 map 样本均被拒绝；
- [x] parser/version 变化会触发 changed/reprocess；
- [x] source revalidation 能发现任何位置漂移。

### 影响

- Schema：若加入 chunk map 或 normalization metadata，升级 evidence schema。
- 重新处理：schema/provenance version 变化需要重建相关 evidence。
- 索引：accepted snapshot/candidate 必须重建；正式索引仍不受影响。

### 实施记录（2026-07-18）

实际修改：

- `src/knowledgehub/writing_rag/provenance.py`：升级 `docling-provenance-v3`，加入 `docling-charspan-v1` envelope/schema/version/bbox 校验及 `writing-chunk-map-v1`；
- `src/knowledgehub/chunking/structural.py`：新生成的 native Docling chunk 只保留版本化 `doc_item_refs`，不复制正文或猜测 offset；
- `tests/writing_material/fixtures/provenance/docling-2.112-schema-1.10.sanitized.json`：由本机现有 Docling 资产结构抽样后去正文构造的最小 fixture；
- `tests/writing_material/test_provenance.py`、`test_materials.py`、`test_extract_review.py`：覆盖跨页、Unicode/换行、重复、gap、bbox、version/schema 漂移、chunk ref 缺失/歧义、changed disposition 和 source location drift；
- `docs/writing_material_provenance_compatibility.md`：记录支持窗口、offset 语义、拒绝路径和 chunk-map contract。

设计偏差和影响：

- 仅放行已由三份现有资产只读验证的 Docling `2.112.x` + `DoclingDocument 1.10.0`；未知新版本先拒绝，而不是依赖项目宽泛的 `<3` 安装范围。
- 未升级 evidence schema；chunk mapping 是独立可选 contract，evidence 继续以 Docling item source span 为事实源。
- 现存 chunk 缺 `doc_item_refs`，返回 `chunk_provenance_contract_missing_or_unsupported`。本阶段没有重建 chunk、candidate 或正式索引。
- reconstruction v2 -> v3 已进入 version bundle，因此后续安全重跑会将旧成功文档识别为 changed；本阶段没有启动该重跑。

验证结果：

- writing-material 专项：39 passed；
- 全仓 pytest：417 passed；
- Ruff：通过；mypy：127 个 source files 通过；
- 三份现有 Docling 2.112.0 资产只读重建成功，paragraph 数为 55/129/49，coverage 均高于 0.999；
- 未调用真实 LLM，未写入 Zotero、Literature 状态、chunk、缓存或任何索引。

## Phase 3：显式审核生命周期（P1）

### 输入

- Phase 1 strict artifacts；
- 当前 review events 与 accepted snapshot；
- 用户对“全量 decision completeness”的决定。

### 工作

1. [x] 将 `pending/accepted/edited/rejected` 作为可查询的 materialized review 状态，同时保持 events 为事实源。
2. [x] 默认要求所有目标得到显式 decision 后才生成 complete accepted snapshot；partial snapshot 必须显式参数并写入独立目录和 manifest。
3. [x] 为 edited material 保存原始 record hash、event、reviewer、timestamp 和最终 materialized hash；evidence 继续不可编辑。
4. [x] 增加 material edit、reject、重复/冲突 event、stale hash、dependency rejection 和重新导入测试。
5. [x] review report 明确展示 pending 数和 accepted dependency exclusion 原因。

### 输出

- review status projection；
- complete/partial snapshot contract；
- 完整状态矩阵测试和操作说明。

### 验收标准

- [x] 任一 accepted derived asset 的全部 evidence 均 accepted；
- [x] edited 输出可回溯原始模型记录与唯一 event；
- [x] pending 不能被误报为 accepted/rejected；
- [x] evidence edit 或 source mismatch 永远 fail closed。

### 影响

- Schema：review/accepted schema 升级；可选择 evidence/material 增加只读 projection 字段。
- 重新处理：无需重新调用 LLM；需重新 materialize 历史 accepted snapshot。
- 索引：现有 candidate 应从新 accepted snapshot 重建；正式索引不变。

### 实施记录（2026-07-18）

实际修改：

- `src/knowledgehub/writing_rag/review.py`：新增 `writing-material-review-status-v1` projection、`writing-material-accepted-v2` complete/partial contract、事件事实源验证、edited audit metadata、dependency exclusion 和完整 snapshot index gate；
- `src/knowledgehub/cli/writing_material.py`：增加显式 `--allow-partial-snapshot`；默认 apply 要求全部目标已有 decision；
- `tests/writing_material/test_extract_review.py`：覆盖 pending projection、partial gate、edited/rejected、依赖拒绝、latest event、重复/冲突/stale decision、幂等重导入、projection 篡改和 CLI flag。

决策与偏差：

- 已采用保守决策：complete accepted snapshot 必须 100% explicit decision。部分审核只写 `accepted-partial/`，manifest 标记 `review_completeness=partial`、pending 数，且 `index_eligible=false`。
- review events 继续使用 append-only v1 作为事实源；没有把可变 review 状态写回 immutable extraction asset。projection 独立保存为 `review-status.jsonl`。
- accepted schema 升级至 v2。旧 accepted snapshot 不会自动迁移或覆盖；需要在安全确认后重新 materialize。
- material edit 后先重新运行 stored material validator，再保存 `reviewed_from_hash`、decision/reviewer/time/reason 和 `materialized_hash`。evidence edit 仍直接拒绝。

验证结果：

- writing-material 专项：43 passed；
- 全仓 pytest：421 passed；
- Ruff：通过；mypy：127 个 source files 通过；
- 未导入或覆盖真实人工审核结果，未重建 candidate/正式索引，未调用真实 LLM。

## Phase 4：增量、选择和恢复能力（P1）

### 输入

- Phase 1 状态机；
- Zotero collection snapshot；
- interrupted run fixture。

### 工作

1. [x] 增加直接 `--document-id` 和显式 `--collection` selection 解析，输出冻结 selection manifest/hash。
2. [x] 实现 interrupted run resume，校验 selection、version bundle、source fingerprint 和 checkpoint commit marker。
3. [x] 定义 stale output：source/schema/taxonomy/prompt/model 变化分别记录原因和需要重跑的阶段。
4. [x] 失败 attempt 不永久跳过；同一 document 的 stage/attempt 可审计且幂等。
5. [x] 增加 CLI dispatch 级 dry-run 和 service resume/retry 自动化测试。

### 输出

- selection resolver；
- resumable stage state；
- stale/reprocess reason report；
- CLI 自动化 E2E。

### 验收标准

- [x] 单文档、小批量、collection selection 均确定且可复现；
- [x] 中断后 resume 不重复写入已提交阶段；
- [x] prompt/model/taxonomy/schema/source 任一变化均得到正确 disposition/stale reason；
- [x] dry-run 不创建 selection、state、cache、run 或 task 记录。

### 影响

- Schema：extraction state DB 和 run manifest 需要迁移/升级。
- 重新处理：按 stale reason 精确重跑；首次迁移不得隐式全量调用 LLM。
- 索引：仅报告 stale candidate，不自动删除或覆盖。

### 实施记录（2026-07-18）

- `provenance.resolve_selection()` 支持 selection JSONL、多个 document ID、collection key/name/path 的确定性并集和 limit，非 dry-run 冻结为带 source/parse/parser fingerprint 的 `selection.jsonl`。
- extraction checkpoint 使用 `writing-material-checkpoint-v1`，manifest 最后提交并记录 selection/evidence/material/failure/review 文件哈希和递增 sequence。
- `--resume-run-id` 只允许恢复 `running` 且没有 `finished_at` 的 run；版本、sections、selection、source 和任一 checkpoint 文件变化均在写入前拒绝。
- state 增加 `version_manifest_json`，attempt 增加 stage/version/output 并以 `(document_id,run_id,stage)` 保证幂等；迁移前只读检查确认现有 state 没有重复 run pair。
- stale report 区分 source、parse、prior failed/partial，以及具体 version component；abstraction-only 变化标记相应重跑阶段，其余进入 classification/provenance。
- CLI 新增 `--document-id`、`--collection`、`--resume-run-id`；dispatch dry-run 测试确认不创建 data root。

验证：writing-material 47 passed；全仓 425 passed；Ruff/mypy 通过。没有迁移真实 state、恢复真实 run、调用 LLM 或写索引。

## Phase 5：正式 Writing release 合并设计与实现（P1/P2）

### 前置门槛

- Phase 1-4 通过；
- 人工审核策略和版权/保留策略已决定；
- pilot accepted snapshot 完整且 source validation 通过。

### 工作

1. [x] 实现从 active Writing release clone 到新物理 candidate 的编排；不得用 scoped pilot 替换 active。
2. [x] 把 accepted writing materials 增量合并到 clone，验证 dense/sparse schema、point count 和 accepted manifest source join。
3. [x] 增加 candidate/active 一致性、snapshot、stage、promotion 和 rollback 流程接口与 fake-backend 回归。
4. [x] promotion 与 build 分离；stage/promotion/rollback 均要求独立显式确认并复验 manifest fingerprint。

### 输出

- release merge module；
- candidate manifest 和完整 validation report；
- stage/promotion/rollback runbook。

### 验收标准

- [x] fake clone 保留原 134 points，新增 3 与 accepted derived assets 一致；
- [x] active collection 在 build/stage 前后保持 134 points；
- [x] merge 输入由 complete accepted-v2 manifest 和 source validation gate 约束；
- [x] snapshot/restore 和 rollback 接口通过 fake 演练；
- [x] 未显式确认时 stage/promotion/rollback 均拒绝。

### 影响

- Schema：可能升级 Writing payload/index processor schema。
- 重新处理：只重建新 candidate；不重跑 Literature 或 LLM extraction。
- 索引：本阶段首次可能影响正式 alias，但只在显式批准的 promotion 步骤。

### 实施记录（2026-07-18）

- 新增 `src/knowledgehub/writing_rag/release.py`：`WritingMaterialReleaseService`、backend/promotion protocols、具体 `QdrantReleaseBackend` 和 `writing-material-release-v1` validated manifest。
- build 顺序固定为 complete review validation → active inspect → snapshot → restore new candidate → clone count/schema validation → accepted merge → final count/schema validation → manifest；build 永不 stage/promotion。
- `src/knowledgehub/cli/writing_material.py` 新增 `release {build,stage,promote,rollback}`；build 从 promotion state 的 `current.active_collection`（或 Hub 配置 fallback）解析物理 active，拒绝用稳定 alias 构造 snapshot URI，再用 Qdrant snapshot/recover 和 `IncrementalChunkIndexer(require_new_collection=False)` 合并 accepted assets；stage/promotion/rollback 各自保留显式 `--yes`。
- 新增 `tests/writing_material/test_release.py`：验证 dry-run 零 mutation、134+3=137、active 不变、错误 merge count/不安全 collection 拒绝、Qdrant adapter 的 snapshot URI，以及 stage/promote/rollback confirmation gate。
- 新增 `docs/writing_material_release_runbook.md`。没有连接本机 Qdrant；仅用 fake client 验证适配器，并对本机已安装 qdrant-client 的 `create_snapshot`/`recover_snapshot` 签名做只读核对。
- 设计偏差：没有机械套用 `CandidateReleaseManager`，因为该 manager 假定“全新 collection + 完整独立 RAG release”；writing-material release 是“clone active + 增量 merge”。最终仍复用 `CollectionPromotionManager` 的 alias transaction 和 rollback，且 manifest 保存 candidate data dir。
- 最终验证包含 Qdrant adapter、release CLI dry-run 和 confirmation gate；见本计划 Phase 6A 后的合并验证结果。

## Phase 6：受控 pilot 与扩量决策（P2）

状态（2026-07-18）：Phase 6A、6A.1-6A.7、Phase 6B.1–6B.1.11 contract hardening/完成性审计已完成；Phase 6B同一30篇selection已以 `classification-v9` + `abstraction-v6`、request-partition-v2（classification≤8 sentences、abstraction初始≤8 evidence、仅token截断时自适应二分至1）、8192-token budget、600秒timeout完成dry-run、zero-failure ready gate和provider-preflight-v2。下一安全门是用户对gate `ecdfedfe...` / version bundle `64338d3b...` 显式批准真实小批extraction。不擅自复用旧approval、导入审核决定或创建Qdrant candidate。

### 输入

- 用户批准的 30-50 篇中英文平衡 selection；
- approved provider/model/secret；
- reviewer 身份、版权、保留期限与访问策略。

### 工作与验收

1. [x] dry-run 输出 Docling/section/provenance gate 计数，并提供严格、只读的评估命令；
2. [x] 提供 run 评估器，报告 exact-span、provider/schema、质量、语言、类别和审核分布；
3. [x] 把 complete accepted snapshot、隔离 candidate、检索/source-join 回归建成顺序 fail-closed gates；检索报告必须由 candidate 查询结果生成，不接受无指纹的手填通过声明；
4. [x] 报告只给出“可由人工决定是否扩量”，永不自动扩量或 promotion；
4a. [x] 将 source-pinned dry-run、ready gate 与后续 extraction 绑定；selection、sections、Literature checkpoint、version bundle 或任一报告指纹漂移均在 LLM/state/run 之前拒绝；
4b. [x] 将机器 ready gate 与显式人工 approval 分离；批准 artifact 绑定 approver/reviewer、rights/retention/access、provider/model 且不包含 secret，不授权索引或自动扩量；
4c. [x] 提供不创建 provider client、不发起网络请求且不输出 endpoint/secret 值的 provider 配置预检；
4d. [x] 将 dry-run/extraction failure policy 与“partial run 禁止 candidate”规则对齐；当前受控 pilot 的 planning、document、exact-span 和 provider failure 均要求为 0；
5. [x] 在获得批准输入后执行 30–50 篇真实 dry-run；
6. [x] 对当前 gate/version bundle 获得新的显式人工 approval 后执行小批真实 extraction；
7. [x] 完成全部人工 decision，生成 complete accepted snapshot；
8. [x] 只构建隔离 candidate，执行检索质量与 source-join 回归；
9. [x] 由用户根据失败率、审核成本和质量决定是否扩量。

### Phase 6A 实施记录（2026-07-18）

- `src/knowledgehub/writing_rag/extract.py`：dry-run 新增 `planning_gates`，明确 provenance 通过/失败、有 section candidate/零 candidate 的文档数；仍在 analyzer 创建和任何 state/cache/run 写入前返回。
- `src/knowledgehub/writing_rag/pilot.py`：新增 `PilotPolicy` 与 `ControlledPilotEvaluator`。默认 selection 为 30–50，provenance 通过率至少 0.80，document/provider failure 各不高于 0.10，exact-span rejection 不高于 0.20；所有阈值均写入报告。
- `src/knowledgehub/writing_rag/pilot.py` 进一步新增 `CandidateRetrievalEvaluator`、`RetrievalPolicy` 和版本化 query/report contract：对实际 candidate hits 计算 recall@k/MRR，逐字段 join accepted asset/evidence provenance，绑定 run/candidate/query/report fingerprint，并复验 policy。
- `src/knowledgehub/writing_rag/review.py`：candidate build 输出升级为 `writing-material-candidate-v1`，绑定 accepted manifest hash/source verification；非 dry-run 原子保存 0600 manifest，dry-run 仍零写入。
- `src/knowledgehub/cli/writing_material.py`：新增 `pilot assess-dry-run`、`pilot evaluate` 和 `pilot evaluate-retrieval`；检索默认 sparse、只读 candidate，可显式将带指纹报告写到临时路径。
- `src/knowledgehub/writing_rag/extract.py`：新增显式配置的 `deterministic_fixture` provider。它无网络/secret/cache，从 authoritative sentence ID 产生本地 derived exact spans，并生成合成 abstraction；不会被生产配置隐式选择。
- `tests/writing_material/test_pilot.py`：覆盖 selection 范围、计数篡改、完整审核、candidate 必须非 dry-run/accepted-only/未 promotion、检索 source join、collection/run/fingerprint/policy gate、人工扩量门和报告不泄露 `original_text`。
- `tests/writing_material/test_extract_review.py`：增加标准 CLI 非 dry-run fixture-provider E2E，使用临时 run/state root 并验证零 LLM cache。
- 默认策略另有 30 文档 mock E2E：fake analyzer 提取 30 文档、生成并导入 120 条显式 decision、materialize complete accepted-v2、fake index 90 条 derived assets、通过 6-query fake retrieval gate；未访问外部服务或创建索引。
- `docs/writing_material_pilot_runbook.md`：记录顺序、安全边界、报告 contract 和停止条件。
- 当前未执行真实 pilot 的原因不是代码缺口，而是本任务明确禁止未批准真实 LLM、全库处理和生产索引修改。Phase 6B 不得标记完成。
- 合并验证结果：`tests/writing_material` 62 passed；全仓 440 passed；Ruff 全仓通过；mypy 129 个 source files 通过；`git diff --check` 通过；CLI help 已确认 `evaluate-retrieval` 及其默认 sparse/显式 output 参数可解析。
- 完成审计：当前计划中仅余 5-9 五个真实操作项；它们分别需要批准 selection、真实 provider/secret、人工 reviewer、隔离 Qdrant candidate 和用户扩量决定。没有可在现有安全授权内继续替代完成的代码项。

### Phase 6A.1 实施记录：dry-run 审批绑定（2026-07-18）

完成性复核发现一项计划外安全偏差：原 dry-run 虽输出 `selection_sha256`，但实际 extraction 没有消费评估报告；审批后若 selection source fingerprint、section、prompt/schema/model version 或 Literature checkpoint 改变，操作员只能人工发现，run manifest 也不能追溯批准了哪份报告。本闭环没有改变 extraction/schema/taxonomy 设计，只加固受控 pilot 的相邻 gate：

- `src/knowledgehub/writing_rag/extract.py`：dry-run 升级为带 `writing-material-extraction-dry-run-v1`、source-pinned selection、sections、Literature checkpoint、version manifest/bundle 和 artifact fingerprint 的只读报告；增加 `pilot_gate_report` 校验，在 analyzer 创建、state 初始化和 run 创建前 fail closed，并把两个报告 fingerprint 保存到 run manifest；resume 保留并校验该 trace；
- `src/knowledgehub/writing_rag/pilot.py`：`assess_dry_run` 严格复验 source report schema/fingerprint、selection fingerprint、sections 和 version bundle，输出带自身 fingerprint 的 `writing-material-pilot-dry-run-v1`；
- `src/knowledgehub/cli/writing_material.py`：增加 `extract --pilot-gate-report`，TaskStore 输入同时记录报告路径与文件 hash；dry-run 或 resume 错误组合在 dispatch 前拒绝；
- `tests/writing_material/test_pilot.py`：覆盖 source report 篡改、gate selection 漂移时零 extraction state/run 写入，以及成功 run manifest 的审批 trace；
- `tests/writing_material/test_extract_review.py`：增加 30 文档、deterministic fixture provider 的标准 CLI dry-run → assess → bound extraction E2E；确认不访问网络、不创建 LLM cache；
- `docs/writing_material_pilot_runbook.md`：实际 extraction 命令现在必须传入保存的 ready gate。

此修正不替代 Phase 6B：fixture provider 只能证明 gate 与流水线组合正确，不能证明真实 provider 的质量。未调用真实 LLM，未处理全库，未创建或修改 Qdrant collection，未导入人工 decision。

验证结果：`tests/writing_material` 64 passed；全仓 442 passed；Ruff 全仓通过；mypy 129 个 source files 通过；`git diff --check` 通过。CLI help 已确认 `extract --pilot-gate-report` 可解析。测试只使用临时 fixture、fake/deterministic provider 和 fake index backend。

### Phase 6A.2 实施记录：显式人工批准 contract（2026-07-18）

Phase 6A.1 的机器 `ready` gate 能证明技术条件，却不能证明“谁批准了什么”。本阶段保留其历史实现记录，但用更严格的当前 contract 取代直接 gate binding：

- `src/knowledgehub/writing_rag/pilot.py`：新增不可覆盖、0600、带 artifact fingerprint 的 `writing-material-pilot-approval-v1`；只有 ready gate、显式 `--yes`、完整 approver/reviewer、版权依据、保留和访问策略才能生成；
- `src/knowledgehub/writing_rag/extract.py`：当前实际输入为 `pilot_approval`，严格 closed-world 验证 approval/gate/source 指纹、selection、sections、Literature checkpoint、version bundle、provider/model 和安全否定项；真实 provider 缺 approval 时在 analyzer/state/run 前拒绝；resume 保留 approval trace；
- `src/knowledgehub/cli/writing_material.py`：新增 `pilot approve-extraction`、`extract --pilot-approval`，并为 extraction dry-run 与 `assess-dry-run` 增加显式 0600 `--output`；机器 gate 不能再直接授权 extraction；
- approval 只声明 configured provider/model execution 获准，`secret_included=false`，不会读取、复制或保存 API key；`production_index_authorized=false`、`automatic_expansion_authorized=false` 固定为保守值；
- `tests/writing_material/test_pilot.py`、`test_extract_review.py`：覆盖缺 `--yes`、stopped gate、不可覆盖输出、selection/provider/model 漂移、真实 provider 缺 approval 的零 state/run 写入，以及 30 文档 CLI fixture 完整组合。

Phase 6A.1 中的 `--pilot-gate-report` 是历史中间实现；当前 CLI 已由 `--pilot-approval` 替代，runbook 已同步，不保留可绕过人工批准的兼容别名。

验证结果：`tests/writing_material` 66 passed；全仓 444 passed；Ruff 全仓通过；mypy 129 个 source files 通过；`git diff --check` 与新增/未跟踪文件尾随空白检查通过。所有 approval/extraction E2E 使用 deterministic fixture 或 injected fake analyzer，未访问外部服务。

### Phase 6A.3 实施记录：无网络 provider preflight（2026-07-18）

- `src/knowledgehub/writing_rag/pilot.py`：新增 `writing-material-provider-preflight-v1`，严格绑定 ready gate、selection 和 version bundle；只报告 provider/model、环境变量名称与 presence、base URL 结构有效性和安全布尔值，不回显 endpoint/API key；
- `src/knowledgehub/cli/writing_material.py`：新增 `pilot preflight-provider --gate-report ... [--output ...]`，显式输出使用 0600；`stopped` 返回非零，便于自动化 fail closed；
- `tests/writing_material/test_pilot.py`：覆盖 base URL 缺失、非法 URL、有效但绝不访问的 `.invalid` URL、API key 缺失、model/version 漂移，以及报告中不出现 endpoint；
- `tests/writing_material/test_extract_review.py`：30 文档 CLI fixture E2E 增加 preflight，验证 provider client 未创建和 0600 输出；
- 预检不探测 endpoint 存活性，因为那会产生网络请求；真实 connectivity 只能在用户再次批准后的受控 extraction 或另行授权的网络 probe 中验证。

验证结果：`tests/writing_material` 67 passed；全仓 445 passed；Ruff 全仓通过；mypy 129 个 source files 通过；diff/尾随空白检查通过。

### Phase 6A.4 实施记录：跨 gate 零失败一致性（2026-07-18）

完成性审计发现 Phase 6A 初始阈值与既定安全决策矛盾：旧 dry-run v1 允许 20% provenance failure，run evaluator 也报告 10% document/provider 和 20% exact-span 上限；但 Phase 1 已明确任何 `partial` run 不得 candidate index，且 extraction manifest 只要存在任一 failure 就是 `partial`。因此旧阈值会把永远无法完成工作项 8 的 run 报为可继续。

- `src/knowledgehub/writing_rag/pilot.py`：`PilotPolicy` 当前默认 document/exact-span/provider failure 上限均为 0，并增加 `require_zero_provenance_failures=true`；dry-run gate 升级为 `writing-material-pilot-dry-run-v2` 和显式 `no_provenance_failures`；
- approval/preflight 只接受与 partial-run index ban 兼容的零失败 policy，不能通过手改 fingerprint 或自定义宽松阈值授权真实 extraction；
- `tests/writing_material/test_pilot.py`：增加“29/30 provenance 仍超过 0.80 但必须 stopped”以及宽松 policy approval 拒绝测试；
- 旧 v1 ready gate 保留为历史 artifact，但当前代码拒绝其 schema，不可生成 approval 或 preflight；runbook 的停止条件已同步为任一 planning/document/exact-span/provider failure。

验证结果：`tests/writing_material` 67 passed；全仓 445 passed；Ruff 全仓通过；mypy 129 个 source files 通过；diff/尾随空白检查通过。

### Phase 6B 准备性只读核对（2026-07-18，未计为完成）

- 现有临时 `papers.jsonl` 共 77 篇，按 Zotero language/title 元数据只能识别出 2 篇中文、75 篇英文，不能满足严格的中英文各半要求；该文件继续保持未批准状态且未修改；
- 对其确定性前 30 篇执行一次不保存报告的 read-only preview：selected 30、provenance passed 29、failed 1、section candidate documents 29、candidate paragraphs 623；失败文档 `zotero:user:18650896:2C7KIX4T:9PX7YE6X:0` 为 `no_provenance_paragraphs`；
- preview selection hash 为 `ff869a9d7bb716d083a1e1f8e52407b882a59fe43de930aabd95ae04922d7788`，但它没有用户批准且语言失衡，因此没有保存为 approved gate，也没有标记工作项 5 完成；
- preview 未调用 analyzer/LLM，未创建 run/state/cache/TaskStore/索引，未读取或修改人工审核结果。下一步需要用户决定接受“2 中文 + 28 英文”的覆盖型 pilot，或提供包含更多中文论文的 selection/collection。

### Phase 6B 工作项 5 初次执行记录（2026-07-18，后被 v2 gate 否决）

- 用户明确批准只执行 dry-run：30 篇、中文英文都有、reviewer `lengmo`、使用说明“MIT 自用”、保留五年；collection 留空，按紧邻上下文采用现有 `papers.jsonl` 的确定性前 30 篇；访问策略暂采用最保守的 `local reviewer only`，真实 extraction approval 前仍可由用户修改；
- 正式 read-only dry-run：selected 30、planned 29、failed 1、candidate paragraphs 623、provenance pass 29/30（0.9667）、section candidate documents 29/29；selection/provenance/section 三项 gate 全部通过，状态 `ready`；
- 唯一失败为 `zotero:user:18650896:2C7KIX4T:9PX7YE6X:0` 的 `no_provenance_paragraphs`；语言元数据仍为 2 中文 + 28 英文覆盖型选择，并非各半；
- source-pinned selection hash `ff869a9d7bb716d083a1e1f8e52407b882a59fe43de930aabd95ae04922d7788`；dry-run artifact fingerprint `1412f1f58269d0e7a747d27a08ebb61a167bf21def73f0c74e7ced571c1ba74b`；ready gate fingerprint `6c298e7c38a513dfa87653f70f15675f108e094a3cab282da9a5732f3b14d065`；
- 0600 报告位于 `/tmp/knowledgehub-writing-material-phase6b-20260718/{dry-run.json,ready-gate.json}`，父目录 0700；两个 artifact fingerprint 和 source binding 已独立重算通过；
- 未调用 analyzer/真实 LLM，未创建 extraction run/state/cache/TaskStore 或索引，未读取/写入人工审核结果。没有生成 `pilot-approval-v1`，因为用户只授权了 dry-run，尚未再次确认 provider/model/secret。

跨 gate 审计后，原 `ready` 结论被撤销：1 个 provenance failure 会产生 `partial` run，而 partial 禁止 candidate。原 `/tmp/.../ready-gate.json` 为 v1 历史证据，不再是可消费 gate；同一 dry-run 经 v2 复验写入 `rejected-gate-v2.json`，`no_provenance_failures=false`、状态 `stopped`。

### Phase 6B 工作项 5 修正执行记录（2026-07-18，当前有效）

- 在用户已批准的“30 篇、中文英文都有”范围内，排除无 provenance 的 `zotero:user:18650896:2C7KIX4T:9PX7YE6X:0`，按确定性顺序补入 `zotero:user:18650896:2G7LBWUZ:EV7QEGFN:0`；冻结 selection 为 1 篇中文 + 29 篇英文，仍满足“都有”；
- 0600 source-pinned selection：`/tmp/knowledgehub-writing-material-phase6b-20260718/approved-selection-v2.jsonl`；selected 30，selection hash `1e3672ee486be9fe3d0eccbabbfe6ca9fe0eac6d86cc1f19fe050b33c54879bf`；
- v2 dry-run：planned 30、failed 0、provenance 30/30、section candidate documents 30、candidate paragraphs 635，artifact fingerprint `23dcb22ce67cb2fb2c462d91972c4518355087bc39c1930628e4bbfba4e8fd12`；
- `writing-material-pilot-dry-run-v2` gate：selection/provenance/no-provenance-failures/section 四项全部通过，status `ready`，artifact fingerprint `0cca164547e777dcdcfca3680dabe26a7b07700bf801a232b86eb0377e5d7e64`；
- 当前有效文件为 `{approved-selection-v2.jsonl,dry-run-v2.json,ready-gate-v2.json}`；旧 v1 文件只保留审计历史。全过程没有 analyzer/LLM、run/state/cache/TaskStore/索引或审核写入。

### Phase 6B provider 准备状态（2026-07-18，未授权 extraction）

- 对上述 ready gate 执行无网络 preflight：provider `openai_compatible` 与 model `qwen3-32b-awq` 已配置且 version bundle 一致；
- `KH_WRITING_MATERIAL_LLM_BASE_URL` 未设置，因此状态 `stopped`；`KH_WRITING_MATERIAL_LLM_API_KEY` 也未设置，但当前 OpenAI-compatible client 明确允许本地无鉴权 endpoint，不能据此假定远端不需要 key；
- 仓库内没有真实 endpoint 配置，只记录了环境变量名称；进程只读筛选没有发现 vLLM/Qwen served-model 进程。Docker API 只读检查因本机权限拒绝，未提权重试；未发起 HTTP 请求；
- 当前 0600 报告 `/tmp/knowledgehub-writing-material-phase6b-20260718/provider-preflight-v2.json`，绑定当前 v2 gate，artifact fingerprint `f35b85262a0ffab731bdce42f115899764a4d7818ccec44f685a9c21f5e4f6b0`；`network_request_performed=false`、`provider_client_created=false`、`secret_values_emitted=false`；旧 preflight 只绑定已失效 v1 gate；
- 工作项 6 继续保持未完成。所需外部输入是 endpoint URL；如 endpoint 要求鉴权，还需由用户在本机设置 API key，不能把 secret 发到聊天或写入 YAML。

### Phase 6B provider 就绪记录（2026-07-18，仍未授权 extraction）

- 用户明确批准仅启动本地 provider，不执行 extraction、不发送 LLM 生成请求。宿主机两张 RTX 3090（各 24 GiB）、NVIDIA driver `580.159.03` 和 Docker NVIDIA runtime 经只读核对可用。
- 复用已缓存的 `vllm/vllm-openai:latest` image，实际 label 为 `v0.25.1`、image digest `sha256:f26809eb13339cbc59c3d0cc972f8c4997830dc8d2121cf18089cb122834e10d`；模型使用已完整下载的 Hugging Face snapshot `0499c3ac83fdef8810b907a23894ba91e95eddd8`（约 19 GiB），以 read-only volume 挂载且启用 offline 环境变量。
- 初次按 vLLM 旧 CLI 参数启动因 `--disable-log-requests` 已移除而 fail-fast；修正为 0.25.1 位置 model 参数后，0.90 GPU utilization 在 CUDA graph capture 阶段 OOM。两个失败容器都未 ready、未收到请求，删除后以 `--enforce-eager --gpu-memory-utilization 0.80 --disable-custom-all-reduce` 安全重建。
- 当前容器 `knowledgehub-writing-vllm` 只映射 `127.0.0.1:8000->8000`，tensor parallel 2、max model length 16384、served model `qwen3-32b-awq`、request logging 默认关闭；每卡权重约 9.15 GiB，KV cache 约 9.42 GiB，16K 上下文估算并发 4.71。
- 只读 `GET /v1/models` 返回 `qwen3-32b-awq` 和 `max_model_len=16384`，没有调用 chat/completions。以 `KH_WRITING_MATERIAL_LLM_BASE_URL=http://127.0.0.1:8000/v1` 重跑无网络 preflight 为 `ready`，新 0600 报告 `/tmp/knowledgehub-writing-material-phase6b-20260718/provider-preflight-ready.json`，artifact fingerprint `5994cdce283443bb644165d1a5c254658fc00c5a5ff8a881cdc1f55ea09b8a0d`。
- provider endpoint 缺失的外部阻塞已解除；工作项 6 仍未完成，因为尚未生成 `writing-material-pilot-approval-v1`，也未获得对真实小批 extraction 的独立明确授权。

### Phase 6A.5 实施记录：真实 provider 边界收口（2026-07-18）

- 用户随后明确批准当时 version bundle 的 30 篇真实 extraction，并生成 0600 `pilot-approval-v1`，artifact fingerprint `e1a51ef356b1f4dacc8d35c222cd2bdd5c89feae11207eb3a6131567d93c346b`；该 approval 固定 reviewer/approver `lengmo`、`private research use`、保留五年、`local reviewer only`，且明确不授权生产索引或自动扩量。
- 首次 run `20260718T055343Z-e1430a9e3f21` 因环境变量错误包含 `/v1`，adapter 实际请求 `/v1/v1/chat/completions`，30/30 均得到 404；run 正确结束为 `partial`、0 evidence，未写索引。这证明旧 preflight-v1 的“任意带主机 URL”检查不足。
- 使用正确 origin 重试的 run `20260718T055530Z-e034b05e0adc` 完成首文档 checkpoint：12 evidence、4 strategy、4 template、4 phrase，但同时拒绝 9 个 offset/text 不一致 span，document 为 `partial`。按既定零失败停止条件人工中断，manifest 保留 `running`、checkpoint sequence 2；第二文档未 checkpoint 的中间结果未进入 evidence/material。
- 只读诊断不回显原文：首文档 classification 共 21 个 span，12 个 exact，9 个拒绝；8 个拒绝文本在源 paragraph 中只出现一次但模型 offset 错误，1 个文本完全不存在。没有使用 exact search 或 fuzzy matching 修正/接受它们。
- `src/knowledgehub/writing_rag/extract.py` 新增 provider-origin closed validation，client 创建前拒绝 credentials/path/query/fragment；带 approval 的 controlled pilot 在首个 partial/failed document 后自动结束，不再继续消耗剩余批次。
- classification 升级为 `classification-v2` 和 `writing-material-prompts-v2`；`configs/writing/prompts/classify-v2.md` 要求使用 payload 已有的 authoritative `sentences` offsets，每个 item 只返回一个连续 span、不穷举切分 paragraph。新 provider JSON Schema 与 Python validator 均强制 `maxItems=1`；旧 `classification-v1` stored evidence 继续可读，仍由各自 run manifest 约束版本一致性。
- `src/knowledgehub/writing_rag/pilot.py` 升级 `writing-material-provider-preflight-v2`，与 runtime client 共用 origin 规则；`http://host/v1` 现在在创建 client 或真实写入前 fail closed，且报告仍不输出 endpoint/secret。
- 新增/更新测试覆盖 origin path/credentials/query/fragment 拒绝、sentence offset payload、classification-v2 单 span、旧 v1 evidence 可读、approval version bundle 漂移零写入拒绝，以及 approved pilot 首 partial 文档 fail-fast；writing-material 专项 73 passed，全仓 451 passed，Ruff 全仓、mypy 129 个 source files 和 `git diff --check` 通过。
- 同一 30 篇 selection 的新 dry-run 仍为 30/30 provenance、0 failure、635 candidate paragraphs；`classification-v2` dry-run fingerprint `68b7e03498290bdb21a2cb4a9a7a90332f60b258ef3a5d7be5815572ca494b15`，ready gate fingerprint `1b19c092a18a832781a8d3cbf6ae116fdcec6151d75df79e869641f95afedbe3`，provider-preflight-v2 fingerprint `9ea94593185e65dc0aa71ef1fd3a896935b01b6c2e1ee64803e7bd6818149ae0`，version bundle `39ec2800efa9aca06812b81a6be6e517d24fbb2c85a0f0af1f3667010fbe4a99`。三份 0600 产物位于 `/tmp/knowledgehub-writing-material-phase6b-20260718/{dry-run-classification-v2.json,ready-gate-classification-v2.json,provider-preflight-classification-v2.json}`。
- 旧 approval 绑定 version bundle `a188...` 并已自动失效；工作项 6 仍未完成，新 `classification-v2` gate 必须再次获得明确人工批准后才能发送下一个真实生成请求。
- 使用当前 30 篇真实 selection 和旧 0600 approval 做了额外的只读失效验收：完整解析 selection 后准确返回 `pilot approval version bundle differs from the current extraction`；注入的 sentinel analyzer 未被调用，`runs/state/cache` 全部文件的路径、大小和 mtime 前后相同。该验收没有创建 provider client、run、state 或 cache，证明旧 approval 不能越过新版 gate。

### Phase 6A.6 实施记录：sentence-ID-only classification（2026-07-18）

- 用户明确批准 `classification-v2` 新 gate 的真实30篇 extraction；生成不可覆盖的 0600 approval `/tmp/knowledgehub-writing-material-phase6b-20260718/pilot-approval-classification-v2.json`，fingerprint `7a993ac5b186c4a8cef770e8e616e0de7b9dc7bcfbe07e244d06385448441e0f`，绑定 reviewer/approver `lengmo`、`private research use`、`local reviewer only`、保留五年，并继续明确禁止生产索引和自动扩量。
- 真实 run `20260718T070515Z-6dd7b1ac9063` 使用正确 provider origin 和 `qwen3-32b-awq`；首篇 classification 返回9个结构合法的单 span item，其中5个 exact、4个复制了源 paragraph 中唯一存在的文本但使用错误 offset。4个 item 全部以 `exact_span_rejected` 拒绝，未搜索修正、未 fuzzy accept、未把模型文本保存为 evidence。
- approved-pilot fail-fast 正常工作：run 在首篇 checkpoint 后自动结束为 `partial`，processed=1、failed=1、evidence=5、strategy/template/phrase 各1，剩余29篇没有继续发送请求。`writing-material validate` 返回 `source_verified=true`、errors=[]、`index_eligible=false`；pilot evaluator 的 exact-span rejection rate 为 4/9，recommendation 为 `stop_and_fix_extraction_contract`。没有创建 candidate/生产索引，没有导入或覆盖 review decision。
- 根因证明 `classification-v2` 的“让模型照抄 authoritative offsets”仍不是可靠 provenance contract。因此 `src/knowledgehub/writing_rag/materials.py` 升级为 `classification-v3`：provider response 只含1–8个 sentence ID；`resolve_sentence_selection()` 拒绝未知、重复、重排和不连续 ID，并从 immutable paragraph 确定性派生 start/end/original_text。模型输出 schema 中已不存在 `spans`、offset 或 `original_text`。
- `src/knowledgehub/writing_rag/extract.py` 与 `configs/writing/prompts/classify-v3.md` 只向模型提供 authoritative sentence ID/text；本地 derived span 仍必须通过完整 `validate_exact_span()`、source segment/page/bbox coverage 和 source revalidation。deterministic fixture 与 OpenAI-compatible JSON Schema 同步升级。
- stored evidence/run 的兼容边界扩展为 classification-v1/v2/v3；新请求只生成 v3，历史 v2 partial run 已在 v3 代码下重新通过 source validation，避免 schema 升级使 immutable audit artifacts 不可读。
- 测试新增 sentence ID 重复/未知/重排拒绝、source-only span 派生、provider payload 不含 paragraph offsets、v1/v2/v3 historical manifest 可读，以及现有 fail-fast/partial/fixture 闭环回归。`pytest -q tests/writing_material` 为77 passed，全仓 `pytest -q` 为455 passed，Ruff 全仓、mypy 129 source files 和 `git diff --check` 通过。
- 同一30篇 selection 的 v3 dry-run 仍为30/30 provenance、0 failure、635 candidate paragraphs。0600 工件为 `/tmp/knowledgehub-writing-material-phase6b-20260718/{dry-run-classification-v3.json,ready-gate-classification-v3.json,provider-preflight-classification-v3.json}`，fingerprints 依次为 `951a83c653ee52d34010f5a70bd088b3e3d7b5253643fdc7877403307068c09b`、`7a8d819454a88bfde1ef58ee0f25e71272cfb2bb54d560fe29ea8747a993a1e8`、`b5f1ac11c1d92f62d55e38208f3417297cc2721a0d0ce297f744a2c2d863ca1d`；version bundle 为 `a2e3d1a050e730348010e9c4fd9fc516a9b45fac37d7a34e9eadee206443b866`，preflight 未创建 client、未发网络请求。
- v2 approval 只授权 version bundle `39ec...`，已自动失效；以当前真实30篇 selection 做零写入验收时准确返回 version bundle mismatch，sentinel provider 未调用，`runs/state/cache` 元数据前后相同。工作项6仍未完成，任何 v3 真实请求都必须重新获得绑定 `7a8d...` gate 的明确人工批准。

### Phase 6A.7 实施记录：动态 enum 单句选择（2026-07-18）

- 用户明确批准 `classification-v3` gate 的真实30篇 extraction；生成0600 approval `/tmp/knowledgehub-writing-material-phase6b-20260718/pilot-approval-classification-v3.json`，fingerprint `f11af58a86ae10c894c296f09f4345a5d1fef704c3e428596bca9ceda66f9214`，绑定 reviewer/approver `lengmo`、`private research use`、`local reviewer only`、保留五年，且继续禁止生产索引与自动扩量。
- 真实 run `20260718T073402Z-6a1899147376` 在首篇执行1次 classification 和1次 abstraction。classification 返回12个 item：9个 sentence selection 成功由本地 source 派生并形成9条 validated evidence；3个拒绝中，1个引用不存在于源 paragraph 的 sentence ID，2个 sentence ID 组合非源顺序连续。没有保存错误 item、没有搜索修正或 fuzzy acceptance。
- approved-pilot fail-fast 再次按策略工作：run 在首篇结束为 `partial`，processed=1、failed=1、evidence=9、strategy/template/phrase 各3，剩余29篇未请求。source validation 返回 errors=[]、`source_verified=true`、`index_eligible=false`；pilot exact-span rejection rate 为3/12=0.25，recommendation 为 `stop_and_fix_extraction_contract`。没有 accepted snapshot、review event、candidate 或生产索引写入。
- `classification-v4` 将 provider 输出进一步收紧为单个 `sentence_id`，不再输出 paragraph ID 或 sentence ID 数组。`OpenAICompatibleAnalyzer.classify()` 为每个真实 batch 动态构造允许 sentence IDs 的 JSON Schema enum；vLLM 受约束解码无法生成未知 ID，也无法产生重排/非连续组合。本地 lookup 将所选 ID 唯一 join 回 paragraph，再沿用 `resolve_sentence_selection()` 和 `validate_exact_span()`。
- 新增 `configs/writing/prompts/classify-v4.md` 并更新生产/fixture 配置；stored evidence/run 兼容集合扩展为 classification-v1/v2/v3/v4。v3 partial run 已在 v4 代码下重新验证为 source-verified，历史 immutable artifacts 保持可读。
- 测试覆盖动态 enum 必须等于 batch authoritative IDs、provider response 不含 paragraph/text/offset、batch 外 ID 拒绝、本地单句 span 派生、historical v1-v4 manifest 可读、低质量 partial 与 approved-pilot fail-fast。`pytest -q tests/writing_material` 为78 passed，全仓 `pytest -q` 为456 passed，Ruff 全仓、mypy 129 source files 和 `git diff --check` 通过。
- 同一30篇 selection 的 v4 dry-run 为30/30 provenance、0 failure、635 candidate paragraphs。0600 工件 `/tmp/knowledgehub-writing-material-phase6b-20260718/{dry-run-classification-v4.json,ready-gate-classification-v4.json,provider-preflight-classification-v4.json}` fingerprints 依次为 `45b4f19303640da33b0afc0b67414989f6d207bbd1d1b92e10805e288f113947`、`f6aaa3f6b40185632439fd1c1eb8c2c14514856e2cd1597924673b5a3f414d83`、`0be7c0a4e26afca9b3f07bab0e3aaefb0843b0d30432c79e5f480a612fda5d6b`；version bundle 为 `52ea98eb01cb882a0e43d3e37ac67b56b70c7b06235577e511aeae857bd32da0`，preflight 未创建 provider client、未发网络请求。
- v3 approval 绑定旧 bundle `a2e3...`，不能驱动 v4。工作项6仍未完成，v4 真实 extraction 必须重新获得绑定 `f6aaa3...` ready gate 的明确人工批准。

### Phase 6B.1 实施记录：v4 真实验证与 abstraction-v2 收口（2026-07-18）

- 用户明确批准 `classification-v4` 的真实30篇 extraction。生成0600 approval `/tmp/knowledgehub-writing-material-phase6b-20260718/pilot-approval-classification-v4.json`，fingerprint `6fea04ae8a30202963766a19f3154e46fef2fd0a4ca0d7fb912a0c3452e05e9d`，绑定30篇 selection、gate `f6aaa3f6b40185632439fd1c1eb8c2c14514856e2cd1597924673b5a3f414d83`、reviewer/approver `lengmo`、`private research use`、`local reviewer only` 和保留五年；production index 与自动扩量继续禁止。
- 真实 run `20260718T075944Z-44d9478a69b7` 的首篇成功提交17条 exact/source-derived evidence、1 strategy、1 template、3 phrases，并继续处理第2篇；因此此前“只验证一个即结束”不是固定行为。第2篇 classification 也成功新增16条 evidence，但 abstraction 返回重复 `risk_flags`，严格 validator 以 `MaterialValidationError: duplicate risk_flags` 拒绝，run 按零文档失败策略结束为 `partial`：processed=1、failed=1、evidence=33、strategy/template/phrase=1/1/3，剩余28篇未请求。
- `knowledgehub writing-material validate --run-id 20260718T075944Z-44d9478a69b7` 返回 `source_verified=true`、errors=[]、`index_eligible=false`、pending=38。run 只有0600 extraction artifacts；未生成 accepted snapshot、review events 或 candidate/生产索引。真实执行写入该独立 run、增量 state checkpoint 和3个0600 LLM cache 文件，不删除或覆盖既有 run/cache。
- 根因不在 provenance、exact-span 或 `classification-v4`，而在 vLLM/xgrammar 不支持 JSON Schema `uniqueItems`，旧 `abstraction-v1` 只能在生成后拒绝重复风险数组。`src/knowledgehub/writing_rag/{materials,extract}.py` 将新请求升级为 `abstraction-v2`：strategy provider 输出使用包含全部5个已知风险键的 closed boolean `risk_flag_decisions` 对象，本地只把值为 true 的键派生为 immutable material `risk_flags`。缺键、额外键、非布尔值和旧数组形态均拒绝；不通过去重静默修正模型输出。
- 新增 `configs/writing/prompts/abstract-v2.md`，更新生产配置与默认配置，prompt bundle 升为 `writing-material-prompts-v5`。stored material/review 对历史 `abstraction-v1/v2` 均只读兼容；新请求只生成 v2。实际变更文件为 `src/knowledgehub/writing_rag/{materials,extract,review}.py`、`src/knowledgehub/hub/config.py`、`configs/writing_materials.yaml`、`configs/writing/prompts/abstract-v2.md` 和对应 tests/docs。
- 测试覆盖 fixed boolean schema、true flag 确定性转换、缺少风险键拒绝、fixture E2E、历史 abstraction-v1 manifest 可读，以及既有非法输出/provenance/exact-span/fail-fast/index 隔离回归。`pytest -q tests/writing_material` 为78 passed；全仓 `pytest -q` 为456 passed；Ruff 全仓、mypy 129 source files、`git diff --check` 均通过。
- 同一30篇 selection 的新 dry-run 仍为30/30 provenance、0 failure、635 candidate paragraphs。0600 工件为 `/tmp/knowledgehub-writing-material-phase6b-20260718/{dry-run-classification-v4-abstraction-v2.json,ready-gate-classification-v4-abstraction-v2.json,provider-preflight-classification-v4-abstraction-v2.json}`，fingerprints 依次为 `a2609563eef17bdbc82884dee898d13fa0d844aae3149d44b7532861b048e536`、`1942a4fa82a7e695b6174b03c1ddd7c169b7be0277d8a12e5d1936bd5da3234e`、`ef54836788df1c970e11246d8f6fe864b64eee4b66e0af8f82424575e975523a`；version bundle 为 `5ea72501df5a5cd14564704bfbe0be4b9c0bdda99f0d26040b4c56eecc5b4279`。preflight 未创建 provider client、未发网络请求。
- Phase 6B.1 的“真实发现 → fail closed → 最小 contract 修复 → 测试 → 新门禁”闭环已完成。当时形成的 `1942a4...` gate 随后被 classification-v5 contract 取代，仅保留为历史证据；不得复用 approval `6fea04...`，也不得在 extraction 全部成功和人工审核完成前创建索引。
- 追加真实 CLI 零写入验收发现一项调用顺序偏差：service 已在 state/run/cache 前验证 approval，但 CLI 原先先进入 `TaskExecutor.execute()`，导致失效 approval 在返回 version mismatch 前尝试创建 TaskStore audit row。受限沙箱阻止了该写入并暴露 `attempt to write a readonly database`；实际 runs/cache/state 指纹未变化。
- `WritingMaterialExtractionService.validate_execution_authorization()` 现以只读方式在 CLI TaskStore 之前验证 provider 配置/origin、selection、sections、Literature checkpoint、provider/model、version bundle 和 approval fingerprint；`extract()` 在持久化边界仍重复验证，避免把 CLI 预检当作唯一信任边界。新增 CLI 回归测试断言 drifted approval 返回 version mismatch 且 task-state/data root 均不存在。
- 使用当前30篇 selection、旧 v4 approval 与无服务 endpoint `127.0.0.1:9` 做真实 CLI 复验，准确在授权层退出，未发 LLM 请求。执行前后 runs 指纹均为 `f68695f8cfd9a16c8acf48f1fc505f7a13decd68d271d5234fadb23febf401de`、cache 指纹均为 `aad62be5dc378b213a8a4592a454dc9beead4084b0b04cebf6abb85402a1ac10`、state DB SHA256 均为 `70ea953a6e32462ebad6c9e2a36b5a9005e7f38ba15f3368f8238fba8b844ea7`；run/cache 文件数仍为9/303。

### Phase 6B.1.1 实施记录：classification 风险映射闭合（2026-07-18）

- 完成性反查发现 `classification-v4` 虽已把 provenance selection 收紧为动态 enum 的单 `sentence_id`，但风险判断仍使用 `risk_flags` 数组；vLLM/xgrammar 同样无法用 `uniqueItems` 保证该数组无重复。前两篇未触发不代表剩余28篇安全，继续等待真实运行暴露会重复消耗并触发零失败停止。
- `classification-v5` 保留 v4 的全部 provenance/exact-span 约束：provider 只能从当前 batch 动态 enum 选择一个 authoritative sentence ID，不输出 paragraph/text/offset/normalized text；本地唯一 join 后确定性派生 source text、offset 和 provenance。唯一 contract 变化是每项改用包含全部5个风险键的 closed boolean `risk_flag_decisions`，本地只将 true 键派生为 immutable evidence `risk_flags`。
- `src/knowledgehub/writing_rag/{materials,extract}.py`、`configs/writing/prompts/classify-v5.md`、`configs/writing_materials.yaml` 与默认 Hub config 已升级，prompt bundle 为 `writing-material-prompts-v6`。缺键、额外键、非布尔值和旧数组形态全部拒绝；stored evidence/review 继续兼容 classification-v1–v5，旧 immutable run 不改写。
- 专项测试验证 classification/abstraction schema 均不使用 xgrammar 不支持的 `uniqueItems`，两个风险对象都要求完整闭合布尔 properties；另覆盖缺风险键拒绝、v1–v5 历史 manifest/evidence 可读、单 sentence-ID exact-span 与既有 fail-fast。`pytest -q tests/writing_material` 为80 passed，全仓 `pytest -q` 为458 passed，Ruff 全仓、mypy 129 source files 和 `git diff --check` 通过。
- 同一30篇 selection 的 dry-run 为30/30 provenance、0 failure、635 candidate paragraphs。0600 工件为 `/tmp/knowledgehub-writing-material-phase6b-20260718/{dry-run-classification-v5-abstraction-v2.json,ready-gate-classification-v5-abstraction-v2.json,provider-preflight-classification-v5-abstraction-v2.json}`，fingerprints 依次为 `82528874a2e979f8991fba4c3cf3f8b47b63dbd7d1c1be58988e409e71e608e7`、`21254ca9a557fa33e9f936270956bd4b22899f5e5fd0fb97a81a8172e86c1742`、`d3ad3ee1a6000c5a21c01764b46850db9927bda4360a5e70e71445fbfa985627`；version bundle 为 `d84ac00b1a1efd5a1c4275ac0a5fb60dce2106a260c78f9e37d4a7006882c65d`，preflight 未创建 provider client、未发网络请求。
- 当时形成的 gate `21254ca9...` 与 bundle `d84ac00b...` 随后被 v6/v3 引用语义 contract 取代，仅保留历史；先前所有 gate/approval 均已失效。本阶段未调用真实 LLM、未创建 run/state/cache、未处理全库、未修改审核结果或任何索引。

### Phase 6B.1.2 实施记录：结构化引用与语义唯一性（2026-07-18）

- 完成性审计发现 provider schema 与本地 durable trust boundary 仍有三处宽窄不一致：同一 sentence/category 的重复 classification item 会到后续 evidence 去重才被静默折叠；abstraction `evidence_ids` 只约束字符串而未动态限定当前 batch；重复 evidence reference 在创建时可通过、到 stored material validator 才失败。material category 也可能与其引用的全部 evidence 类别无关。
- `classification-v6` 在保留动态 enum 单 sentence-ID、固定布尔风险映射和全部 exact-span/provenance 规则的基础上，立即拒绝重复 sentence/category pair，不再依赖后续 `_unique_records()` 决定保留哪条冲突输出。
- `abstraction-v3` 为每次真实请求动态构造当前 batch evidence-ID enum；parser 拒绝未知/重复 evidence reference，并要求 strategy/template/phrase 的 category 至少由一条被引用 evidence 支持。多 evidence 关联能力保持不变，不把 contract 缩减为单 evidence，也不静默删除引用。
- 新增 `configs/writing/prompts/{classify-v6,abstract-v3}.md`，生产/默认配置同步更新，prompt bundle 为 `writing-material-prompts-v7`。stored evidence/material/review 继续兼容 classification-v1–v6 和 abstraction-v1–v3，新请求只生成 v6/v3；历史 immutable run 不改写。
- 测试新增 classification 重复 pair 拒绝、abstraction 动态 evidence enum、重复 reference 拒绝、unsupported category 拒绝，以及 v1–v6 × abstraction-v1–v3 历史 manifest 兼容矩阵。`pytest -q tests/writing_material` 为94 passed，全仓 `pytest -q` 为472 passed，Ruff 全仓、mypy 129 source files、`git diff --check` 均通过。
- 同一30篇 selection 的 dry-run 为30/30 provenance、0 failure、635 candidate paragraphs。0600 工件为 `/tmp/knowledgehub-writing-material-phase6b-20260718/{dry-run-classification-v6-abstraction-v3.json,ready-gate-classification-v6-abstraction-v3.json,provider-preflight-classification-v6-abstraction-v3.json}`，fingerprints 依次为 `a7e64d803d84f729115331442cbb23bc0e42d50dc7e8355bf0fbd0b2a1bd79c5`、`fae08de2b66d20138aff4d0ae052319e9f0f8c145dace5a1faddceb774e755a6`、`317c0e5a9d79e5164d78f0b4d337611a688062286d87b6f755d6f5a338c3721e`；version bundle 为 `fecdba449ea7581c3b755ff9e54fa8b99f56a8c0ca47b452fa54bbd8089f200b`，preflight 未创建 provider client、未发网络请求。
- 当时形成的 gate `fae08de2...` 与 bundle `fecdba44...` 随后被 abstraction-v4 精确 schema contract 取代，仅保留历史。本阶段未调用真实 LLM、未创建 extraction run/state/cache、未处理全库、未修改审核结果或任何索引。

### Phase 6B.1.3 实施记录：provider/parser 精确同构与重复 material 拒绝（2026-07-18）

- 逐字段完成性比较发现 `abstraction-v3` provider JSON Schema 仍统一允许最长2000字符、20项×500字符，而 parser/stored validator 对 label、steps、applicability、slot name/type、function、position、register 等使用160、12×300、1000、80/120、300、120等更严格上限。这会让受约束解码产生“provider schema 合法、应用 validator 拒绝”的响应。
- `abstraction-v4` 的 provider schema 现逐字段与 parser 上限精确一致；category enum 也动态收窄为当前 evidence batch 实际类别。动态 evidence-ID enum、固定风险映射、多 evidence 引用和 category-reference 规则保持不变。
- `parse_abstraction_response()` 现在对生成后的 strategy/template/phrase identity 做集合验证，重复 payload 直接拒绝，不再让 checkpoint `_unique_records()` 静默选择一个。`WritingMaterialReviewService.validate()` 从磁盘重读 durable assets 时也重验 material category 是否由引用 evidence 支持，防止 run JSONL 被篡改后绕过创建时验证。
- 新增 `configs/writing/prompts/abstract-v4.md` 并更新生产/默认配置，prompt bundle 为 `writing-material-prompts-v8`；stored/review 兼容 abstraction-v1–v4，新请求只生成 v4。专项测试覆盖每个关键 provider maxLength/maxItems、动态 category enum、重复 strategy payload 拒绝、stored category-reference drift 拒绝，以及 classification-v1–v6 × abstraction-v1–v4 历史兼容矩阵。
- `pytest -q tests/writing_material` 为101 passed，全仓 `pytest -q` 为479 passed，Ruff 全仓、mypy 129 source files 和 `git diff --check` 均通过。历史真实 partial run 在新代码下仍返回 `source_verified=true`、errors=[]、`index_eligible=false`。
- 同一30篇 selection 的 dry-run 为30/30 provenance、0 failure、635 candidate paragraphs。0600 工件为 `/tmp/knowledgehub-writing-material-phase6b-20260718/{dry-run-classification-v6-abstraction-v4.json,ready-gate-classification-v6-abstraction-v4.json,provider-preflight-classification-v6-abstraction-v4.json}`，fingerprints 依次为 `3f566813892ed105a54e3cae63d422665b36d1e22832d43a34d2fa817dc24599`、`6bfbb49ab8f15bcd8742a46b5ea5114bb05aad97f93b143b0af46c88a2fa9e13`、`cb34fdd1f4e7b2e91203bd34c49f06a1e4610f7853dd6ed8e757385f5533b9d8`；version bundle 为 `f57e0e5d3e6fcce8a10fddd7bb46c87d46b0337d551fa596000f1e3000b2938f`，preflight 未创建 provider client、未发网络请求。
- Phase 6B.2 的当前外部门是对 gate `6bfbb49a...` 与 bundle `f57e0e5d...` 的显式人工授权。本阶段未调用真实 LLM、未创建 extraction run/state/cache、未处理全库、未修改审核结果或任何索引。

### Phase 6B.1.4 实施记录：当前态逐要求完成性审计（2026-07-18）

- 重新读取计划所有 checkbox、收集101个 writing-material 测试节点、核心公开/内部符号、当前30篇 gate 和真实 partial run，不以“未发现 TODO”代替正向证据。Phase 1–5 与 Phase 6工作项1–5均有实现和专项测试；源码范围没有未处理的 TODO/FIXME/NotImplemented/placeholder 分支。
- 当前 `/data` 指纹仍为 runs `f68695f8cfd9a16c8acf48f1fc505f7a13decd68d271d5234fadb23febf401de`、cache `aad62be5dc378b213a8a4592a454dc9beead4084b0b04cebf6abb85402a1ac10`、state DB `70ea953a6e32462ebad6c9e2a36b5a9005e7f38ba15f3368f8238fba8b844ea7`；最新真实 partial run只有0600 extraction/review markdown资产，没有 review-events、accepted、candidate 或 index artifact。
- 当前 selection 仍为30行，ready gate/bundle 为 `6bfbb49a...` / `f57e0e5d...`，报告明确 `real_llm_called=false`、`writes_performed=false`。历史 v4 partial run仍为 processed1/failed1、33 evidence、1/1/3 materials，source verified、不可索引。
- 当前态完成性矩阵已写入 `docs/writing_material_extraction_implementation_audit.md` 第0节：内部实现和 fixture/只读运行证据均为 `INTERNAL_VERIFIED`；仅 Phase 6工作项6–9为 `EXTERNAL_PENDING`。相关实现仍是 `WORKTREE_ONLY`，未获得 Git commit/发布授权。
- 不把目标缩减为“代码测试通过”：工作项6必须有当前 gate的新 approval并实际完成30篇零失败 extraction；工作项7必须由 reviewer 完成全部 decision；工作项8只能在 complete accepted snapshot 后构建隔离 candidate；工作项9由用户决定。当前“不调用真实 LLM、不修改人工审核结果或生产索引”的安全边界禁止自动完成这些事项。

### Phase 6B.1.5 实施记录：source-eligible sentence 与结构唯一分类（2026-07-18）

- 用户解除真实 LLM 限制并批准 gate `6bfbb49a...` / bundle `f57e0e5d...`。生成0600 approval `/tmp/knowledgehub-writing-material-phase6b-20260718/pilot-approval-classification-v6-abstraction-v4.json`，fingerprint `fc2add0b99903f1173c88057084c6b5618080e8b56ac2a8134c46258051e9acf`；provider origin 经 `/v1/models` 验证为 `http://127.0.0.1:8000`，模型为 `qwen3-32b-awq`。
- 真实 run `20260718T090435Z-122ccc118171` 前2篇成功，第三篇 fail-fast：`processed=2`、`failed=1`、78 evidence、2 strategy、2 template、2 phrase。一个选择句的 source range 跨 provenance segment gap，被 `validate_exact_span()` 以 `span is not completely covered by source provenance` 拒绝；同一 batch 另返回重复 sentence/category pair，被 v6 parser 拒绝。run source validation 为 `errors=[]`、`source_verified=true`、`index_eligible=false`、pending=84；剩余27篇未请求，没有 accepted/review-events/candidate/生产索引写入。
- `classification-v7` 将 provider 输出改为动态闭合的 `sentence_id -> category -> decision` 对象；sentence/category 在 JSON 对象结构中唯一，旧可重复 item 数组不再是合法输入。provider 内容使用 duplicate-key-aware JSON decoder，重复 object key 直接拒绝；不会通过去重静默保留一个结果。
- `_eligible_classification_sentences()` 在 schema/payload/candidate 边界只暴露 range、page、bbox 和 coverage 完整的 authoritative sentence。模型仍不输出正文/offset；本地 exact-span 和 source revalidation 保持不变。candidate rules 升级为 `candidate-rules-v2`，prompt bundle 为 `writing-material-prompts-v9`；历史 classification-v1–v6 artifacts 保持只读兼容。
- 新增 `configs/writing/prompts/classify-v7.md`，更新生产/默认配置和 tests。专项新增 incomplete source coverage filtering、nested schema uniqueness、legacy array rejection、duplicate JSON object key rejection；`pytest -q tests/writing_material` 为107 passed，全仓 `pytest -q` 为485 passed，Ruff 全仓、mypy 129 source files 和 `git diff --check` 通过。
- 同一30篇新 dry-run 为30/30 provenance、0 failure、634 candidate paragraphs；减少的1个 candidate 是只有不完整 source-span sentence 的 paragraph。0600工件 fingerprints：dry-run `0d8fc2195fa67570a95919325120ec993c3ac93c498d6c8f36f04c4facd32394`、ready gate `f22073fe2fc0d4b3cc4b54a145d9bddbb7a08beec042d1dbb8ec1ceceb7967be`、preflight `8755067e91e766ca826102e8069d1554eedb6dbc46d109cfcba8283e1e21cc2a`；version bundle `fb0f2dcaf6f1f5167043e6de5eea8f9d0794e01d28e0747ad5832fbdf869a31d`。三份报告均未调用 LLM；旧 approval `fc2add0b...` 已因 version bundle 漂移失效。

### Phase 6B.1.6 实施记录：classification 生成预算与执行参数追踪（2026-07-18）

- 用户批准 v7/v4 gate `f22073fe...` / bundle `fb0f2dca...`，生成0600 approval `a9b173782cc6a2225b68b507ace81f20869cd70a6969a82970ff002c4b95e862`。真实 run `20260718T100054Z-7fc10bb8e8b5` 前6篇成功，第7篇 provider content 在约13945字符处截断并触发 `JSONDecodeError`；run fail-fast 为 `processed=6`、`failed=1`、108 evidence、4 strategy、9 template、8 phrase，source verified、不可索引，剩余23篇未请求。
- 根因是 v7嵌套对象增加了最坏输出长度，而生产 `classification_max_tokens=4096`。严格 parser 正确拒绝不完整 JSON，失败响应未进入 LLM cache，也没有任何残缺 evidence/material 被保存。
- classification schema/prompt 继续保持 v7/v9，不伪造 schema 升级；生产和默认 `classification_max_tokens` 提升为8192。现有 production `batch_size=4` 保持不变，避免请求数翻倍。按本地约20 tokens/s估算8192 tokens 最坏约410秒，因此 provider timeout 同步提升至600秒。
- `classification_batch_size`、`provider_timeout_seconds`、`provider_max_retries` 现与 classification/abstraction token limits 一起进入 version manifest 和 run generation limits；任何执行预算漂移都会使 approval/version bundle 失效。对应 fixture CLI 测试重验持久化字段，provider adapter 测试验证8192请求上限。
- 验证：`pytest -q tests/writing_material` 107 passed；全仓485 passed；Ruff全仓、mypy 129 source files、`git diff --check`通过。最终0600工件 fingerprints：dry-run `8853de0c63798bba6e4c7c2354e96c1a14e820bce8c3fed6b2765fd7f0fa62b7`、ready gate `1992a2195054c100afbb8465fa354d40eb020e81e6304dd573233636f406adc7`、preflight `c65402afa61a20d07dd2c841953b2284fe7fcb6438e11472f7801800b4d34e14`；version bundle `4f79a5aaa52cca4ee740ded004438ac1d45c6562e258ce2aa7b0e99f60cf0d32`。30/30 provenance、0 failure、634 candidates，三份报告未调用 LLM；旧 approval `a9b17378...` 已失效。

### Phase 6B.1.7 实施记录：紧凑多标签 classification contract（2026-07-18）

- 用户批准 gate `1992a219...` / bundle `4f79a5aa...` 后生成0600 approval `a50e010c74259eae7fafa5215a5e65ec84634df24c433133e2d084187266a675`。真实 run `20260718T102848Z-483e807034b3` 前6篇成功，第7篇在约27842字符处返回 `Unterminated string`；这与4096-token run约13944字符的截断位置近似成倍，且 provider 正常返回200，证明模型再次填满8192生成预算而非服务卡死。run 为 `processed=6`、`failed=1`、144 evidence、4 strategy、6 template、13 phrase；只读 source validation 为 `errors=[]`、`source_verified=true`、`index_eligible=false`、pending=167，剩余23篇未请求。
- 不再继续无界提高 token budget：本地模型上下文为16384，v7按每个 sentence/category 重复 claim/risk/confidence 会使高密度 batch 输出随标签数膨胀。`classification-v8` 保留第一层动态 sentence-ID object，但每个选择句只返回一个共享 decision；完整闭合 `category_decisions` boolean map 表示全部启用类别，多标签由 true 项在本地展开。风险 map、claim strength 和 confidence 每句只出现一次。
- parser/provider schema 同时拒绝缺失或额外类别、非布尔类别值、全 false decision、未知 sentence、重复 object key、不完整风险 map 和旧数组/嵌套类别形状。模型仍不能输出正文、offset 或 paragraph ID；本地 exact-span/source revalidation 和 evidence immutability 不变。历史 classification-v1–v7 保持 stored/review 只读兼容，新请求只生成 v8。
- 新增 `configs/writing/prompts/classify-v8.md`，更新生产/默认配置；prompt bundle 为 `writing-material-prompts-v10`。修改文件包括 `materials.py`、`extract.py`、hub/config、writing-material config、三个专项测试文件和相关设计/运行文档。
- 验证：`pytest -q tests/writing_material/test_materials.py tests/writing_material/test_provider_and_dedup.py tests/writing_material/test_extract_review.py` 为85 passed；`pytest -q tests/writing_material` 为113 passed。新增测试覆盖完整类别 map、非布尔/全 false 拒绝、多标签本地展开、provider schema 精确结构、fixture extraction 和 v1–v8 × abstraction-v1–v4 历史 manifest 兼容。
- 同一30篇 selection 的0600工件为 `/tmp/knowledgehub-writing-material-phase6b-20260718/{dry-run-classification-v8-abstraction-v4.json,ready-gate-classification-v8-abstraction-v4.json,provider-preflight-classification-v8-abstraction-v4.json}`；fingerprints 依次为 `e1834eb194906668580b389ca42c41c78dec3e2cec9a674108fb96d8b45def1c`、`a06fd0473ab5993705df0338ad908ca9964a0f0e8633997b2f6aaa4d02ad70ed`、`d1b2e2c72c7ad987f14e35c9b9b0b38c30bcbd3bc4df0e55b3c910bc7f3e82`；version bundle `eb99291722fc66792f13ec0f24dc24694ca6c28b593a24f8514b3c11ad783a1e`。30/30 provenance、0 failure、634 candidates；preflight 未创建 provider client、未发网络请求。旧 v7 approval/gate 不可复用。

### Phase 6B.1.8 实施记录：provider 侧非空类别选择（2026-07-18）

- 用户批准 v8 gate `a06fd047...` / bundle `eb992917...` 后生成0600 approval `/tmp/knowledgehub-writing-material-phase6b-20260718/pilot-approval-classification-v8-abstraction-v4.json`，fingerprint `3f62db82e284665c652a8cb36b19c22174c4de27e8b819d0d859e86bb604c88c`。真实 run `20260718T110041Z-36d9fd47db51` 的首个请求由 vLLM 以约20 tokens/s正常生成并返回HTTP 200，不是服务卡死或超时。
- 首篇响应至少包含一个 `category_decisions` 全 false 的 sentence decision。v8 provider schema要求完整 boolean map，但 JSON Schema不能通过普通 boolean type表达“至少一个 true”；应用 parser 按契约拒绝 `classification decision must select a category`。run fail-fast 为 `processed=0`、`failed=1`、0 evidence/material，后29篇未请求；只读 validation 为 `errors=[]`、`source_verified=true`、`index_eligible=false`、pending=0。失败响应未写入有效 LLM cache。
- `classification-v9` 将 `category_decisions` 改为动态闭合的 selected-key object：只允许启用 taxonomy 类别作为键，每个出现的值必须为 `true`，`minProperties=1`，遗漏键表示 false。由此在受约束解码期排除空对象、false 与未知类别，同时保留多标签、object-key uniqueness 和比v8更短的响应。parser再次拒绝空对象、false、未知/重复键；正文、offset、paragraph ID仍不能由模型输出。
- 新增 `configs/writing/prompts/classify-v9.md`，生产/默认配置切换到v9，prompt bundle为 `writing-material-prompts-v11`；历史 classification-v1–v8 stored/review artifacts保持只读兼容。专项测试覆盖 provider `minProperties`/`const true`、empty/false/unknown rejection、多标签展开、fixture和v1–v9 × abstraction-v1–v4 manifest兼容。
- 验证：核心三文件89 passed，`pytest -q tests/writing_material` 117 passed，全仓 `pytest -q` 495 passed，mypy 129 source files与 `git diff --check` 通过；Ruff残留unused test import已移除并复验。
- 同一30篇 selection 的0600工件为 `/tmp/knowledgehub-writing-material-phase6b-20260718/{dry-run-classification-v9-abstraction-v4.json,ready-gate-classification-v9-abstraction-v4.json,provider-preflight-classification-v9-abstraction-v4.json}`；fingerprints依次为 `a1788cd4b6769a3f4e96bcdcd3f574201f69dbef03f1b1d0a832ffe9b69671f9`、`d98f181ea2fde58e2f8d9aab048ecf3e02e08bc065bcad47f4cee873995a0f32`、`a631281d5efafbf8a34f23692d9e60a752c091674e7fbd0422b80a34bc8a8600`；version bundle `e6b1bbb8b3eb1d01c726cdeaf1a97940854e950cce2c02c95fd4a0ac9a2ba751`。30/30 provenance、0 failure、634 candidates；preflight未创建provider client、未发网络请求，v8 approval不可复用。

### Phase 6B.1.9 实施记录：sentence-bounded request partition 与失败文档原子性（2026-07-18）

- 用户批准v9 gate `d98f181e...` / bundle `e6b1bbb8...` 后生成0600 approval `92b669b60deb6fa0ab187eab6badbd5f1e38cf30c626c5b22189510039d9be59`。真实run `20260718T112350Z-079e448f3db6` 连续完成前6篇；第7篇在最后一个classification请求约32555字符处截断，run为processed=6、failed=1、239 evidence、6 strategy、19 template、13 phrase，source validation通过且不可索引。
- 只读重建失败文档得到41 candidate paragraphs、113 authoritative sentences；旧4-paragraph batches的sentence totals为 `[21,16,17,19,12,8,5,4,6,4,1]`。因此反复截断的根因不是schema字段，而是paragraph batching未限制sentence cardinality；同一请求可包含21句并把输出填满8192 tokens。
- 新增 `writing-material-request-partition-v1`：classification同时限制最多4 paragraph slices和8 authoritative sentences；长段落保持immutable paragraph ID/text/provenance，只把暴露给provider的sentence IDs切成安全slice。classification trace request hash加入sentence IDs。abstraction另按最多8 evidence分批，跨批生成ID重复直接拒绝。
- 分片版本、classification sentence limit和abstraction evidence limit进入version manifest、generation limits与approval bundle。dry-run新增 `request_partition_plan`；当前30篇实际规划426个classification requests，observed maximum=8，避免用paragraph数量猜测输出规模。
- 修复失败checkpoint原子性：classification中途失败不再保存该文档早先子批次的部分evidence；只有classification已全部完成而abstraction失败时，才保留完整且已验证evidence。历史immutable partial run不改写，仍禁止review-complete/index。
- 新增专项fixture验证1-sentence classification slicing、1-evidence abstraction slicing、分片字段持久化和第二classification batch失败时0 partial evidence。验证：writing-material 119 passed、全仓497 passed、Ruff、mypy 129 source files、`git diff --check`通过。
- 新0600工件 `/tmp/knowledgehub-writing-material-phase6b-20260718/{dry-run-classification-v9-partition-v1-abstraction-v4.json,ready-gate-classification-v9-partition-v1-abstraction-v4.json,provider-preflight-classification-v9-partition-v1-abstraction-v4.json}` fingerprints依次为 `40fe73d9d42eece00191eb6a5f8bae93bef39993d3403c2fe0b07b09f8ee7afe`、`0f4e5a53a30ba316d2032e7b76da458b992c49c638bd64f5818e8032a250c85e`、`3d7693dfeb7df0541d4ab1bdbeb5444fea7086a694ff4861b0cff2da577ba5ec`；bundle `0169a08ffb5adf949ea7e51c06472fba9a7cebf4a76b5f8f5d1032b10ef991e8`。30/30 provenance、0 planning failure、634 candidates，preflight未联网。

### Phase 6B.1.10 实施记录：abstraction evidence 引用结构唯一性（2026-07-18）

- 用户批准 partition-v1 gate `0f4e5a53...` / bundle `0169a08f...` 后生成 approval `646b4243...`。真实 run `20260718T121903Z-7119207756be` 的前3篇完整成功；第4篇的 classification 也全部完成，随后 abstraction-v4 因 `material contains duplicate evidence references` fail-closed。run 为 `partial`：processed=3、failed=1、156 evidence、10 strategy、18 template、9 phrase；其中失败文档的89条完整 verified evidence 按既定 checkpoint 语义保留，没有保存该文档的部分 material，后26篇未请求。该历史 immutable run 不改写且不可索引。
- 根因限定在 provider 响应的 `evidence_ids` 数组：JSON Schema 可限制元素 enum，却无法在当前 vLLM/xgrammar contract 中依赖 `uniqueItems` 强制唯一。`abstraction-v5` 将 provider 字段改为 `evidence_decisions` 封闭对象：properties 只列出本批最多8个 evidence ID，`additionalProperties=false`、`minProperties=1`，每个出现的值 `const true`。JSON duplicate-key loader、provider schema和Python parser三层共同拒绝重复、未知、false与空引用。
- `src/knowledgehub/writing_rag/materials.py` 将选择对象按请求 evidence 顺序投影回持久化 `evidence_ids` tuple，因此 `writing-material-v1`、review、accepted snapshot和index读取格式不变；abstraction-v1–v4历史 manifest 仍受支持。`src/knowledgehub/writing_rag/extract.py` 的 fixture/provider schema同步为v5；新增 `configs/writing/prompts/abstract-v5.md`，prompt bundle升级为 `writing-material-prompts-v12`。
- 新增/更新测试覆盖 schema properties/const/minProperties、unknown/false/empty引用拒绝、fixture E2E和 classification-v1–v9 × abstraction-v1–v5 历史 manifest 兼容。验证：writing-material 128 passed、全仓506 passed、Ruff通过、mypy 129 source files通过、`git diff --check`通过。
- 同一 `approved-selection-v2.jsonl`（selection hash `1e3672ee...`）的新0600工件为 `/tmp/knowledgehub-writing-material-phase6b-20260718/{dry-run-classification-v9-partition-v1-abstraction-v5.json,ready-gate-classification-v9-partition-v1-abstraction-v5.json,provider-preflight-classification-v9-partition-v1-abstraction-v5.json}`；fingerprints依次为 `906b9c245630a08796c925058633ca0ed3fe02299c58c53f21b6cf07c0822d13`、`cc24077893f6dddbb77fb711b6ea21b131ce31e177329224321ca0075db92179`、`a6293a9a8687dd1083244acbccb1dc1478d928488903b02eeacac2ad0a518bcf`；version bundle `bdca87aac3a27337ec1d069e186dcf314da2cba07ddf0a4e4d1861d48b0f782c`。30/30 provenance、0 planning failure、634 candidates、426 classification requests且观测最大8句；preflight没有创建provider client或发送网络请求。

### Phase 6B.1.11 实施记录：abstraction token-limit 自适应分片（2026-07-18）

- 用户批准 v9/v5 + partition-v1 gate `cc240778...` / bundle `bdca87aa...` 后生成 approval `d6ff7ea7...`，真实 run `20260718T131420Z-3ec79b86421c` 完成前6篇，并使此前失败的第4篇 `27W2FRNW/QK2DISB2` 完整通过：其89条 evidence 全部产生合法 material，不再出现重复引用。第7篇 `28NB3QPQ/HGTXGMJX` classification 完成后保留37条 verified evidence；首个异常 abstraction 请求持续约6.5分钟、耗尽8192 token并返回约33170字符的截断 JSON，严格解析在 char 33169 拒绝。run 为partial：processed=6、failed=1、273 evidence、35 strategy、60 template、44 phrase；后23篇未请求，无部分失败文档 material、审核或索引写入。
- 对本轮34个成功 abstraction-v5 cache响应做只读统计：最大10610字符，strategy/template/phrase最大数量分别为3/5/6；失败响应是明显长尾而非正常规模。`ProviderOutputTruncatedError` 现在只在 provider 明确 `finish_reason=length` 时产生，原响应不缓存、不解析、不保存。
- `abstraction-v6` 将 strategies/templates/phrases 的 provider `maxItems` 动态设为当前 evidence batch 数，prompt-v13要求优先输出简洁、可迁移且不重叠的记录。`writing-material-request-partition-v2` 在捕获明确 token-limit 后将当前 evidence batch 二分并重新发起独立、完整、严格schema请求；必要时递归至单 evidence，单 evidence仍截断则保持整篇fail-closed。不存在截断JSON拼接、模糊修复或静默接受。
- version manifest、generation limits和dry-run plan新增 `abstraction_adaptive_split_on_truncation=true` 与 `abstraction_min_evidence_per_retry=1`，因此旧approval不能复用。新增测试覆盖 `finish_reason=length` 专用错误、2→1+1自适应成功、动态material cardinality、v1–v6历史manifest兼容；验证writing-material139 passed、全仓517 passed、Ruff、mypy 129 source files和`git diff --check`通过。
- 新0600工件 `/tmp/knowledgehub-writing-material-phase6b-20260718/{dry-run-classification-v9-partition-v2-abstraction-v6.json,ready-gate-classification-v9-partition-v2-abstraction-v6.json,provider-preflight-classification-v9-partition-v2-abstraction-v6.json}` fingerprints依次为 `68172621031428060fa47960eb848f5e1bfe1f4c1db4507fb47f8e14e353cea6`、`ecdfedfe457075987c538104887e40a744d83ab63b05ad4f2421d52036f31d9f`、`f61cc7383e31bc81434bbbe93f4ddc5759e784ccaa3428a70bccc6b796fed3b7`；version bundle `64338d3beb96300779af1bba41d03367becea8fdf6c4a68d1904a2f50db758ab`。selection仍为同一30篇、30/30 provenance、0 planning failure、634 candidates、426个classification请求且观测最大8句；preflight未联网。

### Phase 6B.1.12 实施记录：严格结构化输出的有界纠正（2026-07-18）

- 用户批准 v9/v6 + partition-v2 gate `ecdfedfe...` / bundle `64338d3b...`，生成0600 approval `da4edeff...`。真实 run `20260718T144829Z-7128ac9f0e16` 连续完成前8篇、0失败，其中此前 token-limit 失败的第7篇 `28NB3QPQ/HGTXGMJX` 完整通过；第8篇为全新未缓存文档，也完整通过。
- 第9篇 `29LS6Y9D/M4F9RA36` classification完成后，abstraction provider返回两个产生相同 `phrase_id` 的phrase payload。`parse_abstraction_response()` 按契约抛出 `MaterialValidationError: duplicate phrase payload`；失败响应未写入LLM cache，run按零失败策略结束为partial：processed=8、failed=1、423 evidence、52 strategy、112 template、44 phrase，后21篇未请求。该失败不是token-limit，因此partition-v2正确地没有二分，也没有静默去重或接受非法响应。
- 对该run执行只读`writing-material validate --run-id 20260718T144829Z-7128ac9f0e16`：`errors=[]`、`source_verified=true`、423/52/112/44资产全部通过重读验证；因`extraction_status=partial`且631项pending，`index_eligible=false`，命令按设计以exit 1阻止后续索引。
- 新增 `structured-output-correction-v1`：仅捕获已成功解码但未通过严格本地语义validator的响应，拒绝且不缓存原响应，并在保持原source input、JSON schema、temperature和token budget不变的前提下，追加明确validator错误执行一次完整重生成。第二次仍非法则保留原fail-closed行为；invalid JSON、read timeout、transient HTTP和明确`finish_reason=length`仍使用各自独立策略。
- correction版本、最大尝试次数1和correction instruction hash进入version manifest，prompt bundle升级为`writing-material-prompts-v14`。run generation limits与dry-run partition plan持久化correction版本/次数，因此旧approval/bundle自动失效。provider cache key仍只绑定原始prompt/schema/input/budget：此前已通过同一严格validator的合法响应可安全复用，而非法响应从未缓存，失败请求会实际进入correction-v1。这样既重新验收全部文档，又避免无意义重复生成前8篇。abstraction schema仍为v6、request partition仍为v2，没有伪造schema或分片版本升级。
- 新增provider专项测试验证duplicate phrase首次非法/第二次合法时只缓存合法响应且后续命中缓存；持续重复时仅调用两次、无缓存并拒绝。既有token-limit专用异常与二分测试继续通过。验证：provider 16 passed、writing-material 141 passed、全仓519 passed、Ruff lint通过、mypy 129 source files通过、`git diff --check`通过。全仓`ruff format --check`报告37个既有文件需格式化，未执行无关批量改写。
- 同一30篇selection的新0600工件为`/tmp/knowledgehub-writing-material-phase6b-20260718/{dry-run-classification-v9-partition-v2-abstraction-v6-correction-v1.json,ready-gate-classification-v9-partition-v2-abstraction-v6-correction-v1.json,provider-preflight-classification-v9-partition-v2-abstraction-v6-correction-v1.json}`；fingerprints依次为`9432ab36e2814b2adba6be8248f973762f8f0a900e0557209ca3199b5eb01808`、`decc6ff2d14c1790937d35ad4271e069bae92f118a3bee0ea703e475850a8456`、`b46697f55ed565cfbd939057496c744a5afe21e4fade12265c34680ed710bd92`，bundle为`e0344db6210c2093861b10faa2517f2d83cfd71da8f5b66c5963be202cf1616f`。30/30 provenance、0 planning failure、634 candidates、426 classification requests，preflight未创建provider client或联网。未获得绑定新gate/bundle的明确批准前，不再调用真实LLM。历史partial run保持immutable且不可review-complete/index；已批准的真实run创建了新的合法响应cache与run资产，但失败响应未缓存，且未删除/覆盖既有cache；Qdrant、accepted snapshot、review decisions、Zotero和生产索引均未修改。

### Phase 6B.1.13 实施记录：category-bound abstraction evidence（2026-07-19）

- 用户批准 correction-v1 gate `decc6ff2...` / bundle `e0344db6...`，生成0600 approval fingerprint `44857e72...`。真实 run `20260718T171816Z-afd75304a72c` 复用前7篇合法cache，重新完成第8篇，并完整越过第9篇历史duplicate phrase停止点；随后连续完成到第13篇，均为0失败。
- 第14篇 `2B9M8A53/TWYPCBNT` classification完整完成并保留57条verified evidence；abstraction返回的material category没有被其引用evidence支持。唯一一次correction仍产生同类错误，严格validator拒绝。run按零失败策略结束为partial：processed=13、failed=1、671 evidence、91 strategy、176 template、60 phrase，后16篇未请求；失败文档部分material未保存。
- 只读validate结果为`errors=[]`、`source_verified=true`，671/91/176/60资产全部通过重读验证；因partial且998项pending，`index_eligible=false`。该失败不是token-limit或provider故障，因此没有错误触发二分或传输重试。
- 根因是v6 schema分别约束`category`与`evidence_decisions`，受约束解码仍可生成二者不一致的合法JSON。`abstraction-v7`改为`category_evidence_decisions`：外层闭合对象必须且只能选择一个category key；该key下只暴露输入中同类别evidence IDs，至少选择一项且值只能为true。category/evidence错配、跨类别引用、重复ID、false、空选择和未知ID均在schema/parser边界拒绝。
- 持久化`writing-material-v1`不变：parser仍投影为独立category和按请求顺序排列的evidence_ids，因此review、accepted snapshot和index读取格式无需迁移；abstraction-v1–v6历史run继续只读兼容。新增`configs/writing/prompts/abstract-v7.md`，prompt bundle升级为`writing-material-prompts-v15`，生产及默认配置切换到v7。
- 验证：核心parser/provider/extraction 122 passed、writing-material 150 passed、全仓528 passed、Ruff lint通过、mypy 129 source files通过、`git diff --check`通过。测试直接验证不同category schema只能暴露各自evidence ID，以及unknown/false/empty/mismatched选择拒绝、fixture E2E和v1–v7历史manifest兼容。
- 新0600工件`/tmp/knowledgehub-writing-material-phase6b-20260718/{dry-run-classification-v9-partition-v2-abstraction-v7-category-bound-correction-v1.json,ready-gate-classification-v9-partition-v2-abstraction-v7-category-bound-correction-v1.json,provider-preflight-classification-v9-partition-v2-abstraction-v7-category-bound-correction-v1.json}` fingerprints依次为`85a96fbea9cb49434119c8fced81ac3bd6a74f391c612526e711eebb68fbc1d9`、`d6c3e7858ecaf5d43792c967a31a10a90a59d41216f072359d67439a51582b76`、`fa27cb0cc470ebd14a0097e69ed89ef4cb0497132d2bfa9cac6849a89064af59`；bundle `180766130ac18876627896a4654ab705d8a2a64127debb570ddaf6cba5f97d94`。30/30 provenance、0 planning failure、634 candidates、426 classification requests；preflight未创建provider client或联网。

### Phase 6B.1.14 实施记录：duplicate-specific bounded correction（2026-07-19）

- 用户批准 gate `d6c3e785...` / bundle `18076613...`，生成0600 approval fingerprint `ceb1f146e6b5716c655733b4f3b9849add85d125f745e2245edb4a953e4e4691`。真实 run `20260719T003746Z-2d4777168a9f` 连续完成前24篇、0文档失败，并使旧第14篇 `2B9M8A53/TWYPCBNT` 在 abstraction-v7 下完整成功；这验证了 category-bound schema 修复的真实有效性。
- 第25篇 `2DZMZACE/U8QDPB3D` classification 完成并按失败 checkpoint 语义保留39条 exact/source-verified evidence；某个 abstraction 批次先返回重复 template canonical payload，唯一一次 correction 仍产生同类重复。`parse_abstraction_response()` 以 `MaterialValidationError: duplicate template payload` 拒绝，非法响应未缓存、失败文档没有保存任何部分 material。run 按零失败策略结束为 `partial`：processed=24、failed=1、1342 evidence、241 strategy、360 template、271 phrase，后5篇未请求。
- 只读 `writing-material validate --run-id 20260719T003746Z-2d4777168a9f` 重读全部2214项资产，`errors=[]`、`source_verified=true`；因 extraction status 为partial，`index_eligible=false`且命令按设计exit 1。没有生成 review decision/accepted snapshot，也没有访问或修改Qdrant、Zotero、生产索引、collection或alias。
- 该失败不是token-limit、传输故障或category/evidence错配，因此partition-v2正确地没有二分，provider transient retry也没有误触发。继续按相同temperature=0和相同 correction-v1 提示盲重试会重复未缓存的确定性请求，不能作为实质修复。
- `structured-output-correction-v2` 保持最大纠正次数1、原source input、JSON schema、temperature和token budget不变；仅将 correction instruction 明确为：strategies/templates/phrases 每个数组必须视为canonical payload集合，duplicate错误只生成一个对应record，禁止以表面改写规避重复。原始非法响应仍拒绝且不缓存，第二次仍非法仍整篇fail-closed；没有本地静默去重、fuzzy接受或schema伪升级。prompt bundle升级为`writing-material-prompts-v16`，abstraction仍为v7、partition仍为v2。
- 修改文件：`src/knowledgehub/writing_rag/extract.py`、`tests/writing_material/test_provider_and_dedup.py`、本计划和实施审计。专项parser/provider测试27 passed；`pytest -q tests/writing_material`为150 passed；Ruff writing范围通过，mypy writing_rag 11 source files通过。未重复执行全仓528-test回归，因为本次仅改常量纠正提示和对应provider断言，完整writing范围已覆盖调用链。
- 同一30篇 selection 的新0600工件为`/tmp/knowledgehub-writing-material-phase6b-20260718/{dry-run-classification-v9-partition-v2-abstraction-v7-category-bound-correction-v2.json,ready-gate-classification-v9-partition-v2-abstraction-v7-category-bound-correction-v2.json,provider-preflight-classification-v9-partition-v2-abstraction-v7-category-bound-correction-v2.json}`；fingerprints依次为`594a31e226b3ab87b1f5651ddc161b9a17439e3793dcffa1c27736f420f6e8d6`、`9ceb02cc1ed329320db683ccfbae96c25f57a0673fd85a335e5c30d5abcd7903`、`279a2281269f87b4ed89c8593be8037a80d82b80d828aad24d2d72e3cdf15105`；bundle `dc31d16a127dbc2ea820733d094ffed0d8fa94fa89d1012c2f653c25f60d09b7`。30/30 provenance、0 planning failure、634 candidates、426 classification requests且每请求最多8句；preflight未创建provider client或发送网络请求。旧 approval/bundle 已失效，未获新明确批准前不得调用真实LLM。

### Phase 6B.1.15 实施记录：correction-v2 真实30篇 extraction 闭环（2026-07-19）

- 用户明确批准 gate `9ceb02cc1ed329320db683ccfbae96c25f57a0673fd85a335e5c30d5abcd7903` / bundle `dc31d16a127dbc2ea820733d094ffed0d8fa94fa89d1012c2f653c25f60d09b7` 的 `--retry-failed` 真实 extraction。0600 approval `/tmp/knowledgehub-writing-material-phase6b-20260718/pilot-approval-classification-v9-partition-v2-abstraction-v7-category-bound-correction-v2.json` fingerprint为`e348403bbdedc29933a35c6c725c57f14e8fa59d0394fe576791cfac8276d6d7`，绑定approver/reviewer `lengmo`、`private research use`、`local reviewer only`、`five years`，且`production_index_authorized=false`。
- 本地 `openai_compatible` provider调用`qwen3-32b-awq`完成 run `20260719T064746Z-f99463512f16`。CLI exit 0；manifest为`status=success`、selected/planned/processed=30、failed=0、changed=30，生成1523 evidence、280 strategy、423 template、270 phrase。generation limits确认为classification每请求最多8句、abstraction初始最多8 evidence、仅明确token截断时自适应二分至1、8192 token、600秒timeout、严格校验失败最多纠正1次。
- 旧 correction-v1 关键失败文档 `zotero:user:18650896:2DZMZACE:U8QDPB3D:0` 在第25篇成功原子提交，`duplicate template payload`未复现；后续5篇继续完成，没有被零失败策略提前停止。`failures.jsonl`为0字节，checkpoint sequence 32及全部asset SHA-256写入manifest。
- 严格执行 `knowledgehub writing-material validate --run-id 20260719T064746Z-f99463512f16`，exit 0、`status=success`、`errors=[]`、`source_verified=true`；重读计数与manifest一致。审核分布为accepted=0、edited=0、rejected=0、pending=2496，因此`index_eligible=false`，没有生成accepted snapshot或candidate。
- 本阶段只写入受控approval、extraction run/state与合法LLM cache；没有删除或覆盖旧run/cache/manifest，没有访问、创建或修改Qdrant collection/alias、生产索引、review decisions或Zotero。真实LLM仅为本机127.0.0.1上的获批模型；未处理selection之外的文献。
- 实现验证：correction-v2专项provider/material测试27 passed，`pytest -q tests/writing_material`为150 passed，全仓`pytest -q`为528 passed；Ruff全仓、mypy 129 source files、`git diff --check`均通过。真实run后另执行严格source validation作为外部验收；文档更新后复跑writing-material范围和diff检查。
- Phase 6工作项6至此完成。下一阶段严格限定为工作项7：为2496项pending材料生成/使用审核材料，由`lengmo`给出显式decision并生成complete accepted snapshot；在审核完成前不得创建candidate或写任何正式索引。

### Phase 6B.2 实施记录：全量 accepted 审核与 complete snapshot（2026-07-19）

- reviewer `lengmo` 明确指示“就直接标记为accepted”，授权对当前run全部pending资产作出批量人工接受决定。执行前 `review render` 重新验证并报告records=2496、pending=2496、accepted/edited/rejected=0，且不存在既有`review-events.jsonl`，因此没有覆盖或冲突既有人工结果。
- 从`review-status.jsonl`将每项当前`asset_id`与`based_on_hash`展开为独立decision，reviewer为`lengmo`，统一reason记录本次显式指令。0600 decision工件为`/tmp/knowledgehub-writing-material-phase6b-20260718/decisions-20260719T064746Z-f99463512f16-accept-all.jsonl`，2496行、SHA-256 `1eb0f3a1e4bf0f66aabd2af111c3aa5e7ce8d4bf622852be2a401be76e305695`；任何资产漂移仍会由stale hash gate拒绝。
- `writing-material review apply`在写入前重新执行source validation，随后append 2496条`writing-material-review-v1`事件，duplicate ignored=0；生成complete `writing-material-accepted-v2` snapshot。review counts为accepted=2496、pending/edited/rejected=0，dependency exclusion=0；accepted counts为1523 evidence、280 strategy、423 template、270 phrase，`index_eligible=true`。
- 导入后再次执行`writing-material validate --run-id 20260719T064746Z-f99463512f16`，exit 0、`status=success`、`errors=[]`、`source_verified=true`，accepted/review计数与snapshot完全一致。review events、projection、accepted manifest及JSONL均为0600。
- 本阶段没有调用LLM、没有重新提取文献、没有访问或修改Zotero、Qdrant、collection、alias、candidate或生产索引。Phase 6工作项7完成；下一阶段为工作项8，只能基于本complete snapshot构建新的隔离candidate并执行retrieval/source-join验收。

### Phase 6B.3 实施记录：隔离 candidate 与 retrieval/source-join 验收（2026-07-19）

- 执行前只读确认生产 Writing collection `knowledgehub_writing_qwen3_4b_1024_v1` 为134 points；新名称 `knowledgehub_writing_material_candidate_20260719_f99463512f16` 不存在。index dry-run重验complete accepted/source，规划973个derived assets、0 failure、promotion=false且零写入。
- 实际accepted-only build成功创建全新物理candidate；manifest `/data/KnowledgeHub/writing-materials/index-candidates/cbb7d6fbbd98ac8f01779aa5/writing-material-candidate.json` 为0600、`writing-material-candidate-v1`，fingerprint `4445bdbe159755af44dd1ca110454cc1126b40b52c6d5838ff960e197dd4bf9b`。selected/indexed/chunks均为973，failures=[]、source_verified=true、dry_run=false、promotion_performed=false。Qdrant实读为973 points/973 indexed vectors，dense 1024 cosine并带BM25 sparse；生产 Writing复查仍为134 points。
- candidate build前既有TEI 8080/8082未运行；真实extraction已完成后停止但未删除本地vLLM，按仓库compose启动双TEI。因TEI Rust下载器在权重已完整缓存时仍等待可选Hub元数据，使用临时`/tmp/knowledgehub-embed-offline.override.yaml`将同一固定revision snapshot作为本地绝对路径，未下载模型、未改仓库配置或依赖。build完成后停止TEI并恢复原vLLM容器。
- 0600 retrieval cases `/tmp/knowledgehub-writing-material-phase6b-20260718/retrieval-cases-20260719T064746Z-f99463512f16.jsonl` 包含8条人工选定的中英文、strategy/template/phrase跨类型用例；文件SHA-256为`a80df2b27ba3c720fa6643b471a5c60d4d4c49859e60c8ed86b71c2d99a03cd8`，全部expected IDs均属于同一accepted snapshot。
- 生成器以sparse模式真实查询candidate并逐hit source join，报告 `/tmp/knowledgehub-writing-material-phase6b-20260718/retrieval-report-20260719T064746Z-f99463512f16.json` fingerprint为`401a625eddf39bd5abaf3c2915c4d0e3296880e02a192c84f8847e62842b5a2d`。8条中6条在top-5命中预期资产，recall@5=0.75、MRR=0.75、source-join=1.0、duplicate ratio=0、join failure=0，超过既定minimum queries=5/recall=0.50/source-join=1.0门槛。
- 两项质量观察保留而不美化：中文红外小目标sparse用例没有返回hit；理论贡献模板用例命中了语义相近phrase/strategy但预期template未进top-5。它们不影响当前policy通过，但在扩量决策时应视为中文覆盖和同义跨类型排序风险。
- 最终`writing-material pilot evaluate`所有gates均为true：selection/extraction/exact-span/provider/source-join/complete-review/isolated-candidate/retrieval-quality；状态与recommendation均为`eligible_for_manual_expansion_decision`，automatic expansion=false。质量分数2496项mean=0.918、min=0.665；语言分布en=2470、zh=23、und=3，显示当前中文材料仍明显不足。
- Phase 6工作项8完成。本阶段没有调用LLM、没有处理新文献、没有修改Zotero、生产collection/alias或执行promotion。工作项9必须由用户结合语言失衡和两条retrieval miss明确决定“停止在pilot”或批准具体扩量selection，不能由通过报告自动扩量。

### Phase 6B.4 实施记录：扩量决策准备与当前目录可行性复核（2026-07-19）

- 在不读取PDF正文、不调用Zotero API/LLM和不写任何状态的前提下，将当前`papers.jsonl`的77个document IDs只读关联到固定Zotero documents manifest元数据；77/77解析成功，按language/title判定仅2篇中文、75篇英文。这复核了早期审计结论，不是对完整3574篇库执行selection或提取。
- 两篇中文候选为`2C7KIX4T/9PX7YE6X`（《61771341 基于高阶信息的视觉目标跟踪研究》）与`2F57B97X/ZMCCRWAU`（《低信噪比下的红外弱小目标检测算法研究综述_杨昳》）。当前30篇成功run仅包含后者；前者在早期dry-run已以`no_provenance_paragraphs`严格拒绝，不能在未修复源资产时作为安全扩量输入。
- 当前pilot材料语言分布en=2470、zh=23、und=3，8条retrieval用例又包含一条中文sparse零hit；因此此前建议“从当前清单新增20篇中文优先文献”与当前目录事实不一致，已撤回为不可执行建议。不得用75篇英文候选替代中文平衡目标，也不得为了凑数绕过provenance gate。
- 当前证据支持的保守建议是`stop_at_validated_pilot`：保留30篇成功run与973-point隔离candidate，不扩量、不promotion。若用户希望继续扩量，必须先明确提供/批准一个不属于当前77篇清单的中文Zotero collection或20篇具体document IDs；下一阶段仍只能先做selection+dry-run，不能沿用旧gate/approval调用真实LLM。
- 2026-07-19 通过 Zotero 本地只读 helper 复核候选发现入口：`http://127.0.0.1:23119` 明确返回 connection refused，Zotero Desktop Local API 当前未运行。未修改 Zotero preference、未重启桌面应用，也未用整库元数据扫描绕过 collection 授权；因此无法在本轮安全地产生新的中文 collection 候选清单。
- 2026-07-19 用户明确决定`保持当前 pilot，不扩量`。工作项9据此完成，决策结果为`stop_at_validated_pilot`：保留当前30篇成功run与973-point隔离candidate，不创建新selection、不继续extraction、不stage/promotion，也不修改生产Writing索引。未来若重新提出扩量，应作为新的批准阶段，从新的中文范围、selection与dry-run开始，不能复用本次决定作为LLM或索引授权。

### 安全边界完成性审计（2026-07-19）

- 实施依据已重新核对：基线审计为 `docs/writing_material_extraction_implementation_audit.md`，本计划为唯一后续实施清单，仓库中未发现 `AGENTS.md`；Git 工作区仍有未提交改动和未跟踪文件，本轮未清理、覆盖或提交它们。
- 基线审计的 `DIVERGED` 与可实施的 `PARTIAL`/`NOT_IMPLEMENTED` 缺口已由 Phase 1-5 关闭：stored validators、partial/failed 语义、evidence checkpoint、exact-span fixtures、chunk map、review completeness、stale/resume/selection、collection CLI 和 clone-and-merge release 都有当前代码与自动测试证据。
- 基线中其余非实施缺口已按设计决策收口：非 Docling/PyMuPDF/OCR 输入默认 fail closed；19 类 taxonomy 完整可配置但 MVP 默认启用 12 类；可变 review projection 不回写 immutable evidence/material；processing time 保留在 run/checkpoint；provider 保持可配置的 OpenAI-compatible adapter，未增加未批准依赖或第二 provider。这些是明确的安全/兼容边界，不应为了将历史矩阵全部改成 `IMPLEMENTED` 而机械改写。
- 原始要求中的正常路径、非法结构化输出、原文不存在/重复歧义/offset 不一致、provenance 缺失、schema/taxonomy/prompt/model 变化、provider 失败、changed/retry/resume、dry-run 零写入、review completeness 和索引隔离均已有专项测试。源码范围未发现未处理的 `TODO`/`FIXME`/placeholder 实现分支。
- correction-v1历史run依次暴露category/evidence错配和duplicate canonical payload，分别驱动abstraction-v7与correction-v2；当前run已按30/30 provenance、634 candidate paragraphs、classification每请求最多8句、abstraction初始最多8 evidence且仅明确token截断时二分的contract完成。
- correction-v2真实run `20260719T064746Z-f99463512f16` 已完成30/30、0失败并通过source validation；writing-material 150 passed、全仓528 passed、Ruff lint、mypy 129 source files和`git diff --check`通过。selection仍为30行，未扩量。
- 用户已明确选择保持当前pilot、不扩量。隔离candidate与retrieval/source-join已通过，但当前语言分布en=2470、zh=23、und=3且有两条top-5 miss；本决定不构成production promotion授权。历史partial run继续不可index，任何生产release/promotion仍需独立批准。
- 当前77篇候选目录只有2篇中文，其中1篇缺可靠provenance；因此不能在现有selection source内安全实现“新增20篇中文优先”。默认建议保持validated pilot并停止扩量，直到用户明确选择停止或提供新的中文范围。

### 影响

- Schema：extraction assets 不变；新增 `writing-material-candidate-v1`、`writing-material-retrieval-case-v1` 和 `writing-material-retrieval-evaluation-v1` 报告 contract。
- 重新处理：仅批准 selection；禁止默认全库。
- 索引：仅 candidate；正式影响需另行批准 Phase 5 promotion。

## 用户决策门

Phase 1 已采用以下保守默认并保留决策历史：

1. [已决定] `partial` run 默认禁止 candidate index，本阶段不提供 waiver；
2. [已决定] accepted snapshot 必须 100% explicit decision；部分审核只能生成显式命名且不可索引的 partial snapshot；
3. [已决定] 本轮不把可变 review projection 写回 immutable asset；review/accepted 已独立升级。单条 `processed_at`、统一 `extractor_version` 等 evidence/material schema v2 字段延后到 pilot 证明必要时再迁移；
4. [已决定，保守默认] 不自动提交、迁移、删除或覆盖当前临时 selection/decision/report；未来 pilot 报告只在显式 `--output` 下以 0600 写入批准的受控目录；已有用户 artifacts 保持原状。
5. [已决定，实施默认] 正式路径采用 clone 当前 Writing physical release 后增量合并 accepted materials；不以 scoped pilot collection 替换 active。真实 stage/promotion 仍需独立确认。
6. [已决定，2026-07-19] 保持当前30篇 validated pilot，不扩量；保留隔离candidate但不stage/promotion。未来扩量必须以新的中文范围重新进入selection + dry-run审批。

## Phase 7：检索 miss 修复与生产发布（2026-07-19）

本阶段由用户在 pilot 决策完成后另行明确授权，不改变“不扩量”决定，不重新执行 extraction，也不调用 LLM。发布继续遵循 Phase 5 的 clone-and-merge contract，不用 scoped 973-point candidate 覆盖已有 134-point Writing active。

### Phase 7A：两条检索 miss 质量闭环

1. [x] 建立 Git 实现基线：commit `2173303`（`feat: add Zotero writing material pipeline`）；本地 `papers.jsonl`、临时 decisions 与含原文 review sample 按 local-reviewer policy 排除。
2. [x] 复现中文 query 与目标模板 sparse term 交集为 0；实现保留原文 token 的 deterministic CJK bigram 扩展，并将 sparse preprocessing version 纳入增量 fingerprint。
3. [x] 为 Chunk 增加不污染 dense/display text 的 `sparse_text`，writing-material index processor 升级至 `writing-material-index-v4`；加入可追踪的 asset type/category sparse aliases。
4. [x] 对仅含一个明确 `template/strategy/phrase`（含中文同义词）意图的 query 增加 deterministic `asset_type` filter；含糊 query 不过滤，规则不读取 expected asset ID。
5. [x] 构建新的 accepted-only 隔离 candidate `knowledgehub_writing_material_candidate_20260719_f99463512f16_quality_v2`：973/973、green、source verified、promotion=false，fingerprint `320d00b14aa94c6aa0844027404ee39bddde621de241e329c3892687535adbe6`。
6. [x] 使用未修改的原8条 gold cases 重跑 sparse retrieval：两条旧 miss 均变为目标 Top-1，Recall@5=1.0、MRR=1.0、source join=1.0、duplicate=0；报告 fingerprint `799d2909015bbc9439c32934044975a1c9489abc6c37862371101a4b003bc797`。
7. [x] 全仓 pytest 534 passed、Ruff、mypy 129 source files与`git diff --check`通过；质量改进待本阶段紧接提交。

实际修改文件：`src/knowledgehub/indexing/sparse.py`、`src/knowledgehub/indexing/incremental.py`、`src/knowledgehub/pipeline/models.py`、`src/knowledgehub/retrieval/models.py`、`src/knowledgehub/retrieval/service.py`、`src/knowledgehub/hub/query.py`、`src/knowledgehub/cli/writing_material.py`、`src/knowledgehub/writing_rag/materials.py`、`src/knowledgehub/writing_rag/review.py` 及对应 tests。

### Phase 7B：clone-and-merge stage/promotion

1. [ ] 只读确认 active physical collection、stable alias、promotion state、134-point 基线与 rollback fallback。
2. [ ] release build dry-run 验证 `134 + 973 = 1107`，且 candidate 为新的物理 collection。
3. [ ] 创建 active snapshot，恢复到 release candidate，合并 accepted assets并验证1107 points、dense/sparse schema与source join。
4. [ ] 使用带 fingerprint 的 `writing-material-release-v1` manifest 执行显式 stage。
5. [ ] promotion 前复验 active snapshot/fallback 与 candidate manifest，再显式 promote stable alias。
6. [ ] promotion 后验证 alias、active point count、8-case retrieval/source join；保留 rollback 信息，不删除旧 active、隔离 candidates、snapshot、manifest或缓存。
