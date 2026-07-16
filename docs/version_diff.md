# Version and signature differences

Versions preserve raw value, normalized release, local build, release type,
tag and commit. Stable, pre-release, nightly, branch and commit identities are
distinct and never string-sorted.

`knowledgehub symbol compare` aligns exact qualified symbols and reports
unchanged, modified, moved, introduced, removed or signature-changed. Parameter
add/remove/default, annotation and return-annotation changes are deterministic.
Rename and behavior changes remain empty/unknown unless evidence supports them.
Release evidence continues to come from Code RAG and must be presented
alongside source diffs.

```bash
knowledgehub symbol build transformers 5.13.0
knowledgehub symbol build transformers 5.13.1
knowledgehub build diff --library transformers \
  --from-version 5.13.0 --to-version 5.13.1 \
  --symbol transformers.models.auto.auto_factory._LazyAutoMapping.register
```

The build reads pinned source markers and the Symbol Catalog, writes a
version-scoped normalized manifest, then incrementally indexes only changed
documents. Each `version_diff` record carries old/new commits, paths and line
ranges, structured changes, bounded unified diff, release links and a GitHub
compare URL. It is labeled `system_derived_source_diff`; it is source-backed
derived evidence, not an upstream compatibility conclusion.
