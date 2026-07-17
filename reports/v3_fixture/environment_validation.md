# Environment Validation

- Profile：`fixture-cpu`，`environment_type=fixture`，`data_scope=test`，device=cpu。
- 捕获：Python 3.12.13；KnowledgeHub 0.2.5；NumPy 2.5.1；Torch 2.11.0+cu130。
- 隐私：项目路径固定为 `<fixture_repository>`；无 executable 绝对路径、Token、pip freeze、GPU UUID 或用户目录。
- 幂等：内容哈希 `3145013761124e10a5bd032da625eedb01c24bd1142c638c363b7f0f4f8c9b63`；重复捕获为 unchanged。
- 隔离：Profile 只位于 Fixture Workspace，不写正式 Code Environment 目录。
