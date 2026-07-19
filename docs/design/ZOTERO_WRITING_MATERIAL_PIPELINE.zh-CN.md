# Zotero 写作素材自动提取流水线

状态：MVP 已实现，正式索引接入仍受人工审核和 candidate gate 约束
设计版本：1.0
Taxonomy：`writing-taxonomy-v1`

实施注记（2026-07-18）：后续计划 Phase 1-4 已完成。磁盘 artifact 采用 schema v1 closed-world 重校验；Docling provenance 采用 fail-closed contract；审核采用 append-only events + 显式 projection，完整 accepted-v2 snapshot 必须 100% explicit decision；增量入口支持冻结 selection、document/collection selector、哈希 checkpoint resume、stale reason 和幂等 attempt。

Release 注记：clone-and-merge、candidate count/schema validation、显式 stage/promotion/rollback 已封装在 `writing_rag/release.py`，运行边界见 `docs/writing_material_release_runbook.md`。受控 pilot 评估与报告生成见 `writing_rag/pilot.py` 和 `docs/writing_material_pilot_runbook.md`。这些能力默认不连接或修改生产 alias。2026-07-19 经独立授权和验收，30篇 pilot 的 accepted-only 增量已通过 release candidate `knowledgehub_writing_release_20260719_f99463512f16_quality_v2` stage/promote 到 `knowledgehub_writing_current`；旧134-point physical collection和发布前snapshot均保留为rollback依据，本次没有扩大selection或重新调用LLM。

## 1. 范围与安全边界

本流水线从 Literature RAG 的规范化解析资产中提取可复用的学术写作素材，不重新抓取 Zotero，不从 Qdrant 反向恢复原文，也不默认扫描整个文献库。

实现遵守以下边界：

- 输入必须是显式 selection JSONL；
- MVP 只接受 Docling 解析、section 对齐成功且 provenance coverage 达到配置门槛的文档；
- dry-run 不创建 run、state、cache 或 TaskStore 记录，也不调用 LLM；
- evidence 必须通过 exact-span gate，模型改写不得成为原文；
- 人工审核前不允许构建索引；
- candidate index 必须使用不同于 active Writing collection 的新物理 collection；
- candidate build 不执行 stage 或 promotion；
- 不修改 Zotero source、Literature parser/chunker、现有 Writing records 或现有 collection。

## 2. 当前仓库数据流

实际代码追踪得到的 Literature 数据流如下：

```text
Zotero Web API metadata
+ Nutstore WebDAV <attachment_key>.zip/.prop
  -> sources/zotero/attachments.py 安全解析附件关系和 ZIP
  -> /data/KnowledgeHub/zotero/extracted/<attachment_key>/*.pdf
  -> sources/zotero/state.py 中的 Zotero source SQLite
  -> documents.jsonl + delta-catalog.jsonl + deltas/*.jsonl
  -> pipeline/source.py: ZoteroManifestSource
  -> pipeline/models.py: SourceDocument
  -> parsing/docling_parser.py（失败时 parsing/pymupdf_parser.py）
  -> parsed/json/<document-hash>.json
     parsed/markdown/<document-hash>.md
  -> chunking/structural.py
  -> chunks/<document-hash>.parquet
  -> dense embedding + BM25
  -> indexing/qdrant.py: Literature collection
  -> retrieval/* / CLI / HTTP / MCP
```

关键状态和增量路径：

- Zotero 发布：`src/knowledgehub/sources/zotero/{sync,attachments,manifest,state}.py`；
- source manifest 消费：`src/knowledgehub/pipeline/source.py`；
- 解析资产：`src/knowledgehub/pipeline/artifacts.py`；
- pipeline 状态和恢复：`src/knowledgehub/pipeline/{state,orchestrator}.py`；
- chunk：`src/knowledgehub/chunking/structural.py`；
- 索引：`src/knowledgehub/indexing/{incremental,qdrant}.py`；
- 检索：`src/knowledgehub/retrieval/*`。

## 3. 可复用组件

仓库已有可复用的内部规范化资产层，但尚不是独立、公开、版本化的 normalized-document contract：

