# KnowledgeHub architecture review

## Baseline

The review was performed on `main` with a clean worktree. The pre-extension
suite passes 266 tests. Runtime inspection found 3,574 current Zotero manifest
records and 3,497 parsed/chunk artifacts. Existing runtime data and Qdrant
collections are external to Git and are not migration targets.

## Current structure and data flow

`knowledgehub.sources.zotero` owns Web API metadata sync, read-only WebDAV cache
resolution, SQLite source state and snapshot/delta publication. The downstream
flow is:

```text
Zotero Web API + WebDAV mirror
  -> documents.jsonl / delta-catalog.jsonl
  -> ZoteroManifestSource
  -> Docling or PyMuPDF
  -> ParsedDocument
  -> StructuralChunker / ChunkRecord / Parquet
  -> EndpointPool + SparseEncoder
  -> QdrantIndex
  -> RetrievalService
  -> CLI, HTTP and MCP
```

The public handoff is the manifest contract; downstream code does not read
Zotero SQLite or archives. `PipelineState` provides transactional stages,
claims, retries, delta checkpoints and index operations. Chunk, embedding,
Qdrant and retrieval components are reusable, while `SourceDocument`, parser
selection and `PipelineOrchestrator` are intentionally PDF/Zotero-specific.

## Reuse and minimal abstraction

- Keep Zotero sync, manifests, PDF parsing and the Literature orchestrator
  unchanged.
- Add a domain-neutral document model upstream of shared Chunk records.
- Reuse embedding, sparse encoding, Qdrant replacement and retrieval for Code
  and Writing through a small incremental chunk-index service.
- Route knowledge bases to separate collections; do not add a mandatory filter
  to the existing Literature collection.
- Give new domains independent source, normalized/derived, state and log roots.

## Risks and controls

- The current RAG config accepts only the Zotero source. New domain services
  therefore consume its index/embedding settings without entering the existing
  PDF orchestrator.
- Existing query schemas hard-code Zotero filters. Additive optional fields and
  a default `literature` route preserve old clients.
- Code data is multi-version and must include tag/commit in identity. A shallow,
  fixed-ref checkout prevents accidental full-history downloads.
- Writing entries contain copyrighted source text. Keep it in the derived store,
  return patterns by default, and expose original text only explicitly.
- Deletes deactivate indexed documents while retaining tombstones and source
  artifacts; physical pruning is never implicit.
- Credentials remain environment-only and are redacted from snapshots/logs.

## Files that must not be semantically changed

The Zotero source contract, existing manifests, Literature state database,
parsed artifacts and `zotero_papers_qwen3_4b_1024_v2` collection remain valid.
Existing `knowledgehub zotero`, `knowledgehub rag` and MCP request defaults are
compatibility requirements.

## Recommended additions

Add Hub configuration/routing, Code registry/sync/parsers, Writing derivation,
an incremental chunk indexer, new CLI groups, additive query filters, offline
fixtures and the domain documentation. No directory migration is required.
