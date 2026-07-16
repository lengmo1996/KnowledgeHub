# KnowledgeHub MCP 工具参考

所有工具均为只读、幂等、closed-world，并返回 `structuredContent` 与紧凑 text fallback。所有对象
schema 都拒绝未知字段。响应中的文档文本带有：

```json
{"content_origin":"retrieved_document","trusted_as_instruction":false}
```

疑似 prompt injection 只增加 `possible_prompt_injection_in_retrieved_content` warning，不删除原文，
也不会被解释为工具指令。最终 JSON 响应硬上限为 1 MiB。

## `rag_search`

参数包括 `query`、`mode=dense|sparse|hybrid`、`limit`、`prefetch_limit`、`fallback`、
`reranker=off|auto|light|quality`、`neighbors`、`max_chars_per_hit` 和受控 `filters`。filters 只允许
collection、tag、year 范围、DOI、document/attachment ID 与固定 source；不接受任意 Qdrant filter、
文件路径或 URL。hybrid 继续使用 Qdrant RRF，无法可靠恢复的 `dense_score` 与 `sparse_score` 为 null。

`strict` 在必要依赖失败时返回错误；`degrade` 允许 hybrid 在 embedding 不可用时退化到 sparse，并在
`requested_mode`、`mode`、`degraded` 与 `warnings` 中明确报告。

## 已知 ID 读取

- `rag_get_chunk`：按 chunk ID 读取一条，支持文本裁剪。
- `rag_get_document`：返回 manifest 元数据、pipeline fingerprints 和分页 chunk index；默认不返回全文。
- `rag_get_neighbors`：按 chunk ID 获取同文档前后各 0–10 个 chunk。

不存在的 ID 返回 `not_found`，不会把输入解释为路径或查询表达式。`page_numbers` 从已有
`page_start..page_end` 派生。

## 引用解析与 facets

`rag_resolve_reference` 恰好接受 DOI、citation key、Zotero item key、attachment key 或标题中的一个。
结果为 `not_found`、`unique` 或 `ambiguous`；歧义时只返回候选，不静默选择。citation key 仅通过固定
Zotero state SQLite 的只读连接关联。

`rag_list_facets` 分页返回 collection、tag、year 或 source 的值与计数。cursor 是非负十进制 offset。

## `rag_status`

返回脱敏后的 KnowledgeHub/MCP 版本、协议、listener 名称、collection/point 状态、catalog 计数、
token store readiness、三个 circuit 状态、运行时限制与 reranker profile。不返回文件系统路径、token、
HMAC key、上游 API key 或原始异常内容。

## 错误结构

```json
{
  "ok": false,
  "error": {
    "code": "not_found",
    "message": "Chunk was not found.",
    "recoverable": true
  }
}
```

常见 code：`invalid_arguments`、`not_found`、`embedding_unavailable`、`circuit_open`、
`deadline_exceeded`、`response_too_large`、`unavailable`。认证、Host/Origin 和限流错误由 HTTP 层分别
映射为 401/403、421/403 和 429。
# 多知识库扩展

`rag_search` 新增可选 `knowledge_base`（`literature`、`code`、`writing`），
省略时仍查询 Literature。Code/Writing 可使用版本、来源类型、符号、章节、
写作功能和研究领域过滤器。另提供只读工具：

- `rag_compare_versions`：返回带版本与证据角色的兼容性资料；
- `writing_patterns`：默认返回抽象模板、修辞结构、来源短片段和使用提示。