- Docling/PyMuPDF structured JSON；
- canonical Markdown；
- canonical chunk Parquet；
- Literature `pipeline.sqlite3` 中的 source、parse、chunk 和 embedding 指纹；
- `pipeline.artifacts.safe_document_name()` 的稳定资产命名；
- `core.atomic`、`core.hashing` 和 secret-redacting logging；
- `governance.tasks.TaskStore/TaskExecutor` 的任务、幂等和租约锁；
- `indexing.incremental.IncrementalChunkIndexer` 的隔离构建能力；
- `HubConfig` 和现有 YAML 配置系统。

新流水线只读取 Literature `pipeline.sqlite3` 的 ready 文档和 `parsed/json|markdown`。它不读取 Zotero source SQLite、WebDAV ZIP 或 Qdrant payload。

## 4. Provenance 现状与架构缺口

| 能力 | 当前资产 | MVP 处理 |
|---|---|---|
| section hierarchy | Docling section header/body 顺序；chunk `section_path` | 按 Docling 顺序重建，用 Markdown heading level 补层级；顺序无法唯一对齐则拒绝 |
| paragraph boundary | 旧 Writing 以 Markdown 空行启发式生成 | 每个可追踪 Docling text/list item 生成稳定 paragraph ID |
| sentence boundary | 旧 Writing 只保存临时句序号 | 标准库中英文切句，保存 paragraph-relative `[start,end)` 和稳定 sentence ID |
| PDF page | Docling `prov.page_no/bbox`；PyMuPDF 只有 page text | Docling-only；缺页码或 bbox 的正文 item 降低 coverage，未达门槛则拒绝 |
| Zotero keys | source manifest metadata 和 document ID | evidence 显式复制 item key 与 attachment key |
| chunk/source mapping | page range、section path、text hash | 不使用 chunk 做事实源；从 Docling item 构建 exact segment map |

当前限制：

- Docling `charspan` 的跨版本、OCR 和跨页语义仍需更多真实 fixture 验证；
- MVP 将 Docling text/list item 作为 paragraph，尚不合并被版面切碎的多 item 自然段；
- PyMuPDF fallback 无 item-level bbox/charspan，不能生成 evidence；
- section 对齐采取保守拒绝，可能牺牲召回率；
- PDF 中连字符、ligature、OCR 修复后的文本只按解析资产事实核验，不能声称等同于 PDF 字节内容。

## 5. 推荐架构与已实现模块

```text
Literature parsed JSON + Markdown + pipeline state
  -> ProvenanceDocumentReader
  -> section/paragraph/sentence reconstruction
  -> deterministic candidate detection
  -> configurable LLM classification + exact-span proposal
  -> mandatory exact-span/provenance validation
  -> strategy/template/phrase abstraction by evidence_id
  -> deterministic quality/risk validation
  -> lexical deduplication and clustering
  -> immutable run artifacts
  -> Markdown review + append-only review events
  -> accepted snapshot
  -> isolated candidate collection（显式操作）
```

实现映射：

- provenance reconstruction：`writing_rag/provenance.py`；
- schema、taxonomy、exact span、风险、评分和聚类：`writing_rag/materials.py`；
- provider、缓存、增量状态和 extraction run：`writing_rag/extract.py`；
- review、accepted snapshot 和 candidate index：`writing_rag/review.py`；
- CLI：`cli/writing_material.py`；
- governance chain validation：`governance/validation.py:HubValidator.writing_material_run`；
- 配置：`configs/writing_materials.yaml`、`configs/writing/*`。

## 6. 数据 schema

### 6.1 Evidence

Evidence 是不可变事实记录，主要字段为：

- identity：`evidence_id`、`schema_version`、`language`；
- source：`document_id`、`zotero_item_key`、`attachment_key`；
- versions：source content fingerprint、parse fingerprint、parser name/version、provenance coverage；
- section：section ID/title/path/family；
- location：page range、paragraph ID、sentence IDs、paragraph-relative character range；
- source spans：Docling `self_ref`、item range、paragraph range、page、bbox；
- evidence：`original_text`、source paragraph hash；
- analysis：category、claim strength、risk flags/source、confidence、quality；
- trace：provider、model、prompt version/hash、request/response hash、response schema 和 taxonomy version。

`original_text` 不允许被 abstraction 或 review edit 覆盖。若原文或定位错误，只能 rejected 并重新提取。

### 6.2 Strategy

