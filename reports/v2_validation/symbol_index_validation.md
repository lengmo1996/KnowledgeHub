# Symbol Index 验证

状态：**PASS**

- Symbols：202,897；relations：1,045,496；duplicate symbol IDs：0。
- 类型：method 78,587；import 63,097；class 31,843；function 23,273；constant 3,830；property 2,267。
- 已实现关系：calls 945,215；imports 69,213；inherits_from 31,068。未声称支持 defined_in/exports/documented_by。
- `PreTrainedModel.from_pretrained` 的短名和 fully-qualified name 均解析到 `src/transformers/modeling_utils.py:3874-4408`，返回签名、docstring hash、54 relations、version 和 commit 来源。
- 行号、parent/qualified name、decorator/property/inheritance/import alias/相对导入、重复条件定义和语法降级由 AST 回归测试覆盖。
- 修复后统一 query 将用户显式指定 symbol 合并为 `exact_symbol_source`；原先空 evidence 的命令现返回路径、行号、版本和 GitHub source URL。
