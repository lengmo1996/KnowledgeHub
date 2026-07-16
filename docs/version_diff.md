# Version and signature differences

Versions preserve raw value, normalized release, local build, release type,
tag and commit. Stable, pre-release, nightly, branch and commit identities are
distinct and never string-sorted.

`knowledgehub symbol compare` aligns exact qualified symbols and reports
unchanged, modified, moved, introduced, removed or signature-changed. Parameter
add/remove/default changes are deterministic; rename, type, return and behavior
changes remain empty/unknown unless evidence supports them. Release evidence
continues to come from Code RAG and must be presented alongside source diffs.
