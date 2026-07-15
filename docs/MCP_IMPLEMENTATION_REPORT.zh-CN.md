# KnowledgeHub 远程只读 MCP 实施报告

日期：2026-07-15

## 实现结果

- SDK：`mcp==1.28.1`；最新协商协议：`2025-11-25`，由 SDK 保留向后协商。
- Server：低层 `Server` factory、显式 7 工具注册表、严格递归 `additionalProperties=false` 输入 schema。
- Transport：共用 factory 的 STDIO 与 stateful Streamable HTTP；POST JSON、GET/SSE、DELETE、900 秒 idle cleanup。
- 输出：全部工具返回 `structuredContent`、紧凑 text fallback 和只读/幂等/closed-world annotations。
- 检索：dense、sparse、Qdrant RRF hybrid 共用 `RetrievalService`；hybrid component scores 保持 null。
- 读取：chunk/document/neighbors 使用受控 Qdrant point/document 查询；manifest、pipeline state 与 Zotero state
  采用固定路径和只读 SQLite。document 不返回全文，只返回元数据与 chunk index。
- 异步：TEI、Qdrant、reranker 网络调用使用 async client；deadline/MCP cancellation 会取消下游请求并释放
  semaphore。稀疏编码和本地只读 catalog 是短同步区段。
- 安全：每设备 token 文件、HMAC-SHA-256、常量时间比较、过期/禁用/路径/CIDR、热重载最后有效快照；
  `KH_MCP_BEARER_TOKEN` 仅作为兼容模式。另有 Host/Origin、session principal binding、可信代理边界、
  每 principal/IP 限流、独立 dependency circuit、抖动重试、并发限制、deadline 与 1 MiB 最终上限。
- 内容信任：所有文本都标记 `retrieved_document` / `trusted_as_instruction=false`；疑似注入只产生 warning。
- 部署：两个 hardened systemd unit、LAN root preflight、外部 env 示例、独立 audit log/logrotate、UFW
  dry-run/backup/apply/verify/rollback、Tailscale Serve/rollback 和 Grants 合并片段均已生成。

## 绑定与目标地址

| 实例 | 进程绑定 | 客户端 URL | 网络策略 |
|---|---|---|---|
| LAN | `10.249.44.27:8091` | `http://10.249.44.27:8091/mcp` | 只允许 `10.249.43.193`；可信 LAN 明文 |
| Tailnet backend | `127.0.0.1:8092` | `https://server-ai-00.tail02a76b.ts.net/mcp` | Serve HTTPS；Funnel 必须关闭 |

## 验收证据

- `pytest -q`：252 tests passed；新增 MCP 覆盖协议、工具、token、schema、catalog、
  prompt injection、超时、响应上限、circuit、strict/degrade、Host/Origin 与 session binding。
- `ruff check src tests`：通过；`ruff format --check src tests`：通过。
- `mypy src`：74 个源文件通过 strict 检查。
- shell：UFW、Tailscale、preflight 脚本 `bash -n` 通过；`git diff --check` 通过。
- 真实 Qdrant：collection `zotero_papers_qwen3_4b_1024_v2` green，180,356 points。
- 真实依赖：embedding `127.0.0.1:8080` 与 `:8082` health 均为 true；reranker profile `off`。
- 真实 smoke（3 hits）：sparse 约 31 ms、dense 约 370 ms、hybrid 约 89 ms；均未降级。
- STDIO E2E：initialize 协商 `2025-11-25`，`tools/list=7`，真实 search/chunk/neighbors/document/
  facets/status 链路通过。
- loopback HTTP E2E：healthz 200、Bearer initialize 200、initialized 202、tools/list 200、DELETE 200；
  缺失/错误 token、Host、Origin 与跨 principal session 均有自动测试。
- LAN Codex 实机验收：`10.249.43.193` 使用设备 token 完成 Bearer initialize/tools-list；
  `rag_status` 返回 SDK `1.28.1`、协议 `2025-11-25`、Qdrant 180,356 points 及全部 circuit closed，
  随后 `rag_search(query="retrieval augmented generation", mode="hybrid", limit=3)` 返回 3 hits，
  `degraded=false`。
- 启发式 Git secret 扫描：未发现 private-key header、OpenAI/GitHub token、`khmcp_` token 或长 Bearer。

strict/degrade 故障测试确认：embedding 失败时 hybrid degrade 明确切换到 sparse 并返回 warning；strict
返回可恢复 unavailable。Qdrant 连续失败会打开独立 circuit；请求 deadline 后 semaphore 可立即复用。
reranker `off` 不影响基础 readiness，显式 strict reranker 请求会失败而不静默伪装成功。

## 当前主机状态与尚未执行项

- `knowledgehub-mcp-lan.service` 已启用并运行于 `10.249.44.27:8091`；root preflight 通过。
- `knowledgehub-mcp-tailscale.service` 已启用并运行于 `127.0.0.1:8092`。
- Tailscale Serve 已将 tailnet-only HTTPS `:443` 转发到 `localhost:8092`；Funnel 未开启。
- UFW 已启用：精确允许 `10.249.43.193 → 10.249.44.27:8091`，拒绝其他 8091 来源及
  `eno1 → 8092`，并保留 `10.249.43.193 → :22` SSH 管理路径和原有 20172 规则。
- LAN 和 Tailnet health/readiness 都返回 ready；Qdrant 180,356 points 与双 embedding replica 正常。
- `10.249.43.193` 的 LAN Codex MCP 初始化、`rag_status` 与 hybrid `rag_search` 实际调用已通过。
- 尚需从另一台 tailnet 客户端通过 HTTPS 完成相同 Codex 验收；MCP Inspector 尚未运行。
- 已验证当前官方 Codex 配置字段；本次 LAN 实机会话证明工具已暴露并可调用，但验收输出未单独记录
  客户端版本号。

## 交付位置

- 工具参考：`docs/reference/MCP_TOOLS.zh-CN.md`
- 连接/部署/回滚：`docs/guides/CONNECT_KNOWLEDGEHUB_MCP.zh-CN.md`
- 威胁模型：`docs/design/MCP_THREAT_MODEL.md`
- systemd/env/preflight：`deploy/systemd/`
- UFW：`deploy/ufw/knowledgehub-mcp-ufw.sh`
- Tailscale：`deploy/tailscale/`
- Python 3.12 constraints：`constraints/mcp-py312.txt`

## 已知限制

- LAN bearer 会以明文 HTTP 传输；不可信或敏感网络必须走 Tailnet HTTPS。
- 标题解析采用受控子串匹配，可能返回多个候选；服务不会自动选择。
- prompt-injection 检测是启发式，真正的安全边界是内容信任标记和客户端不执行检索文本。
- pipeline SQLite 当前保留 190,204 条 inactive 历史 chunk，故受控 chunk/document 读取以 Qdrant 当前
  points 为准；pipeline state 仅补充 fingerprints/build 状态。
- 两个 listener 进程相互隔离，但仍共享 Qdrant、TEI、manifest 和 token store 等依赖。
