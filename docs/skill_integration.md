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
tool for code reproduction/adaptation/debugging Skills. Exact symbol workflows
use `knowledge_inspect_symbol` and `knowledge_compare_symbols`. Results include version,
commit, location, source URL and `evidence_role`; callers must label their own
inferences separately.

```json
{
  "library": "transformers",
  "from_version": "5.13.0",
  "to_version": "5.13.1",
  "symbol": "PreTrainedModel.from_pretrained"
}
```

Repository-adaptation Skills may call `knowledge_analyze_repository` with a
path relative to `KH_REPOSITORY_ROOT` and a captured environment name. The tool
is static and read-only; it returns declaration-based compatibility evidence,
not a runtime guarantee.

For academic-writing, manuscript-audit and reviewer-response Skills use
`writing_task`, `writing_patterns` or `rag_search` with
`knowledge_base=writing`:

```json
{
  "query": "transition from prior progress to a limitation",
  "section": "Introduction",
  "writing_function": "research_gap",
  "research_domain": "diffusion_models",
  "venue": "NeurIPS",
  "expression_strength": "cautious",
  "paragraph_words_min": 60,
  "paragraph_words_max": 180,
  "contains_math": false,
  "return_mode": "paragraph_structure",
  "limit": 8
}
```

`writing_task` accepts the same filters plus one of the ten stable task names
and optional input text. It returns a retrieval result and a task plan; it does
not return caller-ready prose. Skills must keep general academic guidance,
Venue Profile evidence and Personal Profile evidence as three separately
labelled style layers.

The preferred response fields are writing function, abstract pattern,
rhetorical structure, usage notes, short source excerpt and source paper. Skills
should adapt patterns to the user's claims, avoid copying source expressions and
request original text only for audit or close contextual analysis.

After the user evaluates a pattern, `knowledge_submit_feedback` accepts one of
the documented labels plus bounded query/rank/note context. It is the only V2
MCP extension that writes state and is explicitly non-idempotent; it never
changes the source paper or Writing entry.

When serving MCP outside Literature-only mode, set `KH_HUB_CONFIG` to the local
catalog path and ensure the separate Code/Writing collections and embedding
endpoint are available. Set `KH_REPOSITORY_ROOT` before enabling repository
inspection. All tools except explicit feedback submission remain read-only.
