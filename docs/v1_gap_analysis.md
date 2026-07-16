# V1 gap analysis

| Problem | Reproduction | Impact | Severity | Likely cause | V2 |
| --- | --- | --- | --- | --- | --- |
| Derived records have no explicit schema envelope | Inspect normalized Code or Writing JSONL | Future incompatible readers cannot fail safely | High | V1 dataclass serialization | V2.0 |
| Index updates write directly to the active collection | Interrupt a build after some documents | Candidate update is not atomically promoted | High | Per-document V1 replacement | V2.0 |
| Domain tasks lack one durable status model | Compare source sync and derived build state | Recovery and operations differ by command | High | Separate V1 state implementations | V2.0 |
| No library/index task lock for derived pipelines | Start two Code builds together | Duplicate work and SQLite/Qdrant races | High | Only Literature has a coordinator lock | V2.0 |
| Validation is subsystem-specific | Run available `rag validate` commands | No cross-domain traceability/count validation | Medium | No Hub validator | V2.0 |
| Only Transformers has a real synchronized/build sample | Inspect V1 manifests | Adapter assumptions are untested for other layouts | High | V1 scope control | V2.1 |
| Version values are normalized only during tag selection | Compare `2.6+cu124`, RC and commit inputs | Environment/repository comparison can be ambiguous | High | No shared version object | V2.1 |
| Environment snapshots omit CUDA/GPU and dependency evidence kinds | Capture an environment | Compatibility matrix lacks runtime context | Medium | V1 pip-oriented capture | V2.1 |
| Exact symbol lookup still enters vector retrieval | Query a qualified method name | Exact navigation may rank semantic matches first | High | No symbol catalog | V2.2 |
| No inheritance/call/import graph or signature diff | Compare one method across two versions | Compatibility evidence remains text-centric | High | V1 AST chunks only | V2.2 |
| No repository intake or API usage inventory | Point KnowledgeHub at a target repository | Codex must assemble dependency evidence manually | High | V1 query-only workflow | V2.3 |
| Writing rules classify whole parsed paragraphs but store no sentence moves | Query paragraph structure | Results cannot explain rhetorical progression | Medium | V1 function-level schema | V2.4 |
| No internal similarity-risk API or user feedback | Reuse a returned expression | Quality cannot learn from user acceptance | High | No feedback/evaluation state | V2.4 |
| GitHub Releases returned unauthenticated HTTP 403 | Run Transformers sync without token | Source sync succeeds but release evidence is partial | Medium | GitHub rate limiting | V2.1/watch |
| MCP get-document/catalog operations remain Literature-oriented | Request a Code document through generic getters | Search works, deep navigation is inconsistent | Medium | V1 catalog is Zotero-specific | V2 integration |

No observed Literature regression, duplicate vector insertion or failed V1 test
is carried into V2. The V1 source/config fingerprint fixes remain the starting
point rather than being redesigned.
