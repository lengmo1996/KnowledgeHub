# Service / API / MCP 验证

状态：**FAIL（Search API 部署） / PASS（MCP/CLI）**

## Search API

- 运行容器 `knowledgehub-rag-app:0.1.0` 健康，鉴权正确：无 token 401、正确 token `/health` 200。
- Embedding endpoint healthy，reranker quality revision 可见。
- OpenAPI 只有 `/health`、`/search`；Literature/Code/Writing 三个 `/knowledge/query` 请求均 404。
- 当前源码/测试包含 `/knowledge/query`，已构建 `knowledgehub-rag-app:0.2.5`（image SHA `1a93055a...`）。
- 切换容器失败：当前账号无权读取 `/etc/knowledgehub/rag.env`，sudo 需要密码；未绕过密钥权限。旧服务保持运行。

## MCP

- 两个真实 listener healthz 200：127.0.0.1:8092、10.249.44.27:8091。
- `mcp doctor`：Qdrant green 190,131、catalog 3,574/190,131、双 embedding healthy。
- `mcp validate`：15 tools，status ok；工具包括 knowledge_query、version compare、exact symbol、repository、Writing、feedback 和 legacy rag_search。
- 22 条 MCP/HTTP tests 通过；鉴权、Origin/Host、session binding、结果上限和错误映射有回归覆盖。
- 真实 HTTP tool invocation 未执行：没有读取/输出现有设备 token；只验证 public healthz 和本地 CLI/tool registry。

## 并发与错误

- 8 路 Literature/Code/Writing 并发只读 CLI 查询 8/8 成功。
- CLI 空/非法参数和 MCP/HTTP 错误状态由测试覆盖。
- MCP status 中 service_version 仍为 0.1.0，来源是 conda editable egg-info 漂移；工具集实际为 15。
