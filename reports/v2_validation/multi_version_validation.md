# 多库、多版本与 Environment Profile

状态：**PASS_WITH_LIMITATIONS**

## 多库

- 同步/规范化：Accelerate 1.14.0、Diffusers 0.39.0、Lightning 2.6.5、PyTorch 2.11.0、Transformers 5.13.0/5.13.1；另有 Transformers 5.14.0 source pin。
- Source markers：7；normalized records：124；0 duplicate document IDs。
- 在线查询显式 library filter 后未观察到跨库高排名污染。

## 多版本

- Transformers 5.13.0 与 5.13.1 同时进入 Symbol Catalog，各 68,156/68,158 symbols；5.14.0 保留 source pin，未建立 symbol catalog。
- symbol ID 含 `library@version`，0 duplicates。
- Version Diff 由两个固定 commit 建立，4 个 version-diff documents；版本过滤和来源 URL 正常。

## Environment Profile

`v2-validation-20260717` 成功捕获 Python 3.12.13、Torch/CUDA、双 RTX 3090 和 9 个关注包；未安装的 diffusers/lightning/python entry 为 null，不阻断捕获；未包含 token。不同 profile 文件可共存。

限制：环境 profile 的 editable `knowledgehub` metadata 仍报告 0.1.0；5.14.0 和 Accelerate/Diffusers 尚无 symbol catalog，因此“所有已同步库均支持符号级查询”不成立。
