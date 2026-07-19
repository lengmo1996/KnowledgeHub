# Zotero 写作素材提取流水线实现状态审计

- 基线审计日期：2026-07-18；终态对账日期：2026-07-19（Asia/Shanghai）
- 审计对象：当前 `KnowledgeHub` 工作区、Git 历史、`/data/KnowledgeHub` 现有只读运行资产与本机 Qdrant
- 审计性质：实现状态、验证和后续范围审计；各阶段实际写入边界以对应补充节为准
- 状态口径：历史基线使用`IMPLEMENTED`、`PARTIAL`、`PLACEHOLDER`、`DOCUMENTED_ONLY`、`NOT_IMPLEMENTED`、`DIVERGED`、`BLOCKED`、`UNKNOWN`；当前态使用`INTERNAL_VERIFIED`、`EXTERNAL_VERIFIED`和明确保留边界

> Phase 9终态注记（2026-07-19，Phase 14C更新于2026-07-20）：本文第1–15节和其中的`PARTIAL`/`NOT_IMPLEMENTED`矩阵是实施前历史基线，不得再作为当前任务清单。当前30篇correction-v2 extraction为30/30、0失败；2496项complete review、973项accepted derived materials、retrieval/source-join、clone-and-merge production release、质量复审与acknowledgement均已闭环。用户明确保持30篇pilot、不扩量。当前权威状态为本节矩阵、Phase 16–30补充和实施计划Phase 1–14C。

## 0. 当前态完成性矩阵（更新至2026-07-20）

本节是当前代码/工件结论；后文状态表继续作为实施前历史基线。状态口径：`INTERNAL_VERIFIED` 表示实现、专项测试和适用运行工件共同证明；`EXTERNAL_PENDING` 表示代码已存在但验收必须依赖新的真实生成、人工判断或用户扩量决定；`WORKTREE_ONLY` 表示实现尚未进入 Git 历史。

| 原始要求/计划阶段 | 当前状态 | 当前权威证据 |
|---|---|---|
| provenance、Zotero item/attachment、document/section/page/paragraph/sentence identity | `INTERNAL_VERIFIED` | `ProvenanceDocumentReader`、`validate_exact_span()`、Docling envelope/version fail-closed；cross-page/Unicode/bbox/chunk-map/fallback rejection tests |
| exact source span、重复文本消歧、offset/Unicode/换行、source fingerprint 失效 | `INTERNAL_VERIFIED` | 单 authoritative sentence ID 动态 enum，本地 join/offset 派生；repeated-text、Unicode/newline、segment-gap、source drift tests |
| schema/taxonomy/prompt/model/version bundle 追踪 | `INTERNAL_VERIFIED` | classification-v9、abstraction-v7、prompt-v16、partition-v2、correction-v2、taxonomy hash 和 model/provider 全部进入 run version manifest/bundle；历史 schema 兼容矩阵 |
| 严格结构化 LLM 输入输出 | `INTERNAL_VERIFIED` | closed schemas、动态 sentence/evidence/category enum、固定风险 map、精确字段上限、unknown/duplicate/reference/category/payload rejection；非法响应一次纠正、不缓存且持续非法拒绝 tests |
| evidence 与 strategy/template/phrase 分离、evidence immutable | `INTERNAL_VERIFIED` | 独立 dataclass/JSONL、abstraction schema 无 `original_text`、review evidence edit rejection 和 immutability tests |
| new/changed/failed/retry/resume、checkpoint 与 cache invalidation | `INTERNAL_VERIFIED` | `ExtractionState`、checkpoint hash、resume/source tamper rejection、changed/parser/prompt/model/taxonomy/retry-refresh tests |
| dry-run、mock provider、approval/preflight、零写入 | `INTERNAL_VERIFIED` | deterministic fixture、30-doc mock pilot、CLI TaskStore前authorization、network-free preflight；最终30篇v9/v7 gate/bundle完整绑定 |
| review render/import、pending/accepted/edited/rejected、complete snapshot | `EXTERNAL_VERIFIED` | run `20260719T064746Z-f99463512f16` 已导入2496条reviewer授权的accepted decision；complete accepted-v2为pending=0、dependency exclusion=0且重读/source validation通过 |
| accepted-only isolated candidate、retrieval/source-join gate | `EXTERNAL_VERIFIED` | quality-v2隔离candidate 973/973、fingerprint有效；原8-case sparse report recall/MRR/source-join=1.0、duplicate=0，两条历史miss均为目标Top-1，promotion=false |
| clone-and-merge release、stage/promotion/rollback | `EXTERNAL_VERIFIED` | production已promotion至quality-v2/1107；真实rollback先切到旧134-point physical，再通过同一机制恢复quality-v2/1107，alias/Qdrant/生产query均完成切换前后读回 |
| 去重、language-scoped clustering、质量与风险评分 | `EXTERNAL_VERIFIED` | deterministic cluster/quality/risk tests；真实audit-v2为36 flagged/36 acknowledged/0 unreviewed，内容finding保留但人工review不再pending |
| 30篇当前 contract 真实 extraction | `EXTERNAL_VERIFIED` | correction-v2 run `20260719T064746Z-f99463512f16`完成30/30、0失败；1523/280/423/270资产严格重读且`source_verified=true`，旧duplicate回归样本通过 |
| 全部人工 decision、candidate 检索验收 | `EXTERNAL_VERIFIED` | 2496项complete accepted-v2；隔离candidate 973/973与8-case retrieval/source-join均通过 |
| 是否扩量 | `EXTERNAL_VERIFIED`（决定） | 用户于2026-07-19明确选择`保持当前 pilot，不扩量`；结果为`stop_at_validated_pilot`，不创建新selection、不继续extraction且不授权promotion |
| quality review versioning、receipt与acknowledgement | `EXTERNAL_VERIFIED` | versioned accepted revision `rev-2519697bb0043f04f9009e3c`、0600 receipt、audit-v2 fingerprint `c89ebb39...a85e5`；`passed=false`、`review_required=false` |
| retention/access运行治理 | `INTERNAL_VERIFIED + CURRENT_NOT_DUE` | 当前five-year active至2031-07-19、28/28 paths private、POSIX RBAC启用；1281 cache逐run scoped。Phase 14C已完成cache→release→run协调处置、双quarantine grace/purge和中断恢复；真实run因未到期只执行零写入plan |
| Git 实施历史 | `EXTERNAL_VERIFIED` | Phase 14B2提交`efd829a`和Phase 14C提交`49c12cb`均已在远端`main`；`git ls-remote origin refs/heads/main`读回`49c12cb38a9bc4a0b9d96239ec6b211fe1d2e2e8` |

Phase 1–14C已经完成，当前为`stop_at_acknowledged_quality_findings`：30篇pilot不扩量，production release为1107 points，质量receipt、运行治理和独立POSIX RBAC已验证，rollback真实切换与恢复已完成。到期处置对cache、released collection、本地引用及run双quarantine形成协调闭环；当前run未到期且未执行真实处置。语言分布仍为en=2470、zh=23、und=3；这是当前不扩量范围的代表性边界，不是自动扩量授权。

## 1. 结论摘要（实施前历史基线）

> 本节保留2026-07-18开始实施前的原始判断，用于解释后续Phase为何存在。当前结论请使用第0节和Phase 16–22，不要按本节再次执行旧任务。

此前设计并非“尚未开始”，也不能视为已经完成生产化。当前工作区存在一套可调用的 MVP 实现，能够从 Literature RAG 的 Docling 规范化资产重建 provenance，经过候选检测、结构化 LLM 分类、exact-span、抽象、审核和 accepted-only candidate indexing 完成小规模端到端运行。专项测试、全仓测试、真实运行资产重验和 Qdrant 只读检查均通过。

但这套实现及其设计、配置、测试和功能报告目前仍是未提交工作；Git 历史没有对应实施提交。真实 50 篇选择集的 run 状态为 `partial`，35 篇处理成功、13 篇失败，累计拒绝 562 个 span。正式 Writing 索引仍为 134 points，隔离 candidate 为 14 points，尚无合并、stage 或 promotion 实现。

最重要的未完成项不是继续扩大提取规模，而是先收紧状态与 schema 安全语义：

