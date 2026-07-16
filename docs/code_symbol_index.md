# Code symbol index

`knowledgehub symbol build <library> <version>` discovers source through the
library adapter and records modules, classes, methods, properties, functions,
constants and imports in SQLite. Stable IDs use
`library@version::module::qualified_symbol`.

AST relations currently include `imports`, `inherits_from` and `calls` with
source-file evidence. They are syntactic same-repository candidates, not a
complete dynamic call graph. Exact inspection avoids vector ranking:

```bash
knowledgehub symbol inspect transformers 5.13.1 PreTrainedModel.from_pretrained
```