保存 evidence IDs、category、策略标签、论证步骤、适用条件、claim-strength guidance、中英文解释、风险和完整 analyzer/version trace。不得复制整段论文原文。

### 6.3 Template

保存原语言 template、typed semantic slots、required/optional 标记、约束、claim-strength guidance、evidence IDs 和版本 trace。英文 evidence 不自动翻译为中文 template。

### 6.4 Phrase

保存短语或连接构件、function、句中 position、register、claim strength、约束、evidence IDs 和版本 trace。Phrase 与完整句式分开索引。

### 6.5 Review event

`review-events.jsonl` 语义上 append-only。每条记录包含：

- `decision_id`、`asset_id`、`asset_type`；
- `based_on_hash`；
- `accepted | edited | rejected`；
- reviewer、timestamp、reason；
- 受 allowlist 限制的 edits；
- review schema version。

Evidence 不允许 `edited`。Accepted snapshot 由最新事件 materialize，不手工覆盖 extraction 文件。

## 7. Taxonomy v1

完整 19 类：

1. `concept_introduction`
2. `concept_definition`
3. `context_setting`
4. `importance_claim`
5. `problem_statement`
6. `gap_identification`
7. `prior_work_limitation`
8. `motivation`
9. `incremental_novelty_positioning`
10. `contribution_summary`
11. `design_rationale`
12. `mechanism_explanation`
13. `transition`
14. `comparison_with_prior_work`
15. `result_reporting`
16. `result_interpretation`
17. `ablation_interpretation`
18. `limitation_acknowledgment`
19. `future_work`

MVP 默认启用 12 类：context、importance、problem、gap、prior limitation、motivation、incremental novelty、contribution、result reporting/interpretation、limitation 和 future work。

Risk flags：

- `unsupported_superlative`
- `exaggerated_novelty`
- `vague_claim`
- `missing_comparison`
- `causal_overclaim`

每个风险来源标为 `deterministic_heuristic` 或 `model_assessment`，不冒充外部事实核查。Incremental novelty 必须限定比较对象、范围或条件，不能自动升级成“首次”“最佳”或“突破”。

## 8. LLM 输入输出契约

真实 adapter 使用仓库已有 `httpx` 调用 OpenAI-compatible `/v1/chat/completions`；不增加 SDK。Base URL 应配置为 provider origin，不包含重复的 `/v1`。API key、base URL 和 model 均可配置，secret 只从环境读取。classification/abstraction token limits、classification batch size、provider timeout 和 retry policy 都进入 version manifest/bundle；修改任一执行预算都会使旧 approval 失效。另有必须显式配置的 `deterministic_fixture` provider，用于小型无网络 E2E；它使用固定 model identity、authoritative sentence ID 和合成 abstraction，不可作为真实质量评估结果。

Classification 输入只包含限定 section、authoritative sentence ID/text、enabled taxonomy 和版本。`classification-v1/v2` 要求模型生成 start/end/original_text，真实运行出现文本正确但 offset 错误；`classification-v3` 改为 sentence ID 列表后仍出现未知 ID 和非连续组合；`classification-v4` 收紧为动态 enum 的单 sentence ID；`classification-v5` 把风险数组改为完整闭合布尔映射；`classification-v6` 在应用层拒绝重复 sentence/category pair；`classification-v7` 只暴露 provenance 完整句子，并以动态闭合 object 保证结构唯一。两次v7 run耗尽4096与8192生成预算，推动v8使用每句共享decision和完整category boolean map；真实v8首篇又生成全false map，虽然parser正确拒绝，但provider schema未能在解码期排除该状态。当前 `classification-v9` 保留动态 sentence ID object和共享decision，`category_decisions` 改为至少一个selected category key，每个出现值恒为true，遗漏键表示false。provider schema以closed properties、`minProperties=1`和`const=true`排除empty/false/unknown，同时比v8完整map更短。本地将类别键展开，join回唯一source paragraph/sentence，并继续exact-span和source revalidation。旧v1–v8 evidence按各自run manifest只读重验，新请求只生成v9：