1. stored artifact 的治理验证没有重跑完整 evidence/material schema；
2. 单个 span 被拒绝后，文档仍可能记录为 `success`，导致失败候选随后的普通增量运行被跳过；
3. abstraction 失败时，代码没有像设计文档所述保留本轮已经通过 exact-span 的 document evidence；
4. `pending` 仅由“没有 event”隐式表达，审核 apply 不要求覆盖全部资产；
5. 没有 interrupted-run resume、collection 选择或正式索引合并发布路径。

## 2. 审计范围和方法

本轮读取并追踪了：

- 根 `README.md`、`pyproject.toml`、Zotero source/manifests、Writing RAG/V2、架构和实施报告；
- 当前设计、配置、taxonomy、prompt、功能测试报告；
- Zotero sync/attachment/manifest/state，Literature source/models/artifacts/state/orchestrator，Docling/PyMuPDF parser，structural chunker 和 incremental/Qdrant indexer；
- writing-material provenance、schema、provider、extract、review、candidate index、CLI、Hub config 和 governance validation；
- `tests/writing_material/*` 及相关配置回归测试；
- Git status、diff、相关路径历史；
- `/data/KnowledgeHub/rag/zotero`、`/data/KnowledgeHub/writing-materials` 的现有只读状态、run、accepted snapshot 和 candidate index 记录；
- 本机 Zotero Desktop Local API 与 Qdrant 的只读状态。

仓库及相关子目录没有发现 `AGENTS.md` 或 `PLANS.md`。没有清理、覆盖或重新格式化任何用户未提交改动。

## 3. 设计、计划和阶段记录

| 项目 | 状态 | 位置与事实 |
|---|---|---|
| 设计文档 | `DOCUMENTED_ONLY`（作为历史/设计证据） | `docs/design/ZOTERO_WRITING_MATERIAL_PIPELINE.zh-CN.md`，当前未跟踪；其中“MVP 已实现”不能作为实现证据 |
| 专门实施计划 | `NOT_IMPLEMENTED` | 未发现 `PLANS.md` 或该功能的独立计划/任务清单 |
| 阶段运行记录 | `IMPLEMENTED` | `/data/KnowledgeHub/writing-materials/runs/*`、state SQLite、LLM cache、candidate index run JSONL |
| 功能验证报告 | `PARTIAL` | `writing-material-functional-test-report.md`，当前未跟踪；其 point 数和 Qdrant 状态已在本轮重新核验 |
| 人工选择与审核输入 | `PARTIAL` | `papers.jsonl` 77 行、`decisions-functional-test.jsonl` 40 行、`review-sample-10.md`，均未跟踪且标记为临时功能测试 |
| Git 实施历史 | `NOT_IMPLEMENTED` | 相关新增模块、配置、测试和设计均不在 `HEAD`；历史只包含旧 Writing RAG/Zotero pipeline，不包含本 MVP 的实施提交 |

## 4. 当前实际数据流

```text
Zotero Web API metadata + Nutstore WebDAV attachment mirror
  -> sources/zotero/sync.py + attachments.py
  -> sources/zotero/manifest.py: documents.jsonl / delta catalog
  -> pipeline/source.py: ZoteroManifestSource
  -> pipeline/models.py: SourceDocument
  -> pipeline/orchestrator.py
  -> Docling parsed JSON + canonical Markdown + pipeline.sqlite3
  -> writing_rag/provenance.py: ProvenanceDocumentReader
  -> Docling section/item/charspan -> section/paragraph/sentence/source-span map
  -> writing_rag/extract.py: deterministic candidates
  -> OpenAI-compatible strict classification JSON
  -> materials.py: closed-world parse + exact-span + quality/risk
  -> strict abstraction JSON -> strategy/template/phrase
  -> immutable run JSONL + review.md + extraction state
  -> review.py: append-only semantic events + accepted snapshot
  -> accepted-only isolated candidate index
```

写作素材流水线不使用 Zotero Desktop Local API，也不从 Qdrant 反向恢复原文。`docs/sources/zotero.md:19-23` 明确现有 Zotero source 使用 Web API/WebDAV 而非 Local API；本轮 Zotero Desktop `127.0.0.1:23119` 拒绝连接，因此 Local API 不可用，但不阻塞当前流水线。

## 5. 设计要求—实现证据矩阵

### 5.1 数据读取与来源

| 能力 | 状态 | 实现与验证证据 |
|---|---|---|
| 从规范化文档层读取 | `IMPLEMENTED` | `ProvenanceDocumentReader` 只读 `pipeline.sqlite3` 和 `parsed/json|markdown`（`provenance.py:117-228`）；真实 run source validation 成功 |
| 避免从向量库恢复原文 | `IMPLEMENTED` | reader 无 Qdrant 调用；`original_text` 取自 Docling paragraph slice（`materials.py:323-338`） |
| document ID | `IMPLEMENTED` | Zotero manifest 稳定格式见 `sources/zotero/manifest.py:81-95,237-245`；selection 强制 `zotero:` ID（`provenance.py:231-257`） |
| Zotero item/attachment key | `IMPLEMENTED` | manifest 和 pipeline metadata 保留；reader 缺 key 即拒绝（`provenance.py:209-228`） |
| section hierarchy | `IMPLEMENTED` | Docling body 顺序 + Markdown heading level 重建并保留 `section_path`（`provenance.py:268-353`） |
| paragraph boundary | `IMPLEMENTED` | 每个可追踪 Docling text/list item 生成稳定 paragraph ID；真实与 fixture 均验证 |
| sentence boundary | `IMPLEMENTED` | 标准库切句及 paragraph-relative offsets/ID（`provenance.py:449-469`） |
| PDF page、bbox、字符范围 | `IMPLEMENTED` | `_segments` 严格要求 page/bbox/charspan（`provenance.py:404-446`）；evidence 保存 page/range/source spans |
| paragraph/sentence 到原文位置映射 | `IMPLEMENTED` | `Paragraph.map_range` 与 `validate_exact_span` 完整覆盖检查（`provenance.py:64-84`; `materials.py:308-409`） |
| 现有 Literature chunk 到 paragraph/sentence 的双向映射 | `PARTIAL` | Literature chunk 只保留 document/page/section（`chunking/structural.py:72-115`）；新 reader 不读 chunk Parquet，无法可靠 join 既有 chunk 与 paragraph/sentence |
| PyMuPDF/OCR provenance | `NOT_IMPLEMENTED` | MVP 明确拒绝非 Docling parser（`provenance.py:166-171`）；fixture 验证该拒绝 |

### 5.2 提取流水线

| 能力 | 状态 | 实现与验证证据 |
|---|---|---|
| section/paragraph reconstruction | `IMPLEMENTED` | `ProvenanceDocumentReader.load`, `_paragraphs`; fixture 与 516 条真实 evidence 重验 |
| candidate detection | `IMPLEMENTED` | `detect_candidates`（`extract.py:884-909`），按 section、长度和确定性信号筛选 |
| rhetorical-function classification | `IMPLEMENTED` | `OpenAICompatibleAnalyzer.classify` + strict response parser（`extract.py:209-242`; `materials.py:253-305`） |
| exact evidence span | `IMPLEMENTED` | 程序切片、hash、完整 source-map、sentence overlap gate（`materials.py:308-409`） |
| strategy abstraction | `IMPLEMENTED` | `parse_abstraction_response`/`_parse_strategy`; fake E2E 与真实 run 有 138 条 |
| reusable template | `IMPLEMENTED` | strict template/slot schema；真实 run 有 128 条 |
| phrase extraction | `IMPLEMENTED` | strict phrase schema；真实 run 有 163 条 |
| quality validation | `IMPLEMENTED` | hard exact/provenance gate + `quality_score`; 低质量记录隔离为 failure |
| risk flags | `PARTIAL` | 五类 flag 均实际合并模型与规则结果（`materials.py:420-432`）；确定性规则仅覆盖有限英文表达 |
| deduplication/clustering | `IMPLEMENTED` | 不删除 evidence，按 type/category/language 聚类（`materials.py:622-681`）；稳定性测试通过 |
| human review | `PARTIAL` | render/apply/materialize 均可调用；pending 隐式且不强制全覆盖，实际事件只有 accepted |
| writing-material indexing | `IMPLEMENTED`（candidate） | accepted-only、新物理 collection、无 promotion（`review.py:504-571`）；真实 14-point candidate green |
| 正式 Writing release 合并/发布 | `NOT_IMPLEMENTED` | 无 clone active + merge、stage、promotion；正式 collection 仍为 134 points |

