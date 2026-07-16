# Literature RAG 回归

状态：**PASS_WITH_LIMITATIONS**

- Zotero manifest：3,574 documents，3,497 ready attachments，77 missing，最近增量同步为 success/no-change。
- 活动 Literature collection：190,131 points，Qdrant green。
- `mcp doctor`：catalog 3,574 documents、190,370 pipeline chunks、190,131 active chunks；双 TEI healthy。
- 实际查询 `retrieval augmented generation` 成功，返回 `source=zotero`、paper/attachment/document/chunk ID、标题、collection path、页码和 section；总检索耗时 0.113 s。
- Code/Writing 在线完整性分别指向独立 collection；未发现跨库 document ID 或 metadata 污染。
- 347 条全量测试和 V1→V2 gate 无 Literature 回归。

限制：Top-3 中出现一个 `References` section 命中，说明 Literature 尚未对参考文献段落做稳定降权。未重新同步 Zotero、未重建 Literature、未读取私人全文到报告。
