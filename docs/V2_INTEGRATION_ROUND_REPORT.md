# KnowledgeHub V2 integration-round report

## Scope

This round implements Phase 7 integration: a stable Skill evidence response,
query budgets, additive MCP/HTTP/CLI interfaces, explicit synchronization plans,
expanded Release Watch metadata and safe runtime cleanup. It does not start a
daemon, download a missing version, execute cleanup, switch environments or
rebuild Literature.

## Evidence interface

`knowledge_query`, HTTP `POST /knowledge/query` and CLI `--evidence-envelope`
return `query_result@2.0` with the `knowledge_evidence` contract: answer context, sources, versions, symbols,
confidence, inferences, warnings and budget usage.

- Retrieved content is a source fact and is never trusted as an instruction.
- Metadata extracted by the pipeline is labelled as a system parse.
- Domain fallback and payload deductions are separated as unverified system
  inferences.
- Confidence is explicitly a retrieval score, not answer correctness.
- Maximum results and estimated total Tokens are enforced across the whole
  response, rather than independently per hit.
- Issue/PR/commit sources require explicit permission. Auto-import permission is
  recorded, but read-only query endpoints perform no download.

The legacy `/search` and `rag_search` contracts are unchanged. MCP now contains
15 strict closed-world tools; only feedback submission writes state.

## Synchronization and release maintenance

`sync plan` represents manual, periodic, release, configuration-change and
on-demand triggers independently of the existing sync implementation. Plans
never start a scheduler, permit downloads, switch environments or promote an
index.

Release Watch still fetches stable official tags and never downloads. When a
cached official release record is available it now reports a bounded summary,
breaking-change signal, version neighborhood and review recommendation, with
release text marked untrusted.

## Cleanup safety

`clean cache|source|snapshots` and `prune unreferenced` are dry-run by default.
Execution requires `--execute --yes` and writes a maintenance audit manifest.
The implementation:

- considers only staging directories older than the configured age;
- protects the checkout referenced by `current.json`;
- keeps at least one snapshot and protects the current snapshot;
- removes only chunk artifacts absent from index state;
- rejects Literature cleanup targets and uses containment-checked deletion.

The real dry run found one 61,520,885-byte stale Transformers 5.13.1 checkout
while protecting the current config-hashed checkout. It was not deleted. Cache,
snapshot and unreferenced-artifact dry runs found no candidates.

## Verification

- 316 tests passed; Ruff passed; strict MyPy passed across 109 source files;
  `git diff --check` passed.
- A real Code evidence query used a 512-Token/3-result budget and returned two
  source contexts, two provenance records, pinned Transformers 5.13.1 and an
  explicit budget-truncation warning. Estimated usage was exactly 512 Tokens.
- A final 128-Token smoke query confirmed flattened `query_result@2.0`, the
  `knowledge_evidence` contract, one source context and 127 estimated Tokens.
- The live query performed no automatic import and exposed no Issue/PR content.
- A real read-only Transformers Release Watch observed latest stable `v5.14.1`,
  the installed `5.13.1`, next stable `5.14.0`, no new-release transition and
  no download or state write.
- Sync planning reported `scheduler_started=false`; all cleanup/prune checks
  remained dry runs and no candidate was deleted.

The bounded Code index ranked localized Arabic API documentation for the
English validation query. This is valid official evidence but reveals a future
language-preference ranking improvement; it is not silently filtered in this
round.
