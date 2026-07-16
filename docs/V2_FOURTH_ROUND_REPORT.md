# KnowledgeHub V2 fourth-round report

## Scope

This round implements V2.4 as a bounded Writing RAG and evaluation increment.
It does not rebuild Literature, process the full paper library, invent a Venue
style, or infer a Personal profile from collected papers.

## Architecture decisions

- `rules-v2` adds paragraph structure and deterministic style facets while
  retaining the original paragraph, processor version, hash and source
  location.
- Qdrant filters compose domain, section, writing function, Venue, expression
  strength, tone, paragraph length and mathematical-description requirements.
- `paragraph_structure` is a source-minimizing return mode; original prose is
  available only through explicit modes and provenance remains mandatory.
- Venue profiles require user-selected paper IDs. Personal profiles require
  explicit draft files. Both are descriptive, non-normative and stored in
  separate runtime directories.
- Writing task plans expose evidence needs for ten stable tasks. Generation is
  intentionally delegated to the calling Skill.
- Feedback changes later ranking through bounded adjustments but never deletes
  or edits derived/source material.
- Similarity results are internal risk signals. The optional semantic layer is
  reported as not evaluated when no scorer is configured.

## Interfaces

CLI additions include `writing-v2 profile venue|personal`, `writing-v2
profiles`, `writing-v2 task`, combined `query writing` filters and the
`paragraph_structure` return mode. MCP adds the read-only `writing_task` tool
and extends `writing_patterns`/`rag_search` with the same filters.

## Evaluation

`evaluate_writing` reports function/section accuracy, pattern transferability,
traceability, duplicate-material ratio, acceptance, wrong-domain recall and
similarity-risk detection. Human scoring and missing-label denominator rules
are documented in `docs/evaluation.md`.

## Real-data boundary

The existing runtime set contains 134 `rules-v1` entries from five papers.
V2.4 validation selects explicit IDs from that bounded set for a descriptive
Venue cohort. A real Personal profile is intentionally deferred until the user
supplies owned drafts. No Qdrant collection is cleared and no full derivation is
started.

## Verification

- 300 KnowledgeHub tests passed; Ruff and strict MyPy passed across 106 source
  files; `git diff --check` passed.
- All 10 evaluation JSONL files parsed successfully, with 18 reviewable rows.
- Runtime integrity remained valid for 7 source markers, 120 normalized Code
  documents and 134 existing `rules-v1` Writing entries.
- A `rules-v2` dry run selected two papers from
  `NeurIPS/NIPS2025_Poster` and produced 75 paragraph entries without writing
  the manifest or index.
- A real descriptive Venue profile was persisted from the Introduction,
  Method and Experiment families of two explicitly selected NeurIPS papers:
  62 samples, 2 source IDs, stable profile ID
  `profile:ecd101ccc6fb89651549da649e0882fa6e99ae3325c78333d6220fdced742d27`.
- The existing Qdrant Writing collection returned three live research-gap
  results in hybrid mode using the pinned 1,024-dimensional Qwen3 embedding.
  Those live points remain `rules-v1`; activating V2.4 combined filters requires
  a separate explicit bounded `rules-v2` build.
- A real Personal profile was not created because no user-owned drafts were
  supplied. This is an intentional provenance constraint, not a fallback to
  Literature data.

To publish the validated two-paper candidate after operator review, run:

```bash
knowledgehub derive writing --collection NeurIPS/NIPS2025_Poster --limit 2
knowledgehub query writing "research gap after prior progress" \
  --venue NeurIPS --writing-function research_gap \
  --return-mode paragraph_structure --top-k 5
```

The first command updates runtime Writing artifacts and the independent Writing
collection; it does not touch Literature or Code indexes. It is intentionally
not run automatically in this round.
