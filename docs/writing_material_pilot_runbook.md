# Writing-material 受控 pilot 手册

本手册只覆盖 30–50 篇冻结 selection 的受控 pilot。它不授权真实 LLM、全库处理、生产索引写入或 alias promotion；这些操作仍需各自满足实施计划中的批准门。

## 1. Dry-run gate

先执行 extraction dry-run，并用显式 `--output` 将权限为 0600 的 JSON 保存到临时受控目录：

```text
knowledgehub writing-material extract \
  --selection /approved/selection.jsonl \
  --limit 30 \
  --dry-run \
  --output /tmp/pilot-dry-run.json
```

dry-run 包含 `planning_gates`：

- `provenance_passed` / `provenance_failed`；
- `section_candidate_documents` / `zero_candidate_documents`；
- `selected`、`planned` 和 candidate paragraph 总数。

随后运行，并把带指纹的 ready/stopped gate 保存到受控临时路径：

```text
knowledgehub writing-material pilot assess-dry-run \
  --report /tmp/pilot-dry-run.json \
  --output /tmp/ready-pilot-gate.json
```

默认 gate 为 30–50 篇、provenance 通过率至少 0.80、provenance planning failure 必须为 0，且至少一个文档具有目标 section candidate。零失败要求来自既定的“partial run 不可 candidate index”规则；否则 dry-run 会错误批准一个永远无法完成工作项 8 的 selection。命令只读报告并输出 JSON；不会创建 run、cache、state、TaskStore 或索引。

extraction dry-run 使用 `writing-material-extraction-dry-run-v1`，绑定 source-pinned selection、sections、Literature checkpoint、完整 version bundle 和 artifact fingerprint。评估结果使用 `writing-material-pilot-dry-run-v2`，继续绑定 source report fingerprint，并带自身 artifact fingerprint；v2 另有 `no_provenance_failures` gate。它只是技术 gate，不代表人工授权。

在请求人工批准前先做无网络 provider 预检：

```text
knowledgehub writing-material pilot preflight-provider \
  --gate-report /tmp/ready-pilot-gate.json \
  --output /tmp/provider-preflight.json
```

`writing-material-provider-preflight-v2` 只输出 provider/model、环境变量名称及“是否配置”、base URL 结构是否有效、gate/version 指纹和布尔安全声明；不会输出 endpoint、API key 或其他环境变量值，不创建 provider client，也不发起网络请求。`openai_compatible` 要求 model 非空，且 base URL 是不带 credentials/path/query/fragment 的 HTTP(S) provider origin，例如 `http://127.0.0.1:8000`；不得配置为 `.../v1`，因为 adapter 会自行追加 `/v1/chat/completions`。当前客户端允许本地无鉴权服务，因此 API key 是否存在只报告、不作为通用 ready 条件。

当前 `classification-v9` provider 输出以动态闭合 object 的 key 选择 authoritative `sentence_id`，不含 paragraph ID、`original_text`、start/end 或 normalized text。每个句子只返回一个共享 decision；`category_decisions`只包含一个或多个适用类别键，每个值恒为true，遗漏类别表示false。provider schema和parser均拒绝empty、false、unknown或重复类别键；风险判断仍使用完整closed boolean `risk_flag_decisions`。本地代码将类别键展开，把sentence ID join回immutable source paragraph，确定性派生evidence文本、offsets、provenance和true风险键，再执行既有source-span/provenance验证。任何prompt/schema变化都会改变version bundle，并使旧approval自动失效。

`writing-material-request-partition-v1`要求classification每个请求同时不超过配置的paragraph和authoritative-sentence上限；生产默认分别为4和8。长paragraph只切sentence视图，immutable source不变。abstraction每次最多8条verified evidence。dry-run的`request_partition_plan`必须显示`observed_max_sentences_per_request <= classification_max_sentences_per_request`；分片版本和上限均绑定approval/version bundle。

当前 `abstraction-v4` 的 strategy 使用 closed boolean `risk_flag_decisions`；每次请求把当前 batch evidence IDs 和 categories 写成动态 enum，并对 label、steps、slot、function 等使用与 parser 完全相同的字段上限。未知/重复 evidence reference、unsupported category、重复 material payload、缺失/额外/非布尔风险键均拒绝；review 从 durable JSONL 重读时再次验证 category-reference。该 schema/prompt 变化会生成新 version bundle，旧 extraction approval 不可复用。

非 dry-run CLI 必须在创建 TaskStore audit row、锁、run、state 或 cache 之前完成一次只读 execution authorization precondition，核对 provider origin、selection、sections、Literature checkpoint、provider/model、version bundle 和 approval fingerprint；service 在真正持久化前再次执行同一信任边界验证。失效 approval 应直接返回 mismatch，且 task-state/runs/state/cache 指纹保持不变。

