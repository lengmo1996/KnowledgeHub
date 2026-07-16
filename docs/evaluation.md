# V2 evaluation

Code fixtures are grouped by API use, compatibility, debugging, source
navigation and repository adaptation. Writing fixtures are grouped by function,
pattern, paragraph structure, venue style and similarity risk. Each JSONL file
is intentionally small and reviewable; user-derived gold sets should remain
private runtime data.

Automated ranking metrics include Recall@K, MRR, correct-version recall and
correct-symbol recall. Writing metrics are writing-function accuracy, section
accuracy, source traceability, duplicate-material ratio, wrong-domain recall
and internal-similarity risk detection. `evaluate_writing` also aggregates
human pattern-transferability scores and explicit user acceptance labels.

Human transferability uses a 0–1 score: `0` copies source-specific entities or
cannot be adapted; `0.5` is reusable only after substantial restructuring;
`1` expresses a domain-independent rhetorical relation with clear usage
constraints. Acceptance is recorded only after a user chooses to use/adapt the
result. Missing human labels are excluded from their denominator, never treated
as zero. Source traceability requires both source paper and location. A wrong
domain is any returned domain outside the sample's declared acceptable set.

Regression gates run whenever schemas, chunks, embedding, reranking, routing,
symbols or Writing scoring change. V1 remains the baseline; a V2 release must
not reduce Literature compatibility or silently lower a task group.

## Executable reports

Evaluation reports use schema `evaluation_report@2.0`, include Git/dirty state,
fixture hashes, mode, profile, per-group metrics, failures and warnings. They do
not contain an overall quality score.

```bash
# Network/model-free classifier, rhetoric, similarity and fixture validation.
knowledgehub evaluate run --mode offline --profile v2 \
  --output /data/KnowledgeHub/reports/evaluation/offline-v2.json

# Same frozen indexes, two query semantics.
knowledgehub evaluate run --mode live --profile v1 \
  --output /data/KnowledgeHub/reports/evaluation/live-v1.json
knowledgehub evaluate run --mode live --profile v2 \
  --output /data/KnowledgeHub/reports/evaluation/live-v2.json

knowledgehub evaluate compare \
  /data/KnowledgeHub/reports/evaluation/live-v1.json \
  /data/KnowledgeHub/reports/evaluation/live-v2.json \
  --thresholds configs/evaluation/v2.yaml \
  --output /data/KnowledgeHub/reports/evaluation/live-comparison.json
```

`profile=v1` means direct dense/sparse retrieval against the frozen collections.
`profile=v2` uses the unified router plus exact Symbol Catalog evidence and
legacy Writing metadata compatibility. This is a controlled query-path
comparison using one current evaluation harness; it is not execution of an old
Git checkout. Offline profiles intentionally share deterministic scorers.

The CLI exits non-zero when a fixture group fails or a configured gate fails.
`configs/evaluation/offline.yaml` gates deterministic groups;
`configs/evaluation/v2.yaml` gates live retrieval. A missing candidate metric
fails closed. Metrics requiring human labels are omitted when no label exists,
not reported as zero.

Legacy Writing domain fallback is a system inference derived from bounded
source title/excerpt terms. Results expose `inferred_research_domain` and
`research_domain_inference=true`; the value is never written back as source
metadata or represented as an author-provided classification.

The repository-adaptation group now includes two public, fixed-commit cases:

- cloneofsimo/lora: PyTorch 2.11 AMP namespace migration;
- state-spaces/s4: Lightning 2 Trainer debug configuration migration and the
  public `unexpected keyword argument 'gpus'` traceback shape.

These fixtures expect exact symbol evidence and explicit verification
boundaries. Syntax or signature-contract success must not be scored as full
model training success.

The compatibility fixture also includes a pinned Transformers 5.13.0→5.13.1
`_LazyAutoMapping.register` case. It expects `version_diff` evidence and the
exact symbol while preserving the distinction between derived source changes
and official release conclusions. The public fixture set contains 24 samples;
private Personal Profile material and user acceptance labels remain outside
Git and are excluded when no user-provided data exists.