请求分片不再只按paragraph计数。`writing-material-request-partition-v1`同时限制每个classification请求最多4个paragraph slices和8个authoritative sentences；单个长paragraph只切分暴露给provider的sentence tuple，不修改paragraph text、ID、offset或provenance。abstraction每次最多8条verified evidence。分片版本和上限进入version bundle；dry-run报告实际request count与observed maximum。classification中途失败时整个文档的临时evidence回滚，避免失败文档部分结果混入durable checkpoint。

```json
{
  "schema_version": "classification-v9",
  "items": {
    "sentence:...": {
      "category_decisions": {
        "gap_identification": true,
        "prior_work_limitation": true
      },
      "claim_strength": "cautious",
      "risk_flag_decisions": {
        "unsupported_superlative": false,
        "exaggerated_novelty": false,
        "vague_claim": true,
        "missing_comparison": false,
        "causal_overclaim": false
      },
      "confidence": 0.91
    }
  }
}
```

Abstraction 只接收已验证 evidence IDs 和 immutable evidence。其 schema 不存在 `original_text` 字段；输出 strategy/template/phrase。`abstraction-v1` 的 strategy 使用风险数组；`abstraction-v2` 改为 closed boolean map；`abstraction-v3` 增加动态 evidence-ID enum 与引用类别规则。当前 `abstraction-v4` 进一步让 provider schema 的每个 maxLength/maxItems 与 parser/stored validator 精确同构，category enum 动态收窄到当前 evidence 类别，并拒绝重复 material payload。review 从磁盘重读时再次验证 category-reference。本地只将 true 风险键派生为 material 的 `risk_flags`，不会静默去重、删除引用或修改 evidence：

```json
{
  "schema_version": "abstraction-v4",
  "strategies": [{
    "risk_flag_decisions": {
      "unsupported_superlative": false,
      "exaggerated_novelty": false,
      "vague_claim": true,
      "missing_comparison": false,
      "causal_overclaim": false
    }
  }]
}
```

所有未知字段、缺少风险键、非布尔风险值、未知 enum、非法长度、越界、重复 payload 或语义不受支持的引用均拒绝。历史 `abstraction-v1/v2/v3` 已保存材料继续只读可验证，新 provider 请求只生成 v4。

Provider 请求固定 temperature 0，并追踪 provider/model、prompt hash、schema/taxonomy version、request hash 和 response hash。缓存 key 还包含实际 schema 和输入；缓存文件权限为 0600。

非 dry-run CLI 在进入通用 TaskExecutor 之前执行只读 authorization precondition，验证 approval 与当前 selection/section/Literature checkpoint/provider/model/version bundle 完全一致，并验证 provider origin 结构；失败不得创建 task、run、state 或 cache。Extraction service 在持久化边界重复验证，避免 TOCTOU 或绕过 CLI 时失去门禁。

## 9. Exact-span 验证

每个 proposed span 必须依次通过：

1. `[start,end)` 是有效 Python Unicode string offset；
2. `paragraph.text[start:end] == original_text`；
3. paragraph hash 和 parse fingerprint 未变化；
4. span 每个字符均被 segment map 覆盖；
5. 每个 source span 有 Docling self-ref、item range、page 和 bbox；
6. sentence IDs 与 span 有一致交集；
7. 目标章节的字符级 provenance coverage 和 section alignment 已通过 gate。

失败记录进入 `failures.jsonl`，不做 fuzzy search、空白修复、近似替换或模型猜测。单个 span 失败不会覆盖同批其他有效 evidence。

## 10. 质量评分和去重

硬门槛：exact-span 和 provenance 必须有效。

软评分：

- classification confidence：20%；
- transferability：20%；
- context independence：15%；
- completeness：15%；
- language/structure quality：15%；
- section-category consistency：15%；
- 每个 risk flag 扣 0.05，最多扣 0.25。

低于配置阈值（默认 0.65）的记录以 `low_quality_candidate` quarantine，不进入审核资产。

去重不删除 evidence，只分配稳定 cluster ID：

- 按 asset type、category、language 隔离；
- exact/near duplicate 使用标准库 token/character shingles；
- Jaccard 默认阈值 0.85；
- template 比较前统一 slot 表示；
- cluster ID 由成员 ID 的稳定排序生成。

MVP 不依赖 embedding 做聚类。

## 11. 增量、缓存和恢复

独立 state 位于 `<writing-material-data-root>/state/extraction.sqlite3`，不修改 Literature schema。

