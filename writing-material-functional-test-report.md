# Writing-material functional test report

- Test date: 2026-07-18
- Extraction run: `20260717T142521Z-f6bf64b6b314`
- Reviewer: `lengmo`
- Review marker: `TEMPORARY_FUNCTIONAL_TEST_ONLY`
- Candidate collection: `writing-material-functional-test-20260718-v2`
- Promotion performed: no

## Accepted snapshot

| Asset type | Count |
|---|---:|
| Evidence | 10 |
| Strategy | 2 |
| Template | 3 |
| Phrase | 9 |

Fourteen derived assets were indexed. Derived assets whose complete evidence dependency set was not accepted were excluded from the snapshot.

## Online validation

The isolated collection was green with 14 points, 1024-dimensional cosine dense vectors, and a named BM25 sparse vector. The existing production Writing collection remained at 134 points.

| Query intent | Top result | Observation |
|---|---|---|
| Identify a research gap without exaggerated novelty | `gap_identification` phrase | Correct Top-1; Introduction provenance present |
| Explain results and compare with prior work | `result_reporting` strategy | Correct Top-1; prior-work limitation ranked second |
| Acknowledge limitations and introduce future work cautiously | `limitation_acknowledgment` phrase | Correct Top-1; no separate future-work asset exists in this small accepted snapshot |

All three hybrid queries completed without degradation or warnings. Returned records included evidence IDs and document, section, page, and paragraph provenance.

## Defect found and fixed

The first live indexing attempt exposed an invalid Qdrant point-ID format (`chunk:<sha256>`). Candidate chunks now use deterministic UUIDv5 point IDs while preserving the readable asset ID in `document_id` and payload. The index processor version was advanced to `writing-material-index-v2`, and a regression assertion verifies UUID validity and uniqueness.

## Scope

This is a disposable functional-validation result, not a quality-approved release. It must not be promoted or treated as a permanent human gold set. A later extraction run may supersede every accepted decision recorded here.

## Regression and static validation

- Full repository test suite: `397 passed`
- Writing-material, Qdrant ID, and existing Writing RAG regression subset: `27 passed`
- Ruff lint: passed
- Strict mypy over `src`: passed (`127` source files)
- Ruff format check for the writing-material change set: passed (`13` files)

The repository-wide Ruff format baseline still reports unrelated pre-existing files that would be reformatted. They were deliberately left unchanged to keep this implementation scoped.
