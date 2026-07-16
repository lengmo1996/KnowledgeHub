# V2 evaluation

Code fixtures are grouped by API use, compatibility, debugging, source
navigation and repository adaptation. Writing fixtures are grouped by function,
pattern, paragraph structure, venue style and similarity risk. Each JSONL file
is intentionally small and reviewable; user-derived gold sets should remain
private runtime data.

Automated ranking metrics include Recall@K, MRR, correct-version recall and
correct-symbol recall. Evidence completeness, unsupported inference,
compatibility conclusions, pattern transferability and user acceptance require
separate human labels. Report metrics by task group, never only as one average.

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
