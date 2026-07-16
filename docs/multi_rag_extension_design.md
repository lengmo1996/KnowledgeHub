# Multi-RAG extension design

## Boundaries

Literature, Code and Writing share embedding/index/retrieval implementations but
use separate Qdrant collections and independent runtime state. Literature keeps
its current collection. Code and Writing use versioned `v1` collections so any
future schema change can be built in parallel.

## Storage and configuration

`configs/knowledgehub.yaml` maps each logical knowledge base to its data root,
collection and query instruction. Code source definitions live in
`configs/sources/code.yaml`; adding a library does not require Python changes.
Raw code, normalized documents, Writing-derived records, index artifacts,
state and logs are distinct runtime directories under `/data/KnowledgeHub`.

## Shared contracts

`KnowledgeDocument` is the normalized source contract. `ChunkRecord` remains
the canonical index unit and carries domain metadata in its existing metadata
mapping. Code IDs include repository, version and path/symbol. Writing IDs
include paper, source location, content hash and processor version.

New domain state records document/content/metadata/processor/embedding hashes.
An unchanged tuple is skipped; a changed document is atomically re-embedded and
replaced. Missing documents are touched only during explicit reconciliation or
prune and become tombstones before active vectors are removed.

## Code pipeline

Environment capture resolves installed versions. Registry strategies select
installed, explicit, latest or adjacent stable tags. Git sync is shallow and
fixed to a resolved commit; Release API responses are cached with origin and
license metadata. Markdown/RST/MDX, Python AST and release-aware chunkers emit
version-filterable chunks. Explicit intent wins over deterministic query rules.

## Writing pipeline

Writing derives from parsed Literature artifacts, never from reconstructed
vector results. A versioned `WritingAnalyzer` protocol permits model-backed
analysis, while the default rules implementation is offline and deterministic.
Derived records retain source text and provenance but index transferable
patterns and guidance. `pattern_first` is the default return mode.

## Interfaces and compatibility

The unified request contains `knowledge_base`, query, intent, filters, limit and
return mode. CLI, HTTP and MCP route to the selected collection. Existing
requests omit `knowledge_base` and therefore continue to query Literature.
Convenience version-comparison and Writing-pattern tools delegate to the same
query service rather than implementing separate retrieval stacks.