- `new`：无历史状态；
- `changed`：source content、parse fingerprint 或 version bundle 改变；
- `failed`：仅在 `--retry-failed` 时重试；
- `unchanged`：跳过；
- `inactive`：显式 selection 中源已不可用，保留历史记录；恢复后重新处理。

Version bundle 包含 reconstruction、taxonomy 内容 hash、candidate rules、两个 prompt 内容 hash、response schema、quality policy、provider/model 和 enabled categories。

每个文档独立写 attempt 状态。Run 允许 partial；已通过的 evidence 即使后续 abstraction 失败也保存在本次 immutable run 中，失败文档可利用 0600 LLM cache 重试。

TaskStore 接入：

- `writing_material_extract` / `derive:writing-materials`；
- `writing_material_review` / `review:writing-materials:<run>`；
- `writing_material_index` / `index:writing:<candidate>`。

日志和错误只保存 sanitized、bounded message，不主动记录全文或 credential。

## 12. 人工审核

每个 run：

```text
runs/<run-id>/
  manifest.json
  evidence.jsonl
  strategies.jsonl
  templates.jsonl
  phrases.jsonl
  failures.jsonl
  review.md
  review-events.jsonl        # 首次 apply 后出现
  accepted/                  # materialized snapshot
```

Reviewer 对每个 evidence 和 material 给出显式 decision。`based_on_hash` 防止对旧版本记录提交决定。Material edits 只允许 category、策略说明、template/slots、phrase guidance 和质量等 allowlisted 字段；IDs、evidence references、provenance 和版本字段不可编辑。

Material 只有在自身 accepted/edited 且引用的全部 evidence accepted 时才进入 accepted snapshot。

## 13. Candidate indexing

MVP 完成标准仍是 accepted JSONL。显式执行 index 时：

- 先运行完整 source/review chain validation；
- 只读取 `accepted/`；
- index text 由 strategy、template、phrase 和 guidance 组成；
- payload 保存 evidence IDs、定位字段和最多 240 字符 excerpt，不保存完整 evidence 字段；
- 完整原文由本地 accepted evidence store 显式 join；
- `IncrementalChunkIndexer(require_new_collection=True)` 强制新物理 collection；
- collection 与当前 active Writing collection 相同则拒绝；
- 不 prune、不 stage、不 promote。

当前通用 `CandidateReleaseManager.bootstrap-candidate` 仍依赖 Code normalized layout，不适合直接克隆 Writing。已实现的 Writing 专用 release service 从 promotion state 解析物理 active，snapshot/restore 到全新 candidate，增量合并 complete accepted assets，并验证 point count 与 dense/sparse schema。build 与 stage/promotion/rollback 分离；真实操作仍需独立确认，不能用 scoped pilot 替换正式 Writing collection。

## 14. CLI

```bash
knowledgehub writing-material extract \
  --selection papers.jsonl \
  --section introduction --section results --section discussion --section conclusion \
  --limit 50 --dry-run

knowledgehub writing-material extract --retry-failed --run-id <prior-run-id>
knowledgehub writing-material review render --run-id <run-id>
knowledgehub writing-material review apply --run-id <run-id> --decisions decisions.jsonl
knowledgehub writing-material validate --run-id <run-id>
knowledgehub writing-material index --run-id <run-id> \
  --accepted-only --candidate-collection <new-physical-name> --dry-run
knowledgehub writing-material pilot assess-dry-run --report <dry-run.json>
knowledgehub writing-material pilot evaluate-retrieval \
  --run-id <run-id> --candidate-report <candidate.json> --queries <cases.jsonl> \
  --mode sparse --output <retrieval-report.json>
knowledgehub writing-material pilot evaluate --run-id <run-id> \
  --candidate-report <candidate.json> --retrieval-report <retrieval-report.json>
```

Retry 使用 prior run 的 selection，但创建新的 immutable run，不覆盖旧 run。非 dry-run OpenAI-compatible extraction 要求配置 approved model 和 base URL 环境变量；`deterministic_fixture` 只允许作为明确标记的测试输出。

## 15. 测试策略与 MVP 步骤

自动测试覆盖：

