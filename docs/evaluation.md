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

The repository-adaptation group now includes two public, fixed-commit cases:

- cloneofsimo/lora: PyTorch 2.11 AMP namespace migration;
- state-spaces/s4: Lightning 2 Trainer debug configuration migration and the
  public `unexpected keyword argument 'gpus'` traceback shape.

These fixtures expect exact symbol evidence and explicit verification
boundaries. Syntax or signature-contract success must not be scored as full
model training success.
