# Writing-material provenance 兼容矩阵

- Contract：`docling-charspan-v1`
- Reconstruction：`docling-provenance-v3`
- Chunk map：`writing-chunk-map-v1`
- 验证日期：2026-07-18

本文记录当前代码实际支持的 provenance 边界。它不是对任意 PDF parser 输出的兼容承诺；未列为支持的格式一律 fail closed。

## Docling 兼容矩阵

| Parser / artifact | 状态 | 已验证语义 | 处理 |
|---|---|---|---|
| Docling `2.112.x` + `DoclingDocument` `1.10.0` | 支持 | text/list item 的 `charspan` 是 `orig` 上的 Python 半开区间 `[start,end)`；page 为正整数；bbox 必须含有限数值 `l/t/r/b` | 重建 paragraph/sentence/page/source-span；保留 item/attachment key 和全部 fingerprint |
| Docling `2.112.x`，但 artifact envelope 与 Literature state 的 parser/version/parse fingerprint 不一致 | 拒绝 | artifact 与 state 不能证明属于同一次解析 | `parsed_contract_mismatch` |
| Docling `<2.112` 或 `>=2.113` | 未验证 | schema/charspan 行为可能漂移 | `unsupported_parser_version`；更新本矩阵和 fixture 前不得放行 |
| 非 `DoclingDocument` 或 schema 不是 `1.10.0` | 未验证 | structured 字段和 provenance 语义未知 | `unsupported_docling_schema` |
| PyMuPDF fallback | 不支持 item provenance | 当前资产只有页文本，不能证明 paragraph 到 item/bbox 的映射 | `unsupported_provenance` |
| OCR 结果 | 条件支持 | 不根据文本相似度推断；只有输出仍满足同一 Docling item-level contract 时才可使用 | 缺 `orig`、charspan、page 或完整 bbox 即拒绝；当前没有声明独立 OCR 兼容窗口 |

兼容证据来自三份本机现有解析资产的只读结构抽样：均为 Docling `2.112.0`、`DoclingDocument` `1.10.0`，`charspan` 为两整数数组，body item 均有 `orig`。抽样只记录 schema 和计数，不复制论文正文。

## 文本和 offset 规则

1. `source text` 是 Docling item 的原始 `orig` 字符串。
2. `normalized matching text` 不存在；当前 contract 不做 Unicode normalization、空白折叠、换行删除、断词拼接或 ligature 展开。
3. `source offsets` 是 item `orig` 上的 `[start,end)`。
4. `paragraph offsets` 在当前一 item 一 paragraph 重建中与 source offsets相同；多 segment 时逐段保留显式映射。
5. 重复文本只能由调用方提供的 offset 消歧；系统不搜索“最像”的出现位置。
6. segment gap 可以存在，但任何跨 gap evidence 因 coverage 不完整而拒绝。
7. `text` 清洗值即使更易阅读也不能替代 `orig`，更不能保存为 original evidence。

最小 fixture 位于 `tests/writing_material/fixtures/provenance/docling-2.112-schema-1.10.sanitized.json`。它保留从现有资产观察到的 envelope/schema/charspan/bbox 形状，正文全部替换为短合成字符串，覆盖 Unicode、换行、重复文本和跨页。

## Literature chunk map contract

新生成的 native Docling chunks 可在 `metadata_json` 中保存：

- `chunk_provenance_version = literature-chunk-provenance-v1`；
- `doc_item_refs`：该 chunk 的稳定 Docling `self_ref` 集合。

`ProvenanceDocumentReader.chunk_map()` 只以 `self_ref` 做确定性 join，返回 `writing-chunk-map-v1`：

- `available`：paragraph 的全部 source refs 均存在，且只指向同一个 chunk；结果包含 chunk ID、paragraph ID、sentence IDs 和 source spans；
- `not_available`：旧 chunk 缺 contract、artifact 缺失/损坏、document identity 不符、ref 缺失或 ref 对多个 chunk 有歧义。`reason` 保存具体原因。

现存 canonical chunk Parquet 只有 document/page/section/text 等字段，缺 `doc_item_refs`；因此当前只读检查正确返回 `chunk_provenance_contract_missing_or_unsupported`。不会用 chunk 文本相似度猜测映射，也不会自动重建现有 chunks 或索引。

## 失效与重处理

- `docling-provenance-v3` 已进入 writing-material `version_bundle`；从 v2 升级会把既有成功文档判定为 `changed`。
- Literature parser/version 变化会改变 `parse_fingerprint`；在受支持窗口内，artifact/state 一致时进入 `changed` 重处理。
- 超出兼容窗口的 parser/schema 直接拒绝，等待新增经验证 fixture，而不是降级到模糊 evidence。
- source revalidation 会比较 parse/source fingerprint、paragraph hash、exact slice 和完整 source-span；page、bbox、charspan 或 item ref 漂移都会使旧 evidence 失效。
