# Writing RAG V2.4

V2.4 upgrades Writing RAG from sentence-pattern lookup to a structured,
paragraph-level evidence service. It does not generate final prose. A Writing
Skill or other caller remains responsible for drafting, validating claims and
checking citations.

## Derived entry and filters

`rules-v2` entries retain the immutable source paragraph and add:

- `paragraph_pattern`, `moves`, `transition_relations`, `sentence_roles` and
  `usage_context`;
- `venue`, `section`, `writing_function` and `research_domain`;
- `expression_strength`, `tone`, `paragraph_word_count` and `contains_math`;
- paper ID, collection, paragraph location, hashes and processor provenance.

The query layer accepts all these facets together. `paragraph_structure`
returns the transferable structure and provenance without a source excerpt;
`pattern_first` includes only a bounded excerpt; `include_original` is explicit.

```bash
knowledgehub query writing "state a bounded research gap" \
  --section Introduction --writing-function research_gap \
  --venue NeurIPS --expression-strength cautious \
  --paragraph-words-min 60 --paragraph-words-max 180 \
  --no-contains-math --return-mode paragraph_structure
```

## Style profiles

Venue and Personal profiles are separate runtime records under
`/data/KnowledgeHub/writing/manifests/profiles`. Both are descriptive and carry
`is_normative_rule=false`.

A Venue profile requires explicit IDs from papers the user selected as
representative. It cannot be created from the whole library implicitly:

```bash
knowledgehub writing-v2 profile venue NeurIPS-selected \
  --paper-id 'zotero:user:...:ATTACHMENT:0' \
  --paper-id 'zotero:user:...:ATTACHMENT:0' \
  --section Introduction --section Method --section Experiment
```

A Personal profile accepts only explicit user-owned draft files. Literature
entries are never used as a fallback:

```bash
knowledgehub writing-v2 profile personal my-drafts \
  --draft manuscript/introduction.md --draft manuscript/discussion.md
knowledgehub writing-v2 profiles
```

Profiles report paragraph and sentence length, function/section distribution,
tone and strength, first-person/passive usage, list/math/contribution/figure and
analysis-expression rates, transitions, terminology and abbreviations. Raw
personal paragraphs are not copied into the profile.

From `writing-profile-v2.5`, length and minimum-paragraph checks use
language-aware lexical units: Latin words and CJK characters. Chinese sentence
punctuation, common transitions and analysis/strength markers are recognized;
frequent Chinese terminology is represented by deterministic character
bigrams. This keeps multilingual drafts from being silently reduced to their
embedded English examples.

## Writing tasks

The stable tasks are `retrieve_patterns`, `generate_outline`,
`draft_paragraph`, `rewrite_paragraph`, `strengthen_argument`,
`improve_transition`, `compare_expressions`, `audit_repetition`,
`audit_source_similarity` and `respond_to_reviewer`.

```bash
knowledgehub writing-v2 task strengthen_argument \
  "make the experiment interpretation evidence-bounded" \
  --text "The method is clearly better." --section Experiment
```

The CLI and MCP `writing_task` return a `writing_task_plan` with filters,
required evidence fields, the three independent style layers and an explicit
generation boundary. `writing_patterns` remains the direct retrieval tool.

## Similarity and feedback

Internal similarity auditing evaluates exact containment, longest shared word
runs, N-grams and continuous transition structure. A pluggable semantic scorer
may add semantic similarity; when absent the layer is explicitly
`not_evaluated`. Results are always labelled `internal_source_similarity` and
never claim a legal plagiarism determination.

Feedback is durable and non-destructive. `useful` can raise a result by at most
0.1 per event; negative labels lower it, with the total adjustment bounded to
`[-0.5, 0.3]`. Query results expose the adjustment and adjusted quality score,
and are re-ranked without modifying or deleting source entries.

```bash
knowledgehub writing-v2 similarity "candidate paragraph"
knowledgehub writing-v2 feedback <writing-id> too_similar
knowledgehub writing-v2 feedback-status
```

Query responses always expose `payload.writing_id`. For the frozen `rules-v1`
index this is a response-only alias of its canonical `document_id`; no Qdrant
payload is rewritten. Pass that value unchanged to the feedback command.
New feedback rejects profile IDs, malformed identities and IDs absent from the
current derived manifest. `feedback-status` is read-only: it reports valid,
malformed and orphan event counts while retaining historical rows for audit.

Changing from `rules-v1` to `rules-v2` creates version-distinct Writing IDs.
There is no automatic full-library derivation; use an explicit paper,
collection or bounded limit.
