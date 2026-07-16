# Writing RAG V2

Writing V2 represents paragraph moves such as context → progress → gap →
solution and returns sentence roles and transition relations. Venue profiles
must come from user-selected venue papers; personal profiles must come from
user-supplied drafts. Neither is mixed with general academic guidance or treated
as a normative venue rule.

Internal similarity checks combine exact containment and configurable N-gram
overlap and report `internal_source_similarity`, never a legal plagiarism
assessment. Feedback labels are durable and produce bounded ranking adjustments
without deleting source entries.

```bash
knowledgehub writing-v2 similarity "candidate paragraph"
knowledgehub writing-v2 feedback <writing-id> too_similar
```