### 5.3 数据资产和 schema

| 能力 | 状态 | 实现与验证证据 |
|---|---|---|
| evidence/strategy/template/phrase 分离 | `IMPLEMENTED` | 四套 dataclass、独立 JSONL 和 ID namespace（`materials.py:78-227`） |
| evidence provenance 必需字段 | `IMPLEMENTED` | document/key/section/page/paragraph/sentence/source text/range/source spans 均存在；真实字段审计确认 |
| schema/taxonomy/prompt/provider/model/source versions | `IMPLEMENTED` | record trace + run `versions` + version bundle（`extract.py:129-154,551-575`） |
| extractor version | `PARTIAL` | reconstruction/candidate/prompt/quality 等组件版本存在，但没有独立 `extractor_version` 字段 |
| processing timestamp | `PARTIAL` | run manifest 有 checkpoint/finished time，单条 evidence/material 没有 processing timestamp |
| review status | `PARTIAL` | 状态在 review event/accepted snapshot 中表达，原始 asset 没有显式 `pending/accepted/edited/rejected` 字段 |
| stored artifact schema 重验 | `PARTIAL` | LLM 输出创建时严格验证；`review.validate` 重读时主要检查 ID、计数、引用和 source，不重跑完整字段/enum/schema validator（`review.py:157-233,235-249`） |

### 5.4 taxonomy 和风险控制

| 能力 | 状态 | 实现与验证证据 |
|---|---|---|
| 完整 19 类 taxonomy | `IMPLEMENTED` | code 与 `configs/writing/taxonomy-v1.yaml` 同时定义并在 config validate 时逐项相等（`extract.py:117-124`） |
| taxonomy 实际使用 | `IMPLEMENTED` | classification enum 受 enabled subset 约束，abstraction category 受完整 taxonomy 约束 |
| MVP 默认覆盖 | `PARTIAL` | 仅启用 12 类；其余 7 类存在 schema，但默认 run 不分类 |
| 五类 risk flag | `IMPLEMENTED` | code/config、strict enum、模型结果和 deterministic detector 均接入 |
| 中英文风险规则等价性 | `PARTIAL` | 当前 regex 主要是英文；中文主要依赖模型 assessment，缺确定性对等规则和测试 |

### 5.5 LLM 接口

| 能力 | 状态 | 实现与验证证据 |
|---|---|---|
| provider/model/base URL/API key 配置 | `PARTIAL` | model/base/API key env 均可配且 secret 不进 YAML；当前只实现 `openai_compatible`，API key 可为空以支持本地服务（`extract.py:86-127,185-204`） |
| prompt 版本化 | `IMPLEMENTED` | prompt 文件、内容 hash、`PROMPT_VERSION` 与 version bundle 均保存 |
| strict structured output | `IMPLEMENTED` | `response_format=json_schema`, `strict=true`，随后再做 closed-world Python validation（`extract.py:280-347`） |
| 非法输出拒绝 | `IMPLEMENTED` | unknown fields、invalid enum/range/reference/JSON 均拒绝；真实 run 记录 12 个结构化 provider failure |
| 重试与永久错误区分 | `IMPLEMENTED` | 408/409/425/429/5xx 和连接类重试；ReadTimeout/invalid JSON/schema 不自动重放（`extract.py:355-381`） |
| mock/fake provider | `IMPLEMENTED` | FakeAnalyzer E2E 与 `httpx.MockTransport` provider tests |
| dry-run 不访问 LLM/不写 state | `IMPLEMENTED` | code 在 analyzer 创建前返回；fixture 与真实 data-root 时间戳/文件计数复验 |
| 测试不访问真实服务 | `IMPLEMENTED` | writing-material tests 全部使用 fake/mock；专项 22 tests 通过 |

### 5.6 exact-span 验证

| 能力 | 状态 | 实现与验证证据 |
|---|---|---|
| `original_text == paragraph[start:end]` | `IMPLEMENTED` | 字节级 Unicode string slice equality（`materials.py:323-330`） |
| start/end、hash、完整 provenance 覆盖 | `IMPLEMENTED` | 越界、hash、page/bbox、coverage、sentence overlap 均为 hard failure |
| 重复文本 | `PARTIAL` | 明确 offset 可定位具体区间，不做字符串搜索；缺重复文本专门测试 |
| Unicode/换行 | `PARTIAL` | Python string offsets 保留原值并对任何变化拒绝；缺 Unicode normalization 与跨换行专门测试 |
| 连字符/ligature/OCR 差异 | `PARTIAL` | 采取保守拒绝而非修复；没有覆盖真实版本差异的 fixture corpus |
| 无法唯一定位时拒绝 | `IMPLEMENTED` | 不做 fuzzy match；无完整 segment map 即拒绝 |
| 改写误存为 evidence | `IMPLEMENTED` | 模型改写文本无法通过 exact slice；fixture 明确验证 |
| strategy/template 污染 evidence | `IMPLEMENTED` | abstraction schema 禁止 `original_text`，review 禁止编辑 evidence |

真实 run 的 562 次 exact-span 拒绝中，549 次为文本不精确相等、13 次为 provenance 覆盖不完整。这证明 gate 实际生效，也表明 provider 的 offset 可靠性仍是主要召回瓶颈。

### 5.7 增量、manifest 和恢复

| 能力 | 状态 | 实现与验证证据 |
|---|---|---|
| new/changed/unchanged/failed/retry/inactive | `IMPLEMENTED` | `ExtractionState.disposition` 与 `--retry-failed`（`extract.py:397-483,614-670`）；真实 retry dry-run 计划 2 个 failed document |
| schema/taxonomy/prompt/model 变化触发 | `IMPLEMENTED` | version bundle 包含内容 hash、schema、provider/model、limits/categories；代码路径明确 |
| stale output 显式标记 | `NOT_IMPLEMENTED` | 旧 immutable run 保留，但 state/accepted/index 没有统一 stale lifecycle |
| interrupted run resume | `NOT_IMPLEMENTED` | `--run-id` 只复用 prior selection 并创建新 run，不恢复 `running` run |
| 文档成功/部分失败语义 | `DIVERGED` | span failure 被记录后仍可走到 `state.record(status="success")`（`extract.py:747-767,833-869`），可能让未提取成功的候选被后续普通增量跳过 |
| abstraction 失败后的 evidence 保留 | `DIVERGED` | 设计文档声称已通过 evidence 会保留；异常分支 checkpoint 只写先前全局列表，没有合并本 document evidence（`extract.py:801-830`） |
| run 完成状态 gate | `PARTIAL` | manifest 有 running/success/partial/finished；`review.validate` 不拒绝 `running` 或 `partial` manifest，validation success 表示链完整而非提取完整 |
| 重复写入/commit ordering | `IMPLEMENTED` | run immutable、manifest 最后写、document assets 先于 state commit；中断测试通过 |

### 5.8 人工审核

| 能力 | 状态 | 实现与验证证据 |
|---|---|---|
| accepted/edited/rejected | `IMPLEMENTED`（代码） | event schema 与 allowlist 实现（`review.py:20-68,302-430`） |
| pending | `PARTIAL` | 没有 event 即 pending，但未作为显式状态保存或输出 |
| 审核前禁止 candidate index | `IMPLEMENTED` | index 必须有 accepted snapshot，并先 source/review validation |
| edited 保留模型原始输出 | `IMPLEMENTED` | run JSONL 不变，accepted copy 保存 `reviewed_from_hash`; 原始内容可回溯 |
| evidence 不可编辑 | `IMPLEMENTED` | edited evidence 被拒绝，source 在 apply/index 前重新验证 |
| reviewer、时间、理由、修改记录 | `IMPLEMENTED` | append-only 语义 event 完整保存；实际 run reviewer 为 `lengmo` |
| Markdown/JSONL 审核与重新导入 | `IMPLEMENTED` | `review render` + `review apply --decisions` |
| 全量 decision completeness | `NOT_IMPLEMENTED` | `apply` 接受任意非空 decision 子集，不要求每项都得到显式决定 |
| edited/rejected 实际验证 | `PARTIAL` | 实际 40 events 全为 accepted；测试仅覆盖 accepted 和 evidence-edit rejection，未覆盖 material edit、reject、latest-event 冲突等完整状态矩阵 |

