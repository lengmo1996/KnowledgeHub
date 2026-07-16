# KnowledgeHub V2 architecture

V2 is additive over the frozen V1 contracts. Governance introduces strict
schema envelopes, explicit migrations, unified task/idempotency/lock state,
Qdrant snapshot manifests and cross-domain validation. Existing collections and
the embedding model are unchanged.

Code intelligence adds five layout adapters, canonical version identities, a
SQLite exact-symbol catalog, AST relations and deterministic signature diffs.
Vector retrieval remains responsible for semantic evidence; symbol lookup is
the exact path for qualified names. Repository Intake produces profiles and
conservative compatibility matrices but never executes or installs target code.

Writing V2 adds paragraph moves, separately sourced personal/venue profiles,
internal-source similarity risk and durable feedback. It does not claim legal
plagiarism detection. Evaluation metrics remain separate from generation.

Runtime governance lives under `/data/KnowledgeHub/{state,indexes}`. V1 data is
read as V1 until explicitly migrated to a different destination.
