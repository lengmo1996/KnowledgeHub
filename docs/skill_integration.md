# Skill and MCP integration

The stable entry point is the unified query request:

```json
{
  "knowledge_base": "code",
  "query": "Why is this Transformers argument incompatible?",
  "intent": "compatibility",
  "filters": {
    "library": "transformers",
    "installed_version": "5.13.1",
    "target_version": "5.12.0",
    "source_types": ["release_note", "api_documentation", "source_code"]
  },
  "top_k": 10,
  "return_mode": "pattern_first"
}
```

`rag_search` accepts this route while remaining backward compatible: omitted
`knowledge_base` means Literature. `rag_compare_versions` is the convenience
tool for code reproduction/adaptation/debugging Skills. Results include version,
commit, location, source URL and `evidence_role`; callers must label their own
inferences separately.

For academic-writing, manuscript-audit and reviewer-response Skills use
`writing_patterns` or `rag_search` with `knowledge_base=writing`:

```json
{
  "query": "transition from prior progress to a limitation",
  "section": "Introduction",
  "writing_function": "research_gap",
  "research_domain": "diffusion_models",
  "return_mode": "pattern_first",
  "limit": 8
}
```

The preferred response fields are writing function, abstract pattern,
rhetorical structure, usage notes, short source excerpt and source paper. Skills
should adapt patterns to the user's claims, avoid copying source expressions and
request original text only for audit or close contextual analysis.

When serving MCP outside Literature-only mode, set `KH_HUB_CONFIG` to the local
catalog path and ensure the separate Code/Writing collections and embedding
endpoint are available. All tools remain read-only.