实际 run 中 516 evidence 只有 10 accepted、506 implicit pending；strategy/template/phrase 各有 10 accepted event，但因 evidence dependency gate，accepted snapshot 最终只包含 2/3/9 条 derived assets。

### 5.9 CLI、配置和任务入口

| 能力 | 状态 | 实现与验证证据 |
|---|---|---|
| 明确命令入口 | `IMPLEMENTED` | `knowledgehub writing-material {extract,review,validate,index}` 已注册并通过 `--help` 验证 |
| 单文档 | `PARTIAL` | 可用单行 selection JSONL，缺直接 `--document-id` |
| 小批量 | `IMPLEMENTED` | `--limit` 和显式 selection；真实 `--limit 5 --dry-run` 成功 |
| 指定 Zotero collection | `NOT_IMPLEMENTED` | CLI 没有 collection 参数，也不从 source collection manifest 展开 selection |
| section 过滤 | `IMPLEMENTED`（MVP） | introduction/results/discussion/conclusion，内部归一到三类 family |
| dry-run | `IMPLEMENTED` | extraction 与 candidate index 均支持并实测零生产写入 |
| resume | `NOT_IMPLEMENTED` | 没有从 interrupted checkpoint 继续 |
| 只提取、不索引 | `IMPLEMENTED` | extract 默认只产出 run，不自动 index |
| 生成/导入审核材料 | `IMPLEMENTED` | render/apply |
| accepted-only indexing | `IMPLEMENTED` | `--accepted-only` 为 required flag，indexer 只读 accepted snapshot |
| 默认安全性 | `IMPLEMENTED` | selection 显式、真实 extract 需 provider 配置、index 需新物理名、禁止 active/alias、无 promotion |

### 5.10 测试覆盖

| 测试目标 | 状态 | 证据/缺口 |
|---|---|---|
| schema validation | `PARTIAL` | classification unknown field、provider schema 和 abstraction fake E2E；缺 stored artifact 全字段重验 |
| taxonomy validation | `PARTIAL` | runtime config 会比对 code/config；缺无效 taxonomy/version 专门测试 |
| exact-span/provenance | `IMPLEMENTED` | exact mismatch、segment gap、Docling identity、fallback/no bbox 拒绝 |
| duplicate detection | `IMPLEMENTED` | stable cluster + language scope |
| manifest 状态转换 | `PARTIAL` | new/unchanged、checkpoint-before-state；缺 failed/changed/inactive/stale/resume 完整状态机测试 |
| prompt/model 版本变化 | `PARTIAL` | 代码进入 version bundle；缺回归测试 |
| invalid LLM output/provider failure | `IMPLEMENTED` | invalid JSON、read timeout、503 retry、strict schema/cache |
| review state | `PARTIAL` | accepted/evidence immutable；缺 material edited、rejected、pending/completeness |
| accepted-only indexing | `IMPLEMENTED` | active collection guard、UUID point ID、accepted-only payload/dry-run |
| CLI dry-run | `PARTIAL` | 本轮真实 CLI 手动验证；自动测试调用 service，不是 CLI dispatch |
| 小型端到端 fixture | `IMPLEMENTED` | fake provider 从 Docling fixture 到 accepted candidate index dry-run |
| 真实外部 provider | `BLOCKED` | 本轮按指令禁止真实 LLM；现有历史 run 提供结果与 failure 证据 |

## 6. 当前端到端可运行范围

结论：**可以完成受限的小规模端到端运行，但尚不具备无监督生产全量运行条件。**

已经实际验证：

- fake provider fixture：Docling reconstruction -> extraction -> review -> accepted snapshot -> candidate index dry-run；
- 真实现有 run：50 selected，47 planned，35 processed，13 failed，516 evidence、138 strategy、128 template、163 phrase，run 状态 `partial`；
- source/review revalidation：516 evidence 全部重新匹配当前 Literature source，结果 `success`；
- 临时审核：10 evidence accepted，accepted snapshot 生成 14 derived assets；
- candidate dry-run：14 selected/indexed chunks，无 failure、无 promotion；
- 本机 Qdrant：active Writing green/134 points；candidate green/14 points；两者均 1024-d cosine + BM25；
- 真实 extraction dry-run：5 documents，state/run/cache 计数与 mtime 前后不变；retry-failed dry-run 能计划失败文档。

没有执行：真实 LLM 新请求、全库扫描、正式索引写入、candidate 重建、stage、promotion 或任何生产状态清理。

## 7. 已完成、部分完成和未实现能力

### 已完成

- 稳定 Zotero document/item/attachment identity 与规范化 Literature 输入；
- Docling-only section/paragraph/sentence/page/bbox/charspan provenance；
- strict classification/abstraction contracts；
- exact-span、quality gate、五类 risk flag、四类资产、lexical clustering；
- immutable run、增量 state、private cache、failed retry；
- Markdown + JSONL review、evidence immutability、accepted dependency gate；
- isolated accepted-only candidate index，且与 active Writing collection 物理隔离；
- fake E2E、全仓回归、lint、type check 与真实 source validation。

### 部分完成

- 19 类 taxonomy 只有 12 类在 MVP 默认启用；
- 中文 risk heuristics、Unicode/换行/ligature/OCR exact-span fixture 不足；
- asset 级 processing/review lifecycle 不完整；
- review pending/completeness 和 edited/rejected 测试不足；
- stored artifact governance validation 不等同于完整 schema validation；
- real provider 能运行但历史 run 结构化输出失败率明显，需先改进 bounded batching/错误恢复；
- 现有 chunk 与 paragraph/sentence 没有双向 map。

### 未实现

- Zotero collection 到 selection 的 CLI 展开；
- interrupted run resume；
- explicit stale-output lifecycle；
- PyMuPDF/OCR evidence provenance；
- 正式 Writing release clone/merge/stage/promotion 和回滚流程；
- 专门实施计划（本轮新增后续计划除外）。

## 8. 与原设计的偏离

1. 设计称 abstraction 失败后已通过 evidence 仍保存；当前异常分支没有把本 document evidence 合并进 checkpoint。
2. 设计强调每个 evidence/material 都给出显式 decision；当前 apply 允许任意非空子集，pending 由 event 缺席隐式表示。
3. 设计把 failed/partial 安全恢复作为目标；当前 span rejection 可与 document `success` 同时发生，普通增量可能跳过这些失败候选。
4. 设计文档把当前状态写为“MVP 已实现”，但相关实现尚未进入 Git 历史，不能按已发布/已合并能力管理。
5. 设计的 MVP 完成边界是 accepted JSONL；实际已进一步构建真实 Qdrant candidate，但正式 release integration 仍未实现。

## 9. 缺陷和回归风险

| 优先级 | 风险 | 后果 |
|---|---|---|
| P0 | span rejection 后 document 仍可标记 success | 失败候选可能被永久当作 unchanged 跳过 |
| P0 | stored artifact validation 不重跑完整 schema | 手工损坏或版本漂移的 material 可能在较晚阶段才失败 |
| P0 | review/index gate 不检查 extraction manifest 是否 finished/可接受状态 | interrupted 或 partial run 的“validation success”容易被误解为 extraction success |
| P1 | abstraction failure 丢失本 document 已验证 evidence | 与设计不符，降低可恢复性并浪费 LLM 成本 |
| P1 | pending 隐式、decision 不要求全覆盖 | accepted snapshot 可以在大量未审记录存在时被生成，报告语义不直观 |
| P1 | provider 输出经常产生 offset mismatch/截断 JSON | 真实 run 549 次 text mismatch、12 次 provider failure，规模扩大后失败成本上升 |
| P1 | 没有 resume/stale lifecycle | 中断和版本升级只能新建 run，人工判断哪些旧资产仍有效 |
| P2 | code/config 双份 taxonomy 常量 | 修改时可能漂移；当前只在 config load 时发现 |
| P2 | 未提交实现与生产 `/data` 资产并存 | checkout 无法复现生产资产所对应的精确 Git revision |

## 10. 验证命令和结果

