# KnowledgeHub V2 验收：仓库状态

- 采证日期：2026-07-17（Asia/Shanghai）
- 分支：`main`
- 基线 commit：`114d62c3d441def676647c8708ce094cae739e9d`
- 基线提交：`Freeze V2.0.5 release`
- 初始工作树：干净；无用户未提交修改
- 初始远端关系：`main...origin/main [ahead 13]`；验收结束时远端引用已与本地持平
- 本轮未提交修改：有限修复、评估 fixture、部署镜像标签和本报告；未提交、未推送

## 真实入口

- 包入口：`knowledgehub = knowledgehub.cli.main:main`
- 当前源码版本：`pyproject.toml` 为 0.2.5
- conda `rag` editable metadata：0.1.0（漂移，代码实际来自当前 checkout）
- CLI：`zotero/rag/mcp/source/environment/sync/build/derive/query/release/evaluate/index/task/validate/symbol/repository/writing-v2`
- 测试：`pytest`，共 347 条（修复前 343 条）
- HTTP Search API：Docker `search-api`，127.0.0.1:8090
- MCP HTTP：127.0.0.1:8092（tailscale listener）、10.249.44.27:8091（LAN listener）

## 运行数据

- Literature：`/data/KnowledgeHub/rag/zotero`，collection `zotero_papers_qwen3_4b_1024_v2`
- Code：`/data/KnowledgeHub/code`、`/data/KnowledgeHub/rag/code`，alias `knowledgehub_code_current`
- Writing：`/data/KnowledgeHub/writing`、`/data/KnowledgeHub/rag/writing`
- Task Store：`/data/KnowledgeHub/state/tasks.sqlite3`
- Symbol Catalog：`/data/KnowledgeHub/code/state/symbols.sqlite3`
- Snapshot manifests：`/data/KnowledgeHub/indexes/code/snapshots`

## 保护状态说明

本轮没有删除原始源码、论文、Literature collection 或用户仓库。发生受控故障时，正式 Code collection 曾因非原子生产构建从 1,118 增至 6,227 points；已使用本轮预先创建的快照恢复，并将本轮新增的 505 条 state/artifact 精确清除。最终在线完整性重新通过。
