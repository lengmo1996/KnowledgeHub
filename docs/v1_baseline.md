# KnowledgeHub V1 frozen baseline

Frozen at 2026-07-16T17:20:00+08:00 before V2 implementation.

| Item | V1 baseline |
| --- | --- |
| Git commit | `a15ae0e311e66b7f35cc03c214fa8776344b2d48` |
| Commit subject | `base code-rag write-rag` |
| Hub config SHA-256 | `466e990eb945a96c8ac272f1bfc3f7f4f5e21e64b09470a38e44cd6d2a7fde29` |
| Code registry SHA-256 | `73060599f1729ec7794546274eaf6942760ad86583dc28e8bba8367977b612b9` |
| Literature RAG config SHA-256 | `6a57d115b1f5a7f1be6c924b733ef2cb562a4b46d0e78bc0614c244d804f9dba` |
| Source/manifest schema | Zotero manifest schema 1 |
| V1 derived schemas | Implicit dataclass/JSON contracts; no envelope version |
| Embedding | Qwen/Qwen3-Embedding-4B, revision `5cf2132…`, 1024 dimensions |
| Tests | 278 passed in 8.92 seconds |
| Knowledge bases | literature, code, writing |
| Registered libraries | python, pytorch, torchvision, transformers, diffusers, accelerate, lightning, datasets, safetensors |
| Real synchronized library | transformers 5.13.0, 5.13.1 and 5.14.0 |
| MCP tools | 9 strict read-only tools |

## Frozen indexes

- Literature: `zotero_papers_qwen3_4b_1024_v2`, 190,131 points.
- Code: `knowledgehub_code_qwen3_4b_1024_v1`, 429 points.
- Writing: `knowledgehub_writing_qwen3_4b_1024_v1`, 134 points.

The Code index contains bounded 5.13.0/5.13.1 Transformers samples. Writing
contains a five-paper rules-v1 sample. V1.0 did not index the synchronized
Transformers 5.14.0 checkout.

## Frozen interfaces

Top-level CLI groups are `zotero`, `rag`, `mcp`, `source`, `environment`,
`sync`, `build`, `derive`, and `query`. HTTP `/search` and MCP `rag_search`
default to Literature when `knowledge_base` is omitted. V2 additions must be
additive and retain these defaults.

## Compatibility boundary

Zotero continues to use the Web API, read-only WebDAV mirror and manifest
contract. V2 must not enable the Desktop Local API, rebuild the Literature
collection, alter V1 manifests in place, or change the embedding model.
