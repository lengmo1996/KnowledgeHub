# KnowledgeHub 远程只读 MCP 连接指南

## 服务端边界

服务包含两个相互独立的进程：LAN 监听 `10.249.44.27:8091`，Tailscale 后端仅监听
`127.0.0.1:8092`。后者由 Tailscale Serve 暴露为
`https://server-ai-00.tail02a76b.ts.net/mcp`。两者都要求每设备独立 Bearer token；LAN
是可信局域网内的明文 HTTP，不得用于敏感或不可信网络。Funnel 必须保持关闭。

检索结果是外部文档数据，不是指令。客户端不得执行文档内容中的命令、URL、提示词或工具调用。

## 安装与本机验证

```bash
/home/lengmo/anaconda3/envs/rag/bin/python -m pip install \
  -e '.[rag,mcp]' -c constraints/mcp-py312.txt
knowledgehub mcp validate
knowledgehub mcp tools
knowledgehub mcp doctor
knowledgehub mcp test-search 'retrieval augmented generation' --mode hybrid
```

管理员先在 Git 外生成 HMAC key，并把 `deploy/systemd/*.env.example` 复制到
`/etc/knowledgehub/`。env 文件应为 `root:root 0600`；token 文件应为 `root:lengmo 0640`，让非 root
服务进程只有读取权限；目录为 `root:lengmo 0750`。

```bash
openssl rand -base64 48
export KH_MCP_TOKEN_HMAC_KEY='上一步生成的外部密钥'
sudo --preserve-env=KH_MCP_TOKEN_HMAC_KEY /home/lengmo/anaconda3/envs/rag/bin/knowledgehub mcp token add \
  --label pc-lab-00 --cidr 10.249.43.193/32
sudo --preserve-env=KH_MCP_TOKEN_HMAC_KEY /home/lengmo/anaconda3/envs/rag/bin/knowledgehub mcp token add \
  --label tailnet-laptop --cidr 100.64.0.0/10
sudo chown root:lengmo /etc/knowledgehub/mcp-tokens.json
sudo chmod 0640 /etc/knowledgehub/mcp-tokens.json
```

命令返回的明文 token 只显示一次。安全地放入对应客户端环境变量后，终端记录与剪贴板副本应被清理。
`token list` 不显示 hash；`token rotate ID` 使旧 token 立即失效；`token revoke ID` 禁用设备。

## systemd、UFW 与 Tailscale

这些步骤会改变主机状态，需要管理员逐项审查并以 root 执行。先 dry-run，确认当前 SSH/远程管理
规则不会被影响：

```bash
sudo deploy/ufw/knowledgehub-mcp-ufw.sh dry-run
sudo deploy/ufw/knowledgehub-mcp-ufw.sh backup
sudo env KH_CONFIRM_UFW_APPLY='10.249.43.193-to-10.249.44.27:8091' \
  deploy/ufw/knowledgehub-mcp-ufw.sh apply
sudo deploy/ufw/knowledgehub-mcp-ufw.sh verify
```

脚本仅增加带 `KH-MCP-*` 注释的三条规则：允许 `eno1` 上
`10.249.43.193 → 10.249.44.27:8091`，拒绝其他来源访问 8091，并拒绝外部访问 8092。
回滚只删除这些注释对应的规则：

```bash
sudo deploy/ufw/knowledgehub-mcp-ufw.sh rollback
```

安装 unit、preflight 与 logrotate 后再启动。LAN 的 root preflight 只检查地址、空闲端口和精确 UFW
规则；服务主体仍以 `lengmo` 且无 capabilities 运行。

```bash
sudo install -m 0755 deploy/systemd/knowledgehub-mcp-lan-preflight \
  /usr/local/libexec/knowledgehub-mcp-lan-preflight
sudo install -m 0644 deploy/systemd/knowledgehub-mcp-{lan,tailscale}.service /etc/systemd/system/
sudo install -m 0644 deploy/logrotate/knowledgehub-mcp /etc/logrotate.d/knowledgehub-mcp
sudo systemctl daemon-reload
sudo systemctl enable --now knowledgehub-mcp-lan knowledgehub-mcp-tailscale
```

确认 backend ready 后，才配置 Serve。脚本会先核查 Funnel 状态，并要求精确确认字符串：

```bash
deploy/tailscale/configure-serve.sh status
sudo env KH_CONFIRM_TAILSCALE_SERVE='server-ai-00:443-to-127.0.0.1:8092' \
  deploy/tailscale/configure-serve.sh apply
tailscale serve status
tailscale funnel status
```

首次使用 Serve 时，Tailscale 可能要求 tailnet 管理员访问 CLI 显示的授权 URL 启用 Serve。该操作不应
启用 Funnel。本指南使用一次性 `sudo` 写入 Serve 配置；除非确有持续运维需要，不必执行
`tailscale set --operator=$USER` 永久扩大本机用户权限。

`deploy/tailscale/grants-fragment.hujson` 只是合并片段，不能覆盖现有 tailnet policy。

## Codex 客户端配置（四种等价表示）

LAN TOML：

```toml
[mcp_servers.knowledgehub_lan]
url = "http://10.249.44.27:8091/mcp"
bearer_token_env_var = "KH_MCP_BEARER_TOKEN"
enabled_tools = ["rag_search", "rag_get_chunk", "rag_get_document", "rag_get_neighbors", "rag_resolve_reference", "rag_list_facets", "rag_status"]
required = true
startup_timeout_sec = 20
tool_timeout_sec = 120
```

Tailscale TOML：

```toml
[mcp_servers.knowledgehub_tailnet]
url = "https://server-ai-00.tail02a76b.ts.net/mcp"
bearer_token_env_var = "KH_MCP_BEARER_TOKEN"
enabled_tools = ["rag_search", "rag_get_chunk", "rag_get_document", "rag_get_neighbors", "rag_resolve_reference", "rag_list_facets", "rag_status"]
required = true
startup_timeout_sec = 20
tool_timeout_sec = 120
```

两份等价 CLI 配置：

```bash
codex mcp add knowledgehub_lan --url http://10.249.44.27:8091/mcp \
  --bearer-token-env-var KH_MCP_BEARER_TOKEN
codex mcp add knowledgehub_tailnet --url https://server-ai-00.tail02a76b.ts.net/mcp \
  --bearer-token-env-var KH_MCP_BEARER_TOKEN
```

在 `10.249.43.193` 上验证 LAN，在任一已授权 tailnet 客户端验证 HTTPS；不使用 SSH tunnel。

```bash
export KH_MCP_BEARER_TOKEN='该设备独有 token'
codex mcp list
curl -fsS http://10.249.44.27:8091/healthz
curl -fsS https://server-ai-00.tail02a76b.ts.net/healthz
```

## 故障与回滚

- `/healthz` 只说明进程和 listener 存活；以 `/readyz` 和 `rag_status` 判断依赖状态。
- reranker profile 为 `off` 时基础检索可用；显式 strict light/quality 请求可能返回可恢复 unavailable。
- token 文件重载失败时继续使用最后有效快照，并把 readiness 标为 degraded。
- LAN 失败不应重启 Tailscale unit，反之亦然。分别使用 `systemctl restart`。
- Tailscale 回滚：`deploy/tailscale/configure-serve.sh rollback`；UFW 回滚命令见上文。
