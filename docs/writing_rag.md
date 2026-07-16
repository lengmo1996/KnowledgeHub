# Writing RAG

Writing RAG derives reusable academic-writing knowledge from existing parsed
Literature artifacts. It does not crawl papers, reconstruct full text from
Qdrant or modify Zotero/Literature state.

```bash
knowledgehub derive writing --paper-id <document-id> --dry-run
knowledgehub derive writing --collection <collection-key> --limit 5
knowledgehub query writing "state a research gap" \
  --section Introduction --writing-function research_gap
```

Filtered or limited derivations reject `--prune`. A complete reconciliation is
an explicit `derive writing --all --prune` operation; source and derived files
remain retained as audit artifacts.

The derivation reads the Literature pipeline database in SQLite read-only mode
and opens canonical parsed Markdown. It recognizes sections and paragraph
groups, skips references and common structural noise, then invokes the stable
`WritingAnalyzer` protocol. The default `RuleWritingAnalyzer` is deterministic,
offline and versioned as `rules-v1`.

Entries retain source paper/location, original and normalized text, writing
function, abstract pattern, rhetorical structure, domains, usage notes,
quality/confidence, analyzer version and content hash. `writing_id` includes
source identity, location, content and processor version, making repeated
derivation idempotent and processor upgrades reproducible.

The indexed text contains only the transferable pattern, rhetorical structure,
guidance and domains; its payload adds only a short source excerpt.
`pattern_first` is the query default and excludes full original or normalized
text. Complete source expressions remain in the local derived JSONL and
should be exposed only when a caller explicitly requests them. Generated prose
must be newly written and verified against its source context.

The first rules cover common Introduction, Related Work, Method, Experiments
and Conclusion functions. Rule confidence is a baseline, not a semantic
guarantee. A future local or remote model analyzer must retain the raw input,
model and prompt versions, timestamp and confidence and may never overwrite the
original text.