## 2. Extraction 与人工审核

只有 dry-run 报告为 `ready`，并且 provider/model/secret、reviewer、版权和保留策略均获批准后，才能运行小批真实 extraction。完成后必须：

1. 对 `failures.jsonl` 中的 exact-span、provider/schema、quality 和 provenance failure 分类复核；
2. 为每个 evidence/material 给出显式 decision；
3. 生成 `accepted-v2` complete snapshot，pending 必须为 0；
4. 不得编辑 evidence；错误 evidence 应 reject 并重新提取。

在确认 provider/model/secret 后，由批准人显式生成不可覆盖的 0600 approval manifest。命令不读取或保存 secret，也不授权生产索引或自动扩量：

```text
knowledgehub writing-material pilot approve-extraction \
  --gate-report /tmp/ready-pilot-gate.json \
  --output /approved/pilot-approval.json \
  --approver lengmo \
  --reviewer lengmo \
  --rights-basis "approved private research use" \
  --retention-policy "retain for five years" \
  --access-policy "local reviewer only" \
  --yes
```

`--yes` 是真实 provider execution 的独立确认。缺少它、gate 非 ready、输出已存在、字段/指纹被修改，或 provider/model/version/selection/section/Literature checkpoint 漂移时均拒绝。

受控 pilot 的非 dry-run extraction 必须绑定 approval，而不是直接绑定机器 gate：

```text
knowledgehub writing-material extract \
  --selection /approved/selection.jsonl \
  --pilot-approval /approved/pilot-approval.json
```

成功 run 的 manifest 会保留 approver/reviewer/policy、approval、gate 和 dry-run source fingerprint。CLI 的真实 provider extraction 缺少 approval 时在 analyzer/state/run 之前拒绝；显式 fixture provider 仍可用于无网络测试。

可随时只读评估：

```text
knowledgehub writing-material pilot evaluate --run-id RUN_ID
```

报告只保存计数、比例和分布，不复制 `original_text`。

### 无网络 fixture provider

临时测试配置可显式设置 `provider: deterministic_fixture`，model 留空或写为固定的 `deterministic-fixture-v1`。该 provider：

- 不访问网络、不读取 secret、不创建 LLM cache；
- 只选择现有 authoritative sentence ID，再由本地 source 确定性派生 exact span；
- abstraction 全部是明确标记的合成 fixture 文本，不把 evidence 改写成原文；
- provider/model 仍进入 version bundle 和每条 asset trace；
- 不会被生产配置隐式选择。

它只用于小型临时 data root 的流水线验证，不能替代真实 pilot 质量评估。

## 3. 隔离 candidate 与检索报告

candidate 必须是新的物理 collection，输入必须为 complete accepted snapshot，且 `promotion_performed=false`。pilot evaluator 只接受非 dry-run、`accepted_only=true`、无 failure 且 indexed 数等于 accepted strategy/template/phrase 总数的 candidate report。

非 dry-run index 会在 candidate data dir 原子写入权限为 0600 的 `writing-material-candidate.json`。它使用 `writing-material-candidate-v1`，保存 accepted manifest hash、source verification、collection/data dir、计数和 artifact fingerprint；后续评估拒绝篡改、旧 schema、run ID 不一致或 dry-run report。

检索 query fixture 每行采用 closed-world contract：

```json
{
  "schema_version": "writing-material-retrieval-case-v1",
  "case_id": "gap-move-01",
  "query": "transition from prior progress to a scoped limitation",
  "expected_asset_ids": ["strategy:..."],
  "top_k": 5
}
```

`expected_asset_ids` 必须来自同一 complete accepted snapshot。至少准备 5 条人工批准 query，然后对独立 candidate 执行：

```text
knowledgehub writing-material pilot evaluate-retrieval \
  --run-id RUN_ID \
  --candidate-report /tmp/candidate/writing-material-candidate.json \
  --queries /tmp/approved-retrieval-cases.jsonl \
  --mode sparse \
  --output /tmp/retrieval-report.json
```

默认 `sparse` 不访问 embedding endpoint。生成器实际查询 candidate，并计算 recall@k、MRR、source-join rate 和单 query 内重复率。每个 writing-material hit 都必须逐字段 join 回 accepted asset/evidence 的 document、Zotero item/attachment、section/page/paragraph/char offsets 和原文 excerpt；报告本身不复制原文。collection 不一致或任何 join 漂移均拒绝。

报告采用 `writing-material-retrieval-evaluation-v1` 并带 candidate fingerprint、query manifest hash、自身 artifact fingerprint、逐 case 排名和失败原因。不得手工填写 `passed: true` 替代生成器。最后执行：

```text
knowledgehub writing-material pilot evaluate \
  --run-id RUN_ID \
  --candidate-report /tmp/candidate-report.json \
  --retrieval-report /tmp/retrieval-report.json
```

