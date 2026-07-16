# Version Diff 验证

状态：**PASS_WITH_LIMITATIONS**

真实变化：`_LazyAutoMapping.register` 在 Transformers 5.13.0→5.13.1 的 `key` 类型由 `type[PreTrainedConfig]` 变为 `type[PreTrainedConfig] | str`；状态 `signature_changed`，confidence 1.0，旧/新路径与行号一致并绑定两个 pinned commits。

对照：`GenerativePreTrainedModel.__call__` 两版 AST/signature 相同，状态 `unchanged`，无误报。

联合证据：4 个 version-diff documents 均含 source patch、from/to commit、compare URL、related release URLs，并以 `system_derived_source_diff` 标记；查询 envelope 将 retrieved source fact 与 inference 分开，本轮 unsupported inference rate=0。

限制：当前真实样本只覆盖 modified/signature_changed/unchanged；moved/renamed/deprecated/removed/introduced 的实现有单测，但没有在本轮固定真实版本中逐类找到实例，不能作为本轮实证通过。
