# Writing RAG 验证

状态：**PASS_WITH_LIMITATIONS**

## 独立派生

- 使用 3 篇固定论文和独立 `/tmp` 状态/collection。
- Dry-run：87 entries/87 chunks。
- 首次：87 indexed，6.76 s；第二次：87 skipped、0 indexed，2.11 s。
- Candidate 完整性：87 state documents、87 artifacts、87 points、green；正式 Writing 保持 134/134。
- 每条包含 source paper ID、section/location、processor/prompt version、pattern、usage notes；原文未覆盖。

## 查询与质量

- 7 条指定中文场景均返回 pattern-first 结果、usage notes、source paper 和 bounded source excerpt。
- Research gap 模式：`Although [prior progress...], [unresolved limitation...] remains.`
- Result interpretation 模式：`The results indicate [finding], suggesting that [supported interpretation].`
- 活动函数分布以 research_context/experimental_setup/method_overview/result_interpretation 等 V1 标签为主；任务中的 background 实际映射为 research_context。
- 24-sample live evaluation：function recall=1.0、source traceability=1.0、duplicate material ratio=0、wrong-domain rate=0。

## 相似度

- 直接复制：high，ngram overlap 0.8095，明确标注 internal source similarity、非法律查重。
- 通用句：low，无误报。
- 高度相似改写：low；semantic layer=`not_evaluated`，未满足任务要求。

## 质量问题

- Method filter 将标题含 `Approach` 的封面/作者行归为 Method，属于 section family 误判。
- 少量 source excerpt 是 figure caption/作者行，pattern 虽完整但来源候选质量低。
- 缺少显式 limitation/design_rationale/quantitative_comparison 等活动标签；不能声称十类均已真实覆盖。
