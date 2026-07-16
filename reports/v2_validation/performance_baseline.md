# 性能与资源基线

## 同步/构建

- Transformers no-change sync：3.02 s / 3.41 s，100% source checkout skipped。
- Writing 3-paper derive：6.76 s 首次；2.11 s 重复；87/87 skipped。
- Code 10-document candidate：6.18 s，109 chunks。
- Code 1-document post-fix candidate：61 chunks，canonical manifest hash 不变。
- Bounded 20-document recovery reindex：12.76 s，216 chunks。
- Search API Docker image build：约 1,309 s；主要受外网 wheel 下载（约 0.2–0.5 MB/s）限制。

## 查询

- Literature 单查询内部 total：0.113 s（dense 0.039、Qdrant 0.074、sparse <0.001）。
- Writing 单查询内部 total：约 0.028–0.061 s。
- Live evaluation：Code group mean 0.242–1.048 s，P95 同组 0.257–1.048 s；Writing pattern retrieval mean 0.239 s。
- 8 路并发 CLI：8/8 成功；单命令 wall 1.75–2.20 s（含进程启动/模型客户端初始化）。

## 资源

- 全量 tests：13.52 s，峰值 RSS 898,028 KiB。
- GPU0/GPU1 观测显存：8,187/19,260 MiB（服务常驻时）。
- Code：1.1G source、2.6M RAG artifacts、21M Qdrant。
- Literature：3.0G RAG、1.5G Qdrant。

未单独采集解析/chunk/embedding/index 的峰值内存与 GPU 显存时间序列；本报告不据此做性能优化结论。
