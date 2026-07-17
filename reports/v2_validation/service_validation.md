# Service / API / MCP 验证

状态：**PASS**

## Search API

- 运行容器为 `knowledgehub-rag-app:0.2.5` 且 healthy。
- `/health`：正确鉴权 200、未鉴权 401。
- OpenAPI 已包含 `/knowledge/query`；Literature/Code/Writing 三库真实请求均返回 200 和来源。
- 8 路并发请求全部 200，wall time 4.831 s。
- 空 query、非法 knowledge base、`limit=101` 返回 422；不存在的 library 安全返回 200/空来源。
- KH-V2-014 已关闭：重建后的运行镜像对非法 `mode` 和未知 filter 均返回 422。
- 8 路并发 Code 查询 8/8 返回 200 且都有来源，wall time 4.093 s。
- 容器重启后自动恢复 healthy；复测 health=200、非法 mode=422。

## MCP

- LAN/Tailscale listener 均 active/running，service version 0.2.5。
- `mcp doctor`/`mcp validate` 已通过，Qdrant green，15 tools 可见。
- 鉴权、Origin/Host、session binding、结果上限及错误映射均有自动化覆盖。

## 运行镜像

- Image：`knowledgehub-rag-app:0.2.5`
- Image ID：`sha256:aa301e48b8118d9f5567f74287bb8d33eea2319c71060d3bb6593f319c300075`
- 最终容器状态：running / healthy