| 验证 | 结果 |
|---|---|
| `python -m pytest tests/writing_material tests/multi_rag/test_core.py -q` | `22 passed in 0.66s` |
| `python -m pytest -q` | `397 passed in 15.53s` |
| `python -m ruff check .` | passed |
| `python -m mypy src` | passed，127 source files |
| CLI `--help` | extract/review/validate/index 入口均可解析 |
| Hub config load | provider/model、12 categories、3 section families、version bundle 正常 |
| 真实 `extract --limit 5 --dry-run` | success/planned；无 run/cache/state 修改 |
| 真实 `--retry-failed --run-id ... --limit 5 --dry-run` | 2 failed planned，3 unchanged skipped |
| 真实 run source validation | success，516 evidence source verified |
| 真实 candidate index dry-run | success，14 selected/14 chunks，promotion=false |
| Qdrant collection status | active green/134；candidate green/14 |
| Zotero Desktop Local API | connection refused；当前 Web API/WebDAV pipeline 不依赖该接口 |

## 11. 无法或未执行的验证

- 按本轮边界未调用真实 LLM，因而没有重新测量当前 provider/model 的 structured-output 成功率；
- 未处理完整 Zotero 库；
- 未写入、删除、stage 或 promote 任何 Qdrant collection；
- 未验证 Docling 所有版本、OCR、跨页自然段、ligature 和连字符修复的一致性；
- 未验证 candidate 查询质量报告中的三个语义查询，因为本轮重点是实现链与索引健康，且没有重建 gold set；
- Zotero Desktop Local API 未运行，但它不属于当前生产数据路径。

## 12. 推荐下一阶段范围

推荐只实施后续计划的 Phase 1：状态/schema 安全收口。不要先扩大 paper selection，也不要接入正式 Writing index。Phase 1 应完成 stored artifact strict validation、明确 finished/partial gate、修正 span failure 的 document 状态、保留 abstraction failure 前的 evidence，并补齐对应状态机测试。完成后再进入 provenance 边界 fixture 和 review lifecycle。

详细阶段、输入、输出、验收标准、schema/reprocess/index 影响见 `docs/writing_material_extraction_followup_plan.md`。

## 13. 需要用户决定的问题

1. `partial` extraction run 是否允许经过明确 waiver 后构建 candidate，还是必须先重试到无 document failure；
2. 审核是否必须 100% 显式 decision 后才能生成 accepted snapshot；
3. asset schema v2 是否加入 `processed_at`、`extractor_version` 和显式 `review_status`，并对现有 516 条 evidence 重建；
4. 长期采用独立 Writing Materials collection，还是 clone 当前 134-point Writing release 后合并；
5. 是否保留当前临时 `papers.jsonl`、decision/review/report 文件进 Git，还是迁移到受控 pilot artifact 目录并做版权/访问控制；
6. provider 的 invalid JSON/offset mismatch 应优先通过 prompt/batch 调整，还是引入二阶段 deterministic span locator（仍禁止 fuzzy evidence）。

## 14. 建议但尚未执行的修改文件

- `src/knowledgehub/writing_rag/materials.py`：stored-record validators、schema v2 字段；
- `src/knowledgehub/writing_rag/extract.py`：失败状态语义、evidence checkpoint、resume/stale lifecycle；
- `src/knowledgehub/writing_rag/review.py`：finished/partial gate、explicit pending/completeness、strict artifact revalidation；
- `src/knowledgehub/cli/writing_material.py`：document/collection selection、resume/waiver 参数；
- `src/knowledgehub/writing_rag/provenance.py`：真实边界 fixture 支持与可选 chunk map contract；
- `tests/writing_material/*`：状态机、schema migration、Unicode/PDF 差异、review 完整矩阵、CLI E2E；
- `configs/writing/taxonomy-v1.yaml` 与 prompts：仅在 pilot 证据支持后调整；
- 正式 release integration 应另建独立模块/测试，不复用当前 candidate build 直接 promotion。

## 15. 工作区说明

本轮新增的审计和计划文档之外，所有修改/未跟踪文件均为审计开始前已有用户工作。没有修改生产代码，也没有修改 `/data/KnowledgeHub` 中的生产状态、cache、run、accepted snapshot 或索引。

## 16. Phase 7 复审补充（2026-07-19）

本节保留上文历史审计原文，并以当前代码和真实运行证据覆盖其中已经过时的“正式 Writing release 未实现/未执行”结论。

### 16.1 状态更新

- **IMPLEMENTED + VERIFIED**：CJK sparse bigram、独立 `sparse_text`、sparse preprocessing fingerprint、writing asset type query filter；实现提交 `0d743f7`。
- **IMPLEMENTED + VERIFIED**：Writing clone-and-merge release build、snapshot、manifest validation、显式 stage/promotion/rollback；本轮已实际 stage/promote，不再属于“未实现”。
- **EXTERNAL_VERIFIED**：`knowledgehub_writing_current` 当前指向 `knowledgehub_writing_release_20260719_f99463512f16_quality_v2`，Qdrant green/1107 points；旧 `knowledgehub_writing_qwen3_4b_1024_v1` green/134 points并保留为 fallback。
- **VERIFIED**：accepted-only merge 为973/973、source verified、0 failures；release manifest fingerprint 为 `cceb6d67322488b48d0fd5719073e5cb95968fc5f518d2b27bfa9fe1bb083087`。
- **VERIFIED**：原8条 gold cases 在 release candidate 上全部目标Top-1，Recall@5=1.0、MRR=1.0、source join=1.0、duplicate=0；production alias 对两条历史 miss 的目标 template 也均为Top-1。

### 16.2 审计偏差

第一次 `quality_v1` release build 返回 `status=validated` 且生成了合格 manifest，但 `TaskExecutor._terminal_status` 当时只接受 `success/planned/skipped/completed/available`，导致任务账本误记 failed。修复提交 `a153992` 将 `validated` 映射为 completed，并由 `test_executor_records_validated_release_as_completed` 锁定。错误历史记录未被覆盖；使用新的 `quality_v2` 物理 collection 重建后，任务 `03422846-9e9b-4359-8ec4-b0cacad0365b` 正确为 completed。

### 16.3 本次验证与安全边界

| 验证 | 结果 |
|---|---|
| `python -m pytest -q` | `535 passed in 27.57s` |
| `python -m ruff check .` | passed |
| `python -m mypy src` | passed，129 source files |
| release candidate | green/1107，dense 1024 cosine + BM25 |
| production alias | `knowledgehub_writing_current` -> `...quality_v2` |
| rollback fallback | 旧 physical green/134 + 发布前 snapshot保留 |

本阶段没有调用真实 LLM，没有重新 extraction，没有扩大30篇 pilot，没有处理完整 Zotero 库，没有删除旧索引、candidate、snapshot、manifest、cache或审核结果。生产变更仅为用户明确授权的 stage/promotion；原有 Zotero RAG、Code/技术知识库集合和旧 Writing physical collection未被覆盖。

## 17. Phase 8 accepted corpus 质量复审（2026-07-19）

### 17.1 新增能力

- **IMPLEMENTED + VERIFIED**：`AcceptedCorpusQualityAuditor` 对 complete/source-verified accepted-v2 snapshot 进行确定性、无 LLM 的 corpus-level 审计。
- **IMPLEMENTED + VERIFIED**：NFKC/whitespace/casefold 规范化、低质量分、重复片段、超长字段、重复列表项、精确主文本重复和多成员 lexical cluster 检测。
- **IMPLEMENTED + VERIFIED**：`writing-material pilot audit-quality` 生成0600、fingerprinted、无 source/material text 的报告；不修改 review、accepted 或 index state。

### 17.2 当前30篇结果

报告 fingerprint：`af061ab96e18ffec3d9de059ae4424bd0033967b04c1f8c8a44db52a5ac289d0`。

| 指标 | 结果 |
|---|---|
| assessed material | 973（strategy 280、template 423、phrase 270） |
| flagged assets | 36，3.6999% |
| repeated text segment | 6 errors；最大重复30次 |
| oversized field | 26 warnings |
| quality score `<0.75` | 8 warnings，0.8222% |
| multi-member lexical cluster | 2组/4 assets，0.4111% |
| exact primary-text duplicate | 0 |
| repeated list item | 0 |

因此 quality audit `passed=false`，建议 `manual_review_flagged_assets`。这不会否定 exact-span/source-join 或原8条 retrieval gate，但说明此前“全部 accepted”不能等同于逐项内容质量验证；当前 production 中确实包含需要复核的 derived material。