只有所有 gate 通过时，状态才是 `eligible_for_manual_expansion_decision`。这不是扩量授权，更不是 promotion 授权。

## 4. Accepted corpus 质量审计

complete accepted snapshot 和检索 gate 通过后，使用确定性、无 LLM 的质量审计检查低自评分、字段内重复片段、超长字段、重复列表项、精确主文本重复及多成员 lexical cluster：

```text
knowledgehub writing-material pilot audit-quality \
  --run-id RUN_ID \
  --output /tmp/writing-material-quality-audit.json
```

报告采用 `writing-material-quality-audit-v1`，绑定 accepted manifest SHA-256 和自身 artifact fingerprint。报告仅包含 asset ID、字段名、计数和阈值，不复制 evidence 或 material 文本；命令不调用 LLM，不修改 review events、accepted snapshot 或索引。`passed=false` 表示应为 flagged assets 创建显式人工复核决定，不能静默编辑 evidence，也不能直接覆盖当前 accepted snapshot。

默认 policy：quality score 至少0.75；同一字段片段最多重复2次；受审字段最多800字符；lexical cluster 默认只允许单成员。阈值完整写入报告，调整阈值必须产生新的 fingerprinted report。

对 `passed=false` 的报告生成 reviewer-local 复核包：

```text
knowledgehub writing-material pilot render-quality-review \
  --run-id RUN_ID \
  --audit-report /tmp/writing-material-quality-audit.json \
  --reviewer REVIEWER \
  --output-dir /tmp/writing-material-quality-review
```

新目录必须不存在；生成器创建0700目录和两个0600文件：`quality-review.md` 供人工阅读，`quality-review-packet.json` 保存 fingerprint、`based_on_hash`、findings、建议动作和可选确定性 edit 草稿。包中允许包含派生 material 字段，但不包含 evidence 原文或 provenance excerpt。所有 `decision_draft.decision` 与 `reason` 默认为 `null`，`decision_import_ready=false`，因此不能直接传给 `review apply`。

重复片段只提出去重 edit；末尾如果是已出现句子的截断前缀会一并移除。near-duplicate 只建议比较后 keep/reject，低分和单纯超长项保留人工判断。生成复核包不会追加 review event、重写 accepted projection 或修改索引。

人工决定应另存为JSONL，每个packet item恰好一行；从`decision_draft`复制后必须填写`decision`和`reason`，并核对`reviewer`。不要修改packet fingerprint或`based_on_hash`。先执行只读preflight：

```text
knowledgehub writing-material review apply-quality \
  --run-id RUN_ID \
  --packet /tmp/writing-material-quality-review/quality-review-packet.json \
  --decisions /tmp/writing-material-quality-decisions.jsonl \
  --dry-run
```

只有dry-run的`status=planned`、decision count与packet flagged count一致，且人工确认keep/edit/reject分布后，才可在独立授权下导入：

```text
knowledgehub writing-material review apply-quality \
  --run-id RUN_ID \
  --packet /tmp/writing-material-quality-review/quality-review-packet.json \
  --decisions /tmp/writing-material-quality-decisions.jsonl \
  --yes
```

导入会追加review event，并将新complete projection写入`accepted-revisions/rev-.../`；历史`accepted/`和旧revision不覆盖。0600 `accepted-current.json`记录当前revision，所有candidate/release/pilot读取器按review events解析并复验当前snapshot。导入成功后原packet会因accepted manifest SHA变化而失效，必须重新运行quality audit；该命令不创建candidate、不修改索引，也不授权stage/promotion。

如果reviewer对flagged items全部选择`accepted`，重新quality audit仍会报告同样的内容finding并保持`passed=false`，因为内容没有发生edit/reject。这应记录为人工接受已知质量风险；不能篡改阈值或报告使其显示通过，也不应循环复用已stale的旧packet要求重复审核。只有material内容实际变化时，才需要基于新snapshot另行评估candidate/release。

## 5. 默认停止条件

- selection 不在 30–50 篇；
- provenance 通过率低于 0.80；
- 任一 document failure；
- 任一 exact-span rejection；
- 任一 provider/schema failure；
- source revalidation 有任何错误；
- review pending 不为 0；
- candidate 是 dry-run、非 accepted-only、发生 promotion 或 indexed count 不一致；
- candidate/retrieval fingerprint、run ID、collection 或 policy 不一致；
- 检索少于 5 条 query、recall@k 低于 0.50、source join rate 不为 1.0、存在重复 material hit 或 source join failure。

阈值属于 `PilotPolicy` / `RetrievalPolicy` 并完整回显在报告中；最终 evaluator 还会复验生成报告所用 policy，不能通过修改报告静默放宽。