- Docling reconstruction、稳定 ID、sentence offsets 和 Zotero identity；
- PyMuPDF、缺 bbox 和低 provenance 拒绝；
- exact-span 成功、文本不等和 closed-world schema；
- provider strict schema、invalid JSON 和 private cache hit；
- dry-run 零 state/cache/run 写入；
- new/unchanged 增量行为；
- accepted/edited/rejected 规则和 evidence immutability；
- accepted-only candidate 输入与 active collection 保护；
- lexical clustering 的稳定性与语言隔离。

建议 pilot：

1. 人工创建 30-50 篇 document ID selection；
2. 先 dry-run，统计 Docling/coverage/section gate 通过率；
3. 用已核对 fixture 和 `deterministic_fixture` provider 运行无网络 CLI 回归；
4. 配置 approved provider/model 后小批 extraction；
5. 审核 Introduction、Results/Discussion、Conclusion 的核心 12 类；
6. 生成 accepted JSONL 并复验 source；
7. 如需检索实验，仅构建新 candidate collection，并从至少 5 条批准 query 实际生成带 source-join/fingerprint 的报告；
8. 所有 pilot gate 通过后仍只进入人工扩量决策；stage/promotion 继续要求独立确认。

## 16. 风险、权衡和待决策事项

主要权衡：保守 provenance gate 会降低召回率，但避免无法可靠定位的“原文”；标准库切句和 lexical dedup 可保持零新增依赖，但不处理复杂语言现象；OpenAI-compatible schema 能隔离 provider SDK，但不同 provider 对 strict structured output 的兼容性仍需验证。

默认目标章节字符级 coverage 门槛为 0.80。未选择章节的解析缺口不会拖低本次 run；paragraph segment map 可以保留不连续但合法的区间，而所有入选 evidence 仍受逐字符完整映射硬门槛保护。该值是辅助质量门槛，不会放宽单条 evidence 的 exact-span 要求。

需要用户决定：

- provider endpoint、model 和 secret 环境变量的最终值；
- 30-50 篇 paper selection 及中英文比例；
- evidence 保留期限、访问权限和版权策略；
- reviewer 使用自由文本还是受控身份；
- pilot 后采用“克隆现有 Writing release 后增量合并”还是长期独立 Writing Materials collection；
- provenance coverage 0.80 是否应在 pilot 后提高，或按文献类型/parser version 分层。

仍无法从本轮本地实现确认：

- 在线 Qdrant 当前 alias 和 point payload 状态；
- Docling `prov.charspan` 对全部论文、OCR 和跨页自然段的一致性；
- pilot selection 中 PyMuPDF fallback/OCR/低 coverage 的实际比例；
- 最终选择的 provider/model 对 strict JSON schema 的可靠程度。

## 17. 修改和新增文件

新增：

- `docs/design/ZOTERO_WRITING_MATERIAL_PIPELINE.zh-CN.md`
- `configs/writing_materials.yaml`
- `configs/writing/taxonomy-v1.yaml`
- `configs/writing/prompts/{classify-v6,abstract-v4}.md`
- `src/knowledgehub/writing_rag/{provenance,materials,extract,review,release,pilot}.py`
- `docs/writing_material_{provenance_compatibility,release_runbook,pilot_runbook}.md`
- `src/knowledgehub/cli/writing_material.py`
- `tests/writing_material/*`

修改：

- `configs/knowledgehub.yaml`：注册独立配置；
- `src/knowledgehub/hub/config.py`：新增配置类型和 loader；
- `src/knowledgehub/cli/main.py`：注册命令组；
- `src/knowledgehub/governance/validation.py`：增加 run-chain validation。

未修改 Zotero source、Literature parser/chunker、现有 Writing derive/query、现有 Writing manifest 或正式 collection。仓库及上级目录未发现 `AGENTS.md` 或 `PLANS.md`，因此没有额外计划文件规范需要遵循。

## 18. 审查时读取的关键文件

包括根 README、Zotero source/manifests 文档、Writing RAG/V2 文档、V2 架构文档，以及实际的 Zotero sync/attachment/manifest/state、pipeline source/models/artifacts/state/orchestrator、Docling/PyMuPDF parser、structural chunker、Writing analyzer/derive/v2、incremental indexer、Qdrant、query、CLI、TaskStore、validation、logging 和相应测试。