### 17.3 验证与边界

| 验证 | 结果 |
|---|---|
| `python -m pytest -q` | `539 passed in 27.65s` |
| `python -m pytest tests/writing_material/test_quality_audit.py -q` | `4 passed` |
| `python -m ruff check .` | passed |
| `python -m mypy src` | passed，129 source files |

没有调用 LLM、没有扩大selection、没有修改evidence、review events、accepted snapshot、Qdrant collection/alias、manifest或cache。现阶段不自动回滚或过滤生产结果；下一阶段先生成36项人工复核包，任何 edit/reject 和新 release 都必须保持 append-only 审核与显式 reviewer 决定。

## 18. Phase 8B 人工复核包复审（2026-07-19）

- **IMPLEMENTED + VERIFIED**：fingerprinted `writing-material-quality-review-packet-v1`，绑定quality audit、accepted manifest和immutable run asset `based_on_hash`。
- **IMPLEMENTED + VERIFIED**：0700 reviewer-local目录、0600 JSON/Markdown；包可显示派生material，但不含evidence原文/provenance excerpt。
- **IMPLEMENTED + VERIFIED**：6项重复内容提供去重edit草稿；4项cluster建议比较后keep/reject；8项低分建议人工keep/edit/reject；18项超长项建议keep/edit。
- **SAFE BY CONSTRUCTION**：36项decision/reason全部为null，`decision_import_ready=false`；没有调用review apply或修改任何审核/索引状态。

采用的v2 packet fingerprint为 `e708ad9a6e2cca6bdada5a5370b24b4466bb78c3787b11925eb014595aaaae50`，路径 `/tmp/knowledgehub-writing-material-phase6b-20260718/quality-review-v2-20260719T064746Z-f99463512f16/`。对1523条accepted evidence的完整`original_text`执行JSON/Markdown contains检查，结果为false。

第一次v1包发现确定性清理会保留位于字段末尾的已见句子截断前缀；修复规则后使用新目录生成v2，v1未覆盖或删除。全仓验证：pytest 542 passed、Ruff passed、mypy 129 source files passed。

新的实现约束：当前review events为append-only，但complete projection固定写入`accepted/`，后续event materialization会覆盖该目录。二次审核导入前必须先实现versioned accepted projection；否则不能满足“不覆盖历史人工审核结果”。因此本阶段只生成不可导入草稿，生产alias仍为green/1107且未变化。

## 19. Phase 8C 版本化 accepted 与质量决定导入复审（2026-07-19）

- **IMPLEMENTED + VERIFIED**：首个`accepted/`保持兼容和只读历史；后续审核projection进入`accepted-revisions/rev-<fingerprint>/`，不会覆盖旧snapshot。
- **IMPLEMENTED + VERIFIED**：`accepted-current.json`为0600、fingerprinted pointer；current revision由append-only review events与projection hash确定，validate会校验存在的pointer但不把它作为唯一恢复依据。
- **IMPLEMENTED + VERIFIED**：candidate/release/pilot/CLI消费者统一通过`WritingMaterialReviewService.accepted_dir()`读取当前complete snapshot。
- **IMPLEMENTED + VERIFIED**：`review apply-quality`绑定`writing-material-quality-review-packet-v1`、current accepted manifest SHA、reviewer和raw asset hash；要求所有flagged items一一具有显式合法决定。
- **SAFE BY CONSTRUCTION**：dry-run零写入；真实导入必须`--yes`；旧人工edit在二次keep/edit中显式carry forward；evidence不属于质量packet且不能被编辑；导入后旧packet自动stale。

fixture测试覆盖legacy与两个revision均不被覆盖、current pointer权限、null/缺项/stale拒绝、source revalidation和不创建index。定向writing-material回归121 passed、全仓pytest 544 passed、Ruff lint passed、mypy 129 source files passed、`git diff --check` passed。新代码只读验证生产run `20260719T064746Z-f99463512f16`仍为source verified、errors=[]、index eligible，证明无pointer的历史accepted-v2无需迁移。

本阶段没有对36项真实packet执行导入，没有调用LLM，没有扩大30篇selection，没有修改Zotero、Qdrant collection/alias、生产索引、现有run/accepted、cache或manifest。生产alias仍指向既有quality-v2 release。审计中Phase 8B所述“fixed accepted会覆盖”的实现缺口已经关闭；本节落笔时真实内容质量决定仍为**BLOCKED_ON_REVIEWER_DECISIONS**，该状态已由下节Phase 8D解除。

## 20. Phase 8D 全部 accepted 质量决定导入（2026-07-19）

- **EXTERNAL_VERIFIED**：用户明确决定36项全部accepted；0600 decisions JSONL为36条完整、无edits的`lengmo`决定，SHA-256 `618427b17e1ad1b4a2da39eb95f50bd2c3967a237ca85c4cc0c1e4bf2fa3d1db`。
- **VERIFIED**：dry-run为planned/36 accepted/source verified/zero write，fingerprint `b85063257e46d794a66d2d5f74c70aa5c41335dc77340f4af73c6fc207faefc8`。
- **EXTERNAL_VERIFIED**：Task `938aa58e-1f4d-4842-a46c-51c561d2f99e` completed并追加36 events；新revision `rev-2519697bb0043f04f9009e3c`完整、index eligible，import fingerprint `e8f75364ad257e20321e9097664995d5e90f0200bce7d34f716826a3b7955a73`。
- **VERIFIED**：legacy accepted manifest仍为`2b54fae...a065`，新manifest为`5d01160...dde1`，0600 current pointer为`cd11796...a278`；validate source errors=[]，旧packet自动stale。
- **QUALITY RISK ACCEPTED**：post-accept audit仍为36 flagged/42 findings、passed=false，fingerprint `d83d80bb48fce854b0ec69fc93e10c8c090f037e01b6ca8fd68d4ebbb5a1354d`。这是用户选择保留内容的真实结果；不得把accepted decision伪装成内容规则通过。

没有调用LLM、没有扩大selection、没有修改evidence/Zotero/cache/Qdrant或执行index/release。磁盘alias state仍记录quality-v2 active/1107；Qdrant 6333当时离线，故未完成live collection读回。人工审核阻塞已解除，当前保守终态为`stop_at_acknowledged_quality_findings`。

## 21. Phase 8E acknowledgement receipt 与 audit-v2（2026-07-19）

- **IMPLEMENTED + VERIFIED**：`writing-material-quality-review-receipt-v1`以0600 immutable artifact绑定packet/audit/decisions、逐asset decision/hash、结果accepted manifest和review events；不保存evidence或material文本。
- **IMPLEMENTED + VERIFIED**：新quality import自动写receipt；`reconcile-quality-receipt`只在historical packet/decisions与current latest events逐项完全一致时补写缺失receipt，dry-run零写入、真实操作需`--yes`。
- **IMPLEMENTED + VERIFIED**：`writing-material-quality-audit-v2`把内容`passed`与人工`review_required`解耦，并只把unreviewed assets路由到review renderer；v1 failed report保留兼容。
- **EXTERNAL_VERIFIED**：真实reconcile Task `6c1f1915-572b-4b13-8e8e-c13a448809d2` completed；receipt fingerprint `e0d81c46f04e4ccd64e321d8ad196bcfcc1905afe8fe84f4893ae83f6c9dc2b9`。
- **EXTERNAL_VERIFIED**：真实audit-v2 fingerprint `c89ebb39713ccfc724dfa1dc72121933dafbb1d234f85c68afae8eb07f1a85e5`，36 flagged/36 acknowledged/0 unreviewed，42 findings；`passed=false`、`review_required=false`、recommendation=`quality_findings_acknowledged`。

验证为定向writing-material 121 passed、全仓pytest 544 passed、Ruff passed、mypy 129 source files passed、source validation errors=[]/index eligible。没有新增依赖、LLM调用、extraction、evidence/accepted/index/alias变更；唯一真实数据写入是审核receipt和TaskStore审计记录。当前Phase 8质量闭环完成，无强制后续阶段。

## 22. Phase 9 终态状态对账（2026-07-19）

