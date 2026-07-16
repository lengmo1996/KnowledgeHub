# KnowledgeHub V2 evaluation-round report

## Scope

This round implements Phase 6 after V2.4: executable Code/Writing evaluation,
same-index V1/V2 query-path comparison and per-task regression gates. It does
not add libraries, rebuild Literature, publish the `rules-v2` Writing candidate
or treat synthetic fixtures as user acceptance data.

## Implementation

- Added `evaluation_report@2.0` with Git/dirty provenance, fixture hashes,
  grouped metrics, warnings and failures. No overall average is produced.
- Added `knowledgehub evaluate run|compare`, offline/live modes, V1-direct and
  V2-routed profiles, atomic report writes and non-zero gate exits.
- Code metrics now separate source Recall@K/MRR, source-type accuracy, correct
  version/symbol recall, evidence completeness, unsupported inference and
  latency. Human conclusion accuracy is omitted until labelled.
- Writing execution covers function classification, section normalization,
  paragraph-move exact match, similarity detection, profile-source separation
  and live pattern retrieval.
- Added fail-closed offline/live threshold files. Missing candidate metrics are
  gate failures; human-label metrics are absent rather than silently zero.
- The V2 live profile combines hybrid retrieval with exact Symbol Catalog
  evidence. This matches the documented Code workflow rather than pretending
  vector retrieval alone is symbol inspection.

## Compatibility fixes found by evaluation

The first live run exposed that canonical `Introduction` filters did not match
legacy headings such as `1 Introduction`. V2 now retrieves a bounded candidate
set and applies normalized section-family filtering.

The second run exposed that `rules-v1` Writing points contain venue/year tags
but often lack research domains. V2 applies a deterministic legacy fallback
from source title/excerpt terms, labels it `research_domain_inference=true`, and
never mutates source metadata.

## Observed V1 → V2 live comparison

Both profiles used the same frozen Qdrant collections and top 10. V1 is direct
retrieval; V2 uses unified routing and exact Symbol Catalog evidence.

| Group metric | V1 | V2 |
| --- | ---: | ---: |
| Code API correct-symbol recall | 0.0 | 1.0 |
| Code API source recall | 1.0 | 1.0 |
| Code compatibility source recall | 1.0 | 1.0 |
| Code debugging source recall | 0.0 | 0.5 |
| Code source-navigation recall | 0.0 | 1.0 |
| Repository-adaptation source recall | 0.0 | 0.666667 |
| Writing pattern function recall | 0.0 | 1.0 |
| Writing pattern source traceability | 0.0 | 1.0 |
| Writing wrong-domain recall | 0.0 (no V1 hits) | 0.0 |

All configured live gates passed. Offline deterministic groups scored 1.0 and
the offline V1/V2 no-regression comparison passed.

## Limits

- The public set is deliberately small: 11 groups and 23 samples.
- Compatibility conclusions, pattern transferability and user acceptance have
  no human gold labels yet and are therefore not scored.
- V1/V2 comparison changes query semantics, not the frozen index content or
  embedding model, and does not execute the historical V1 checkout.
- Latency is recorded but not gated because this single local run is not a
  stable performance benchmark.
- Debugging recall remains 0.5 because one generic missing-symbol fixture has no
  version-pinned exact symbol. Repository adaptation remains 0.666667 because
  declaration-only evidence is not indexed as Code chunks.

## Verification

- 306 tests passed; Ruff passed; strict MyPy passed across 107 source files;
  `git diff --check` passed.
- Eleven evaluation groups containing 23 reviewable samples executed offline.
- Both V1-direct and V2-routed live runs queried the existing local Qdrant and
  pinned 1,024-dimensional Qwen3 embedding service.
- Offline and live comparison reports passed every configured per-group gate.
- No source, normalized document, Qdrant point or Literature artifact was
  changed by evaluation.
