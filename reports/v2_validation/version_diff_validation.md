# Version Diff 验证

状态：**PASS_WITH_LIMITATIONS**

## 修复后结果

- Transformers 5.13.0/5.13.1 符号数分别为 68,156/68,158。
- 默认 discovery 不再只做 inner join；现在同时覆盖 shared/changed、removed 和 introduced。
- 真实默认 dry-run 返回 6 个 diff documents、18 chunks：`introduced=2`、`modified=3`、`signature_changed=1`。
- 两个真实 introduced 符号为 `PreTrainedConfig.is_remote_code` 和 `PreTrainedConfig.is_custom_code`。
- `_LazyAutoMapping.register` 的 `key` 类型变化继续正确识别为 `signature_changed`，旧/新路径和 pinned commits 可追踪。

回归 fixture 同时覆盖 introduced 和 removed；KH-V2-015 已关闭。此次只执行 dry-run，没有写入或切换正式 Code alias，因此当前正式索引仍未包含新增的六份 diff evidence。

## 限制

当前固定真实版本对没有 removed/moved/renamed/deprecated 样本；removed 有自动化 fixture 覆盖，其余状态不能作为本轮真实数据实证通过。