- **VERIFIED**：实施计划无未勾选任务；writing-material源码/测试范围无`TODO`、`FIXME`、placeholder或`NotImplementedError`分支。
- **VERIFIED**：Git工作区在Phase 8E提交后干净；当时实现和运行记录已有9个本地提交，本终态文档提交后为第10个；均尚未push，不把本地ahead误写成已合并远端。
- **VERIFIED**：current accepted manifest `5d011605...dde1` source validation errors=[]/index eligible；audit-v2 `c89ebb39...a85e5`为36 acknowledged、0 unreviewed。
- **HISTORICAL OBSERVATION**：Phase 9受限sandbox内的localhost探测不能连接6333，当时保守地没有新增live声明。Phase 10使用获准的只读CLI/localhost探测确认该判断是网络隔离造成的false negative；磁盘alias state本身始终为quality-v2 active/1107。
- **HISTORICAL ONLY**：第1–15节中的WORKTREE_ONLY、生产134 points、未实现release/resume/review completeness等结论已被Phase 1–8实现与真实运行证据覆盖；保留原文只用于决策追溯。
- **INTENTIONAL BOUNDARIES**：非Docling/OCR输入继续fail closed；默认12类taxonomy、单一OpenAI-compatible provider、run级processing time和raw asset不回写review status均为已记录设计选择，不是自动扩展授权。

当前没有强制开发任务。未来只有三类新工作可以重启计划：用户提供新的中文Zotero范围并批准从selection+dry-run开始扩量；用户改为edit/reject已acknowledged质量finding；或用户明确授权push/新的production release。它们都不能由“继续”自动推导为扩量、LLM或索引授权。

## 23. Phase 10 运行治理收口（2026-07-19）

- **IMPLEMENTED**：`validate_run_governance()`只读解释当前`five years`policy，基于approval timestamp计算到期时间；到期只会使validation/index eligibility失败，不自动删除run、accepted、cache或index。
- **IMPLEMENTED**：递归检查run目录树，拒绝symlink及任何group/other权限位；报告checked/private paths。`local reviewer only`明确标注为`private_filesystem_permissions`且`identity_enforced=false`，不伪装为RBAC。
- **BACKWARD COMPATIBLE**：历史自由文本retention/access policy继续可读，只产生不可自动解释warning；没有approval的历史fixture标记`not_declared`，不追溯破坏旧run。
- **EXTERNAL_VERIFIED**：真实run governance=`verified`，retention active、expires_at=`2031-07-19T06:47:32.819105+00:00`，28/28 paths private，source/review validation仍为errors=[]、index eligible。
- **EXTERNAL_VERIFIED**：稳定alias指向`knowledgehub_writing_release_20260719_f99463512f16_quality_v2`；Qdrant实时green、optimizer ok、1107/1107 points、update queue 0；生产sparse query命中新accepted-only strategy及完整source join。
- **VERIFIED**：新增正常、到期、权限漂移、历史自由文本和无approval兼容测试；writing-material 164 passed、全仓548 passed、Ruff passed、mypy 129 source files passed、`git diff --check` passed。
- **BOUNDARY**：没有执行真实rollback、自动删除、RBAC、扩量、extraction、LLM请求、审核修改或索引写入。已接受的42项质量finding继续保留，不因治理收口被静默改写。

## 24. Phase 11 生产 rollback readiness（2026-07-19）

- **IMPLEMENTED**：`release rollback --dry-run`生成closed、fingerprinted readiness报告；只读检查live alias、current/previous collections、point/schema、release manifest和snapshot，不调用promotion rollback或任何restore/write。
- **SAFE BY CONSTRUCTION**：`--dry-run --yes`拒绝；真实rollback仍由原`--yes`路径控制。alias target或schema/point/manifest漂移返回`blocked`，不能被readiness伪装成ready。
- **EXTERNAL_VERIFIED**：真实报告fingerprint `33f4505b05b97d75d113d6d6abf718009f4d9b79b68817c6effbfb215b7adc3f`，ready=true；active quality-v2=1107、previous v1=134，均green、schema一致、snapshot available。
- **ZERO WRITE VERIFIED**：演练后current active/previous/fingerprint不变，`rolled_back_at=null`；transaction仍为`operation=promote/status=committed`，没有新TaskStore、snapshot、collection或alias transaction。
- **VERIFIED**：writing-material 165 passed、全仓549 passed、Ruff passed、mypy 129 source files passed、`git diff --check` passed。
- **BOUNDARY**：唯一warning是previous collection早于writing-material专用release manifest。真实alias切换仍未授权；自动删除、独立RBAC、扩量和已accepted内容修改也未授权。

## 25. Phase 12 生产 alias 真实可逆 rollback 演练（2026-07-19）

- **EXPLICITLY AUTHORIZED**：用户明确授权真实alias rollback；执行前重新验证readiness=`ready`，fingerprint仍为`33f4505b05b97d75d113d6d6abf718009f4d9b79b68817c6effbfb215b7adc3f`。
- **EXTERNAL_VERIFIED SWITCH**：`2026-07-19T15:51:41.186457+00:00`，`release rollback --yes`把`knowledgehub_writing_current`从quality-v2/1107切到v1/134；alias-status和transaction state读回一致。生产检索样例随之从accepted-only命中变为0 hits，证明查询实际流量已切换。
- **EXTERNAL_VERIFIED RESTORE**：`2026-07-19T15:54:13.634353+00:00`，第二次同命令把alias恢复到quality-v2/1107；current/previous、release manifest与artifact fingerprint重新形成原服务方向。
- **POST-RESTORE VERIFIED**：Qdrant为green、optimizer ok、indexed vectors/points=1107、update queue=0；相同生产query再次命中accepted-snapshot-only strategy，source join/provenance完整。
- **MUTATION BOUNDARY**：两次alias transaction及`rolled_back_at`/transaction历史是本阶段唯一生产写入。没有snapshot create/recover、collection/index写入、extraction、LLM、accepted/evidence/cache/Zotero修改；最终物理集合内容和active服务目标未改变。
- **OPERATIONAL FINDING**：旧134-point集合不含该质量修复后的检索目标，切换窗口约2分32秒内该query为0 hits。真实rollback即使技术成功也会恢复旧质量水平，因此必须按维护窗口、健康查询和立即恢复步骤执行。
- **VERIFIED**：`python -m pytest -q tests/writing_material/test_release.py -k rollback`为`1 passed, 6 deselected`，`git diff --check`通过。Phase 12没有代码变化，未重复运行Phase 11已通过的549项全仓回归。

Phase 12未修改生产代码或测试代码；它使用Phase 11已测试的路径完成外部演练，并将结果记录到实施计划、审计、设计和release runbook。当前真实rollback阻塞已解除；独立RBAC、自动到期处置、已accepted质量内容修改和Git push属于后续独立阶段。

## 26. Phase 13 独立 POSIX 身份 RBAC（2026-07-19）

- **IMPLEMENTED**：`writing_rag/access.py`提供closed/fingerprinted `writing-material-rbac-v1`，认证主体来自effective POSIX username+UID而非CLI声明；六类固定角色映射七项最小权限。
- **FAIL CLOSED**：配置`rbac_policy_path`后，策略缺失、owner/mode/symlink/fingerprint/schema/role/assignment异常均拒绝所有writing-material业务命令。bootstrap要求`--yes`、当前identity匹配并拒绝覆盖。
- **CLI ENFORCED**：extract/review/index/release和pilot子命令分别映射到明确权限；validate与只读pilot使用read。authorization decision进入run governance，和0600/0700 private permission共同报告。
- **BACKWARD COMPATIBLE**：没有配置RBAC路径的历史/fixture继续工作并明确`identity_enforced=false`；当前正式配置已指定独立策略路径，不走兼容旁路。
- **EXTERNAL_VERIFIED**：真实policy owner=`lengmo:lengmo`、dir=0700、file=0600，roles=`administrator,reviewer`，fingerprint `0a3a9468b94a5548a36cda0bf9809cb47d298545ddbe1f679b091c5977d8fda9`。真实30篇validate显示subject=`lengmo`/uid=1000/read granted、governance verified、errors=[]、index eligible。
- **VERIFIED**：writing-material 174 passed、全仓558 passed、Ruff passed、mypy 130 source files passed、`git diff --check` passed；包括配置policy缺失和权限不足时业务service调用前拒绝。
- **BOUNDARY**：这是单机POSIX账户信任域，不是跨主机IdP/MFA；没有增加依赖。没有LLM、extraction、审核内容、index、collection或alias变更；唯一运行时写入是显式授权的新RBAC策略。

