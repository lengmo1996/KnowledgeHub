# Fixture Repository

- 阶段目标：提供 CPU、离线、确定性、可比较且能稳定失败的小型视觉项目。
- 实现：240 个合成样本、60/20/20 split、seed 42、双分支 NumPy 模型、addition 与 concatenation_projection。
- 配置：baseline、fusion_add、fusion_concat、failure_nan、failure_fix。
- 输出：每个成功运行生成结构化 metrics JSON；失败生成稳定 traceback/log。
- 测试：数据确定性与 concat 参数量更高，2/2 通过。
- 未实施：真实数据下载、GPU 训练、Checkpoint 和外部模型调用。
