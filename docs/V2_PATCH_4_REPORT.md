# KnowledgeHub V2.0.4 multilingual profile report

## Outcome

V2.0.4 closes the Personal Profile external-input boundary after the user
supplied an explicit local draft. The implementation commit is
`1d6c6fe6253f7b70ce03bce530dde9e4718705f6`; the release commit contains this
report and `state/releases/v2_0_4_manifest.json`.

## Changes

- Personal Profile paragraph eligibility and length statistics now use
  language-aware lexical units: Latin words and CJK characters.
- Chinese sentence punctuation, first-person/passive indicators, cautious,
  strong and critical expressions, transitions, contribution/analysis phrases
  and figure/table references receive deterministic handling.
- Frequent Chinese terminology is represented by deterministic character
  bigrams. No external segmenter or model is required.
- The profile processor changed from `writing-profile-v2.4` to
  `writing-profile-v2.5`; the persisted profile schema remains 2.4.
- Frozen `rules-v1` query results now expose `payload.writing_id` as a
  response-only alias of `document_id`. Qdrant payloads are not rewritten.
- Root-level `personal-writing-profile*.md` files are ignored so explicit user
  drafts cannot be accidentally committed.

## Runtime evidence

The supplied Personal Profile was rebuilt from one explicitly selected local
draft. The corrected analyzer selected 24 paragraphs rather than only the 3
embedded English examples. The runtime profile reports
`processor_version=writing-profile-v2.5`, `evidence_source=user_supplied_drafts`
and `is_normative_rule=false`.

The source draft, raw paragraphs, source hash and runtime profile ID remain
outside Git. This report records aggregate validation only.

A real Writing query returned the canonical identifier
`writing:4dabcb38588e1e2bd0db1cd93afddb7c45f5ce139f40caa5ca2280c0fd86d4a8`
in both `document_id` and the normalized `writing_id` response field. It can be
passed unchanged to `knowledgehub writing-v2 feedback` after the user chooses a
label.

## Verification

- 342 tests passed; Ruff, strict MyPy over 112 source files and diff checks
  passed.
- The live V2 evaluation passed all 11 groups and 24 samples, with no configured
  gate regression against V2.0.3.
- `knowledgehub validate all` passed all source, dependency, normalized,
  Writing artifact/state and Code/Writing Qdrant membership checks.
- Final indexes remained green and unchanged: Literature 190,131 points, Code
  1,118 points and Writing 134 points.

## Remaining user decision

No feedback label was invented. `useful`, `not_useful`, `too_generic`,
`too_similar`, `wrong_function`, `wrong_domain` and `poor_style` are subjective
user judgements and are recorded only after explicit selection.