Phase 13消除了Phase 10所记录的`identity_enforced=false`当前缺口。历史节仍保留当时事实。剩余已授权阶段为自动到期处置、修改已accepted质量问题，完成这些本地提交后再Git push。

## 27. Phase 14A 到期处置安全闭环（2026-07-20）

- **IMPLEMENTED**：`writing_rag/retention.py`提供只读fingerprinted plan、atomic quarantine、30天grace和inventory-verified purge；CLI由RBAC retention-dispose权限、`--yes`、TaskStore lock共同控制。
- **SAFE RECOVERY**：intent先于rename落盘；rename后receipt失败可重试补记。purge重验逐文件SHA-256，宽限期、内容漂移、symlink或private permission漂移均拒绝；重复成功操作幂等。
- **REFERENCE AWARE**：扫描index-candidates、release-candidates和releases的run绑定；真实provider存在unscoped shared cache时同样阻断。不会先删run再遗留生产points/cache。
- **EXTERNAL VERIFIED**：当前run真实plan为`not_due`、expires_at=`2031-07-19T06:47:32.819105+00:00`、fingerprint `3506c5f5882cb2c1aa4936c27b9176174191bc9a0d93af8a2ae8c3892e7ada4d`、零写入。
- **FUTURE-TIME AUDIT**：同run在到期时刻的只读模拟为blocked，检测7个candidate/release引用、23个run文件和unscoped provider cache。这是Phase 14B输入，不是被忽略的warning。
- **VERIFIED**：writing-material 182 passed、全仓566 passed、Ruff passed、mypy 131 source files passed、`git diff --check` passed。
- **NO PRODUCTION MUTATION**：没有真实quarantine/purge、LLM、extraction、review、cache、index、collection或alias写入；所有破坏路径只在/tmp fixture执行。

当前自动到期处置状态为`PARTIAL`：unreferenced run闭环完成，released run的cache/index副本处置尚未完成。下一阶段Phase 14B必须建立cache retention scope，并为所有绑定collection/candidate提供验证后deindex与引用解除；完成前不得把当前run标为自动可删除。

## 28. Phase 14B1 逐 run cache retention scope（2026-07-20）

- **IMPLEMENTED**：新cache atomic write即绑定run scope和scope fingerprint；cache hit被另一run复用时追加scope。response及response hash不受scope元数据变更影响。
- **CONSERVATIVE LEGACY MIGRATION**：历史请求不能完整反推失败/空响应归属，因此所有unscoped legacy cache绑定当前获批run；避免漏删，代价仅是到期时可能清除额外可重建cache。
- **RECOVERABLE**：versioned migration intent固定target key set，部分完成可重试；旧receipt不能掩盖后来出现的unscoped entry。真实fingerprint升级第一次失败后从intent恢复，Task retry_count=1。
- **EXPIRED PURGE IMPLEMENTED**：独占scope entry删除；共享entry只去掉expired run并重算scope fingerprint。active run拒绝，invalid/unscoped metadata拒绝，操作受RBAC和derive+retention双锁控制。
- **EXTERNAL VERIFIED**：初始1281 unscoped/0 invalid，plan fingerprint `0ccd86ea...5144d9`；最终1281 scoped-to-run、0 unscoped/invalid、0 response hash mismatch、0 scope fingerprint mismatch。最终receipt `d16cde00...96cba8`。
- **VERIFIED**：writing-material 185 passed、全仓569 passed、Ruff passed、mypy 131 source files passed、`git diff --check` passed。
- **MUTATION BOUNDARY**：真实写入仅为1281个cache文件的scope元数据、versioned intent/receipts和TaskStore审计；没有修改response、调用LLM、删除cache、修改run/review/index/collection/alias。

当前未来到期阻断已从“unscoped cache + 7 references”收敛为“已知cache scope待到期purge + 7 references”。Phase 14B2必须先安全回退active alias（若仍绑定该run），再删除仅绑定该run的physical collections并隔离对应本地candidate/release工件；此步骤尚未实现或执行。

## 29. Phase 14B2 released run 安全退役（2026-07-20）

- **IMPLEMENTED**：新增`WritingMaterialReleaseRetirementService`和fingerprinted plan/intent/receipt；未到期plan不访问Qdrant且零写入，到期执行必须显式`--yes`。
- **OWNERSHIP VERIFIED**：每个candidate/release manifest必须通过run identity和artifact fingerprint，物理collection必须只由目标run引用；目录以逐文件SHA-256 inventory绑定。共享owner、alias drift、collection/inventory变化和新增owner均阻断。
- **ALIAS SAFE**：目标run为active时先使用既有原子rollback到健康独立fallback；目标run仅为previous时不再次切换alias。collection删除严格发生在live alias离开目标collection之后。
- **RECOVERABLE**：intent固定原始7目录/5 collection及inspection。rollback后、部分delete后、atomic move后或receipt前中断均可重试；promotion current中的retired previous/staged引用最后清除。
- **RBAC + LOCKED**：CLI计划/处置除`writing_material.retention_dispose`外额外要求`writing_material.release`；TaskStore同时锁定derive、writing promotion和目标run retention。
- **REAL DRY-RUN**：当前run返回`not_due`、expires_at=`2031-07-19T06:47:32.819105+00:00`、fingerprint `b0e937cf...4a295d`，明确index/LLM/write均为false。因为未到期，没有真实alias、collection或目录mutation。
- **VERIFIED**：退役/retention/release/governance定向34 passed，另有双RBAC权限测试；全仓579 passed、Ruff passed、mypy 132 source files和`git diff --check`通过。

自动到期处置仍为**PARTIAL**而不是完成：released run的引用解除已经实现，但release-reference quarantine尚缺30天grace后的inventory-verified purge，且cache purge、release retirement和run quarantine仍是三个显式命令。Phase 14C应只做这两个协调闭环，不扩量、不调用LLM、不提前执行当前未到期run。

## 30. Phase 14C 协调式自动到期处置（2026-07-20）

- **IMPLEMENTED**：`WritingMaterialRetentionCoordinator`把cache scope purge、released-run retirement和run quarantine绑定为fingerprinted、confirmation-gated、可恢复的单一处置事务；不引入daemon、队列或依赖。
- **ORDER ENFORCED**：cache未完全scoped、release plan非ready或治理漂移时不创建处置intent；真实步骤固定为cache→release→run，run不会在派生cache或索引引用仍存在时先行移动。
- **RECOVERABLE**：各子服务保留独立intent/receipt，coordinator只记录顺序和receipt fingerprint。cache已purge、部分collection/目录已处理、run已rename或coordinator receipt失败均可重试，也可接管先前安全完成的独立处置。
- **REFERENCE PURGE IMPLEMENTED**：release-reference quarantine采用独立30天grace、逐文件inventory和purge intent/receipt；部分purge恢复、数据重新出现及内容漂移均fail closed。coordinated purge等待run/reference两类grace并分别保留审计receipt。
- **CLI + RBAC**：`retention {plan-disposition,dispose,plan-disposition-purge,purge-disposition,plan-reference-purge,purge-references}`均受retention-dispose与release双权限控制；mutation使用derive、promotion、per-run retention锁。
- **CURRENT EXTERNAL DRY-RUN**：真实run为`not_due`，三个步骤均未启用，fingerprint `2c46ec7b...8fcd4`；reference purge为`not_available`，fingerprint `ec84c806...a1320`。二者writes/index/LLM均为false。
- **VERIFIED**：writing-material 204 passed、全仓588 passed、Ruff passed、mypy 133 source files和`git diff --check`通过。
- **GIT REMOTE VERIFIED**：手动HTTPS push最初返回无credential，但随后本地tracking ref和只读`git ls-remote`均确认远端`main`为Phase 14C提交`49c12cb38a9bc4a0b9d96239ec6b211fe1d2e2e8`。据此只报告远端读回事实，不把失败命令本身误判为最终远端状态。

自动到期处置当前分类更新为**IMPLEMENTED + FIXTURE_VERIFIED + CURRENT_NOT_DUE**。这表示到期时具有安全命令闭环，并不表示当前run已被提前删除，也不表示仓库内新增常驻scheduler。生产run仍active至2031-07-19；外部调度只能在只读plan返回ready后调用确认门控命令。
