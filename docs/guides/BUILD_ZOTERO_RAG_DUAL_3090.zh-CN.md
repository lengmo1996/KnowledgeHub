# 在双 RTX 3090 工作站构建 KnowledgeHub Zotero RAG

本手册对应仓库内实际 CLI。Zotero source 负责同步和附件安全解压；RAG
pipeline 只消费 snapshot、delta catalog 和 ready PDF，不直接读取 source
SQLite 或 WebDAV ZIP。

## 架构与目录

```text
Zotero Web API + 坚果云分页 WebDAV (PROPFIND/GET)
  -> /data/KnowledgeHub/zotero_cache（source 同步时只读）
  -> /data/KnowledgeHub/zotero/manifests
  -> parse -> chunk -> dense+sparse -> Qdrant
  -> RRF -> optional light/quality reranker
```

| 内容 | 默认位置 |
| --- | --- |
| WebDAV 本地镜像 | `/data/KnowledgeHub/zotero_cache` |
| Zotero source | `/data/KnowledgeHub/zotero` |
| RAG artifacts/state | `/data/KnowledgeHub/rag/zotero` |
| Qdrant | `/data/KnowledgeHub/qdrant` |
| 模型缓存 | `/data/KnowledgeHub/model-cache` |
| 正式 collection | `zotero_papers_qwen3_4b_1024_v2` |
| smoke collection | `zotero_papers_qwen3_4b_1024_smoke` |

密钥只放仓库外的 0600 环境文件。KnowledgeHub 不自动加载 `.env`。

## 环境检查和初始化

```bash
cd /home/lengmo/KnowledgeHub
conda activate rag
python -m pip install -e '.[rag,dev]'
knowledgehub --config configs/rag/default.yaml rag doctor --dry-run
```

`doctor` 显示 Python、包、GPU 逻辑 ID/UUID/PCI bus、显存、Docker、端口、
source ready 数和磁盘权限。`auto` 检测两卡后选择 `dual`；不会改变模型、
revision、维度或 collection。

初始化脚本默认仅预览：

```bash
scripts/bootstrap_zotero_rag.sh --dry-run
scripts/bootstrap_zotero_rag.sh --apply --preseed-model-cache
```

第二条命令只把旧 embedding/light 缓存复制到新目录，旧项目保持只读。
`--download-quality` 仅在明确授权后下载固定 revision 的 4B reranker。脚本
不会 sudo、修改驱动、下载 8B、OCR 全库或启动全库 embedding。

## 建立当前 Zotero source

在 `~/.config/knowledgehub/zotero.env` 写入 `ZOTERO_API_KEY`、坚果云账号
`ZOTERO_WEBDAV_USERNAME` 和应用密码 `ZOTERO_WEBDAV_PASSWORD`，并设为 0600。
先刷新完整附件镜像，再建立 source：

```bash
set -a
source ~/.config/knowledgehub/zotero.env
set +a
knowledgehub --config configs/sources/zotero.yaml zotero refresh-cache
knowledgehub --config configs/sources/zotero.yaml zotero sync --full
knowledgehub --config configs/sources/zotero.yaml zotero validate
```

`refresh-cache` 不依赖 rclone 的目录列举：它循环跟随坚果云返回的
`Link: rel="next"` 和 `mk` 标记，直到完整列举结束；然后按 size/ETag/mtime
增量下载，以临时文件校验后原子替换。默认通过
`webdav_request_interval_seconds: 2.0` 将 PROPFIND、GET 和重试请求限制为至少间隔
2 秒启动一次；请求耗时和服务端退避已计入间隔。只有完整列举和下载全部成功后才更新本地索引
并清理远端已不存在的本地 ZIP/PROP。每个对象下载成功后还会原子更新权限为 0600
的临时进度索引；刷新中断后，下一次完整列举会复核远端 size/ETag/mtime 和本地文件，
安全跳过已完成对象，并在成功汇总的 `resumed` 字段中计数。完整刷新成功后删除临时
进度索引。列举完成后会打印远端对象总数；处理每个对象后打印 `当前/总数` 以及累计
`downloaded`、`resumed`、`unchanged`。调试时可用 `--no-prune` 保留本地旧文件。

确认 `documents.jsonl`、`delta-catalog.jsonl` 和 `deltas/*.jsonl` 位于
`/data/KnowledgeHub/zotero/manifests/`。catalog 包含 304 空 delta；pipeline
验证序号、前驱、版本、SHA-256 和行数，发现缺口时停止并要求 reconcile。

## Compose 服务

### 生成在线服务访问 key

`KH_RERANKER_API_KEY` 和 `KH_SEARCH_API_KEY` 不是 Zotero、Qwen、Hugging Face
或其他云服务签发的 key，而是本机管理员自己生成的 Bearer token。两项必须使用不同
的随机值。分别执行下面的命令，每条都会输出一个 64 位十六进制字符串：

```bash
openssl rand -hex 32
openssl rand -hex 32
```

使用 `sudoedit` 把两个不同的输出写入 root-only 环境文件；不要写入字面量 `...`：

```bash
sudo touch /etc/knowledgehub/rag.env
sudo chown root:root /etc/knowledgehub/rag.env
sudo chmod 0600 /etc/knowledgehub/rag.env
sudoedit /etc/knowledgehub/rag.env
```

文件至少包含：

```dotenv
KH_RERANKER_API_KEY=替换为第一条命令生成的64位十六进制字符串
KH_SEARCH_API_KEY=替换为第二条命令生成的64位十六进制字符串
```

这两个 key 分别保护 8081 reranker API 和 8090 Search API。服务只接受
`Authorization: Bearer <key>`；不要把 key 放进 URL、YAML、unit、命令输出或 Git。
如果怀疑泄露，重新生成对应值并重启在线服务即可完成轮换。

只验证配置：

```bash
sudo docker compose \
  -f deploy/qdrant/compose.yaml \
  -f deploy/gpu/compose.yaml \
  --profile core --profile embed-dual config
```

启动 Qdrant 和双 TEI：

```bash
docker compose -f deploy/qdrant/compose.yaml --profile core up -d
docker compose -f deploy/gpu/compose.yaml --profile embed-dual up -d \
  embedding-gpu0 embedding-gpu1
```

GPU 0/1 分别只映射一个 `device_id`，主机端口是 8080/8082。在线双卡使用
GPU 0 embedding 和 GPU 1 quality reranker。因为 `/etc/knowledgehub/rag.env`
是 `root:root 0600`，使用 `sudo docker compose` 让 Compose 能够读取该文件：

```bash
sudo docker compose \
  -f deploy/qdrant/compose.yaml \
  -f deploy/gpu/compose.yaml \
  --profile core --profile online-dual up -d
```

4B reranker 首次启动需要加载固定 revision，可能要等待数分钟。查看状态和日志：

```bash
sudo docker compose \
  -f deploy/qdrant/compose.yaml \
  -f deploy/gpu/compose.yaml \
  --profile core --profile online-dual ps --all
sudo docker compose -f deploy/gpu/compose.yaml \
  logs --tail 100 reranker-quality-gpu1 search-api
```

`reranker-quality-gpu1` 和 `search-api` 都应为 `Up`，不能是 `Exited (1)`。如果日志
显示 `KH_RERANKER_API_KEY is required` 或 `KH_SEARCH_API_KEY is required`，说明
Compose 未读取 `/etc/knowledgehub/rag.env`、变量名拼错或值为空。

使用环境文件中的 token 验证两个鉴权接口。下面的命令不会把 key 打印到标准输出：

```bash
sudo bash -c '
  set -a
  source /etc/knowledgehub/rag.env
  set +a
  curl -fsS -H "Authorization: Bearer ${KH_RERANKER_API_KEY}" \
    http://127.0.0.1:8081/health
  printf "\n"
  curl -fsS -H "Authorization: Bearer ${KH_SEARCH_API_KEY}" \
    http://127.0.0.1:8090/health
  printf "\n"
'
```

再通过 Search API 验收 quality reranker：

```bash
sudo bash -c '
  set -a
  source /etc/knowledgehub/rag.env
  set +a
  curl -fsS -X POST \
    -H "Authorization: Bearer ${KH_SEARCH_API_KEY}" \
    -H "Content-Type: application/json" \
    --data-binary '\''{
      "query":"红外小目标检测",
      "mode":"hybrid",
      "limit":3,
      "use_reranker":true,
      "reranker_profile":"quality",
      "fallback_policy":"strict"
    }'\'' \
    http://127.0.0.1:8090/search
  printf "\n"
'
```

成功响应应包含命中结果和 `reranker_profile: "quality"`，且不能出现
`reranker_failed`、`reranker_unavailable` 或降级结果。`fallback_policy: "strict"`
确保 reranker 故障时验收直接失败，而不是静默保留未重排结果。

6333/6334、8080/8081/8082、8090 均只绑定 `127.0.0.1`。
如端口被旧服务占用，可通过 `KH_QDRANT_HTTP_PORT`、`KH_QDRANT_GRPC_PORT`、
`KH_EMBED_GPU0_PORT`、`KH_EMBED_GPU1_PORT`、`KH_RERANKER_PORT` 和
`KH_SEARCH_PORT` 改写主机端口，不必停止旧容器。受保护的 TEI 使用
`KH_EMBEDDING_API_KEY`；客户端只通过 `Authorization: Bearer` header 发送，
不会把密钥写入 URL、日志或运行摘要。

### Core/API 开机、GPU 推理按需启动

生产启动策略按资源与依赖拆分：

| systemd unit | 管理的容器 | 开机策略 |
| --- | --- | --- |
| `knowledgehub-rag-core.service` | Qdrant（CPU/磁盘） | `enable`，首先启动并等待健康 |
| `knowledgehub-rag-search-api.service` | Search API（CPU） | `enable`，在 Qdrant 之后启动 |
| `knowledgehub-rag-online.service` | GPU 0 embedding、GPU 1 quality reranker | static，手工启动 |
| `knowledgehub-rag-embed-dual.service` | GPU 0/1 embedding | static，手工启动 |

Search API 本身不使用 GPU，因此使用 `restart: unless-stopped` 并随系统启动；没有
embedding/reranker 时仍可提供健康状态和 sparse 能力，并明确报告 hybrid/quality
依赖降级。Qwen3-Reranker-4B quality reranker 会加载到 GPU 1，所以属于按需 GPU
workload。所有 embedding 和 reranker 容器保持 `restart: "no"`。

两个手工 GPU unit 没有 `[Install]`，不能被 `systemctl enable`；它们互相
`Conflicts=`，切换时先释放另一套 workload 的显存。每日 RAG timer 不依赖这两个
手工 unit，而是运行独立调度器临时选择并启动 embedding 容器，任务结束后只清理自己
启动的容器。

```bash
sudo systemctl start knowledgehub-rag-online.service
sudo systemctl stop knowledgehub-rag-online.service
sudo systemctl start knowledgehub-rag-embed-dual.service
sudo systemctl stop knowledgehub-rag-embed-dual.service
```

启动顺序为：

```text
docker.service
  → knowledgehub-rag-core.service（Qdrant healthy）
      → knowledgehub-rag-search-api.service（CPU API）
      → MCP LAN/Tailscale
      → 每日 RAG 调度器按显存情况临时启动 embedding GPU 容器
```

## 计划、解析和索引

```bash
knowledgehub --config configs/rag/default.yaml rag plan \
  --source zotero --gpu-mode dual --gpu-ids 0,1 --limit 1

export KH_QDRANT_COLLECTION=zotero_papers_qwen3_4b_1024_smoke
knowledgehub --config configs/rag/default.yaml rag ingest \
  --full --gpu-mode dual --gpu-ids 0,1 --limit 1
```

分阶段运行：

```bash
knowledgehub --config configs/rag/default.yaml rag parse \
  --gpu-mode dual --gpu-ids 0,1 --limit 20
knowledgehub --config configs/rag/default.yaml rag embed \
  --gpu-mode dual --gpu-ids 0,1 \
  --endpoints http://127.0.0.1:8080,http://127.0.0.1:8082
```

单卡或 CPU 小样本：

```bash
knowledgehub --config configs/rag/default.yaml rag ingest \
  --full --gpu-mode single --gpu-ids 0 --limit 1
knowledgehub --config configs/rag/default.yaml rag parse --gpu-mode cpu --limit 1
```

开发验收阶段不要移除 `--limit` 后直接运行全库 embedding。

## 增量、恢复、校验与查询

```bash
knowledgehub --config configs/rag/default.yaml rag ingest --incremental
knowledgehub --config configs/rag/default.yaml rag ingest --resume
knowledgehub --config configs/rag/default.yaml rag ingest --reconcile
knowledgehub --config configs/rag/default.yaml rag validate
knowledgehub --config configs/rag/default.yaml rag validate --qdrant

knowledgehub --config configs/rag/default.yaml rag query \
  '红外小目标检测中如何提高低信噪比下的检测性能？' \
  --mode hybrid --reranker quality --top-k 10
knowledgehub --config configs/rag/default.yaml rag query \
  'infrared small target detection' --mode sparse --top-k 10
```

PDF/content 变化使 parse 及下游失效；metadata-only 默认只更新 payload；
delete/unavailable 删除 active chunks 和 Qdrant points，同时保留审计状态。
Qdrant upsert 默认按 32 points 分批，避免包含长文本和 1024 维向量的单篇
文档请求超过服务端 32 MiB JSON 限制；可用 `KH_QDRANT_UPSERT_BATCH_SIZE`
调小，但不应设为 0。
Search API 使用 `KH_SEARCH_API_KEY`，reranker 使用独立 key。embedding 不可用时
sparse-only 仍可运行，reranker 故障返回 fallback 并保留融合结果。

## 1/20/100 有界验收

```bash
knowledgehub --config configs/rag/default.yaml rag benchmark \
  --stage parsing --compare single,dual --limit 20
knowledgehub --config configs/rag/default.yaml rag benchmark \
  --stage embedding --compare single,dual --limit 100
```

结果写入 `RAG_DATA_DIR/build/benchmarks/`，使用独立 smoke collection。检查两张
GPU 的文档/页数、endpoint batches/texts、耗时、显存和 fingerprint 一致性，
但不预设双卡必然达到 2 倍吞吐。

## 故障排查

- `source snapshot is missing`：先完成 Zotero full sync 与 validate。
- 镜像始终约 750 个对象：不要再用 rclone 列举；运行 `zotero refresh-cache`，
  并在 JSON 摘要中确认 `pages > 1` 和 `remote_objects` 为完整数量。
- `webdav_auth_error`：核对坚果云账号和应用密码，不要使用登录密码。
- WebDAV 分页、XML 或下载失败：本轮不清理本地旧文件；保留已完成下载，冷却后重试。
- delta sequence/hash 错误：停止 incremental，修复 source 后 reconcile。
- GPU ID unavailable：用 doctor 对照逻辑 ID、UUID 和容器 runtime。
- collection schema mismatch：创建新 collection，禁止原地混入不同向量。
- TEI OOM：减小 batch/token，不更换模型或维度。
- reranker OOM：服务把 batch 减半至 1；仍失败时显式选择 light/off。
- 端口冲突：检查 6333、8080、8081、8082、8090。

## 安装并启用 systemd 定时同步

仓库中的 systemd 文件只是示例，不会由安装包或 CLI 自动安装。以下步骤适用于
本文使用的主机路径、用户 `lengmo` 和 conda 环境 `rag`。如果实际路径或用户不同，
应先修改本节安装的 unit 和脚本中的绝对路径及 `User=`。

### 1. 核对运行前提

```bash
test -x /home/lengmo/anaconda3/bin/conda
test -d /home/lengmo/KnowledgeHub
test -f configs/sources/zotero.yaml
test -f configs/rag/default.yaml
```

同时确认 Docker、NVIDIA 驱动和 Compose GPU 支持正常。Qdrant 由 core unit 拉起；
每日 RAG 增量任务会自行选择并临时启动可用的 embedding endpoint。

### 2. 安装 Zotero 配置

服务以 `lengmo` 运行，因此 `/etc/knowledgehub` 必须允许该组进入，YAML 也必须允许
该组读取：

```bash
sudo install -d -o root -g lengmo -m 0750 /etc/knowledgehub
sudo install -o root -g lengmo -m 0640 \
  configs/sources/zotero.yaml /etc/knowledgehub/zotero.yaml
```

以后修改了仓库中的 Zotero 配置，需要重新执行第二条命令，systemd 服务才会读取到
新版本。

### 3. 安装 Zotero 密钥环境文件

如果已经按前文把密钥保存在 `~/.config/knowledgehub/zotero.env`，直接复制它：

```bash
sudo install -o root -g root -m 0600 \
  ~/.config/knowledgehub/zotero.env /etc/knowledgehub/zotero.env
```

也可以使用 `sudoedit /etc/knowledgehub/zotero.env` 创建或检查文件。至少应包含：

```dotenv
ZOTERO_API_KEY=替换为_Zotero_API_key
ZOTERO_LIBRARY_TYPE=user
# user library 可留空；group library 必须填写数字 ID
ZOTERO_LIBRARY_ID=
ZOTERO_WEBDAV_USERNAME=替换为_坚果云账号
ZOTERO_WEBDAV_PASSWORD=替换为_坚果云应用密码
```

不要把密钥写进 YAML、unit 文件或提交到 Git。WebDAV 密码应使用坚果云应用密码，
不是网页登录密码。重新编辑后恢复严格权限：

```bash
sudo chown root:root /etc/knowledgehub/zotero.env
sudo chmod 0600 /etc/knowledgehub/zotero.env
```

`EnvironmentFile=` 由 systemd 管理器在切换到 `User=lengmo` 前读取，因此环境文件可
保持 `root:root 0600`。

### 4. 创建 RAG 环境文件

RAG unit 要求 `/etc/knowledgehub/rag.env` 存在。只运行离线 ingest 且所有设置均来自
`configs/rag/default.yaml` 时，该文件可以为空；要完成在线服务验收，必须按“Compose
服务”一节写入非空且不同的 `KH_RERANKER_API_KEY` 和 `KH_SEARCH_API_KEY`。下列
命令不会清空已有文件：

```bash
sudo touch /etc/knowledgehub/rag.env
sudo chown root:root /etc/knowledgehub/rag.env
sudo chmod 0600 /etc/knowledgehub/rag.env
sudoedit /etc/knowledgehub/rag.env
```

不要把整个 `.env.example` 原样复制到生产环境，因为其中的空值和示例值可能覆盖
YAML 中已经验证过的配置。

### 5. 安装九个 systemd unit 和 GPU 调度脚本

```bash
sudo install -o root -g root -m 0644 \
  deploy/systemd/knowledgehub-zotero-cache-refresh.service \
  /etc/systemd/system/knowledgehub-zotero-cache-refresh.service
sudo install -o root -g root -m 0644 \
  deploy/systemd/knowledgehub-zotero-sync.service \
  /etc/systemd/system/knowledgehub-zotero-sync.service
sudo install -o root -g root -m 0644 \
  deploy/systemd/knowledgehub-zotero-sync.timer \
  /etc/systemd/system/knowledgehub-zotero-sync.timer
sudo install -o root -g root -m 0644 \
  deploy/systemd/knowledgehub-zotero-rag-incremental.service \
  /etc/systemd/system/knowledgehub-zotero-rag-incremental.service
sudo install -o root -g root -m 0644 \
  deploy/systemd/knowledgehub-zotero-rag-incremental.timer \
  /etc/systemd/system/knowledgehub-zotero-rag-incremental.timer
sudo install -o root -g root -m 0644 \
  deploy/systemd/knowledgehub-rag-core.service \
  deploy/systemd/knowledgehub-rag-search-api.service \
  deploy/systemd/knowledgehub-rag-online.service \
  deploy/systemd/knowledgehub-rag-embed-dual.service \
  /etc/systemd/system/
sudo install -d -o root -g root -m 0755 /usr/local/libexec
sudo install -o root -g root -m 0755 \
  deploy/systemd/knowledgehub-rag-incremental-run \
  deploy/systemd/knowledgehub-rag-incremental-with-retries \
  /usr/local/libexec/
```

### 6. 校验 unit 语法

反斜杠必须是行尾最后一个字符，后面不能有空格：

```bash
sudo systemd-analyze verify \
  /etc/systemd/system/knowledgehub-zotero-cache-refresh.service \
  /etc/systemd/system/knowledgehub-zotero-sync.service \
  /etc/systemd/system/knowledgehub-zotero-sync.timer \
  /etc/systemd/system/knowledgehub-zotero-rag-incremental.service \
  /etc/systemd/system/knowledgehub-zotero-rag-incremental.timer \
  /etc/systemd/system/knowledgehub-rag-core.service \
  /etc/systemd/system/knowledgehub-rag-search-api.service \
  /etc/systemd/system/knowledgehub-rag-online.service \
  /etc/systemd/system/knowledgehub-rag-embed-dual.service
```

例如下面这条输出来自系统自带的 Snap unit，不是上述 KnowledgeHub unit 的
错误：

```text
/lib/systemd/system/snapd.service:23: Unknown key name 'RestartMode' in section 'Service', ignoring.
```

它表示当前 systemd 版本不认识较新版 snapd unit 使用的 `RestartMode=`，不会阻止
KnowledgeHub timer。不要为此修改 KnowledgeHub unit；可在系统维护时通过发行版更新
systemd/snapd。若输出明确点名 `knowledgehub-*.service` 或 `knowledgehub-*.timer`，则应
先修复对应错误再启用。

### 7. 重新加载并启用两个 timer

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now \
  knowledgehub-rag-core.service \
  knowledgehub-rag-search-api.service
sudo systemctl enable --now \
  knowledgehub-zotero-sync.timer \
  knowledgehub-zotero-rag-incremental.timer
```

`enable` 在 `timers.target.wants/` 下创建链接，使 timer 在系统进入
`timers.target` 时自动启动；`--now` 同时让它们从本次开机立即进入等待状态。需要
开机启动的是两个 timer，三个 oneshot service 不需要单独 `enable`：它们被 timer
触发，执行结束后回到 inactive 属于正常现象。

`knowledgehub-rag-online.service` 和 `knowledgehub-rag-embed-dual.service` 也是
oneshot + `RemainAfterExit`，但故意没有 `[Install]`，显示 `static` 是正确状态，禁止
把它们加入开机启动。`knowledgehub-rag-core.service` 和 CPU-only
`knowledgehub-rag-search-api.service` 应为 `enabled`。

两个 timer 都设置了 `Persistent=true`。如果机器在计划触发时关机，timer 会在下次
开机并激活后补执行一次错过的任务。首次 `enable --now` 也可能因此很快触发一次。

### 8. 确认开机启动和下次执行时间

```bash
systemctl is-enabled \
  knowledgehub-zotero-sync.timer \
  knowledgehub-zotero-rag-incremental.timer
systemctl is-active \
  knowledgehub-zotero-sync.timer \
  knowledgehub-zotero-rag-incremental.timer
systemctl list-timers --all \
  knowledgehub-zotero-sync.timer \
  knowledgehub-zotero-rag-incremental.timer
```

前两条应分别输出两行 `enabled` 和两行 `active`；`list-timers` 应显示 `NEXT`。
`knowledgehub-zotero-sync.timer` 每小时触发并附加最多 10 分钟随机延迟；
`knowledgehub-zotero-rag-incremental.timer` 每天 03:30 触发并附加最多 10 分钟随机
延迟。

### 9. 手工验收一次并查看日志

先手工启动 source 同步链。它会先运行 WebDAV cache refresh，成功后再进行 Zotero
Web API/manifest 增量同步：

```bash
sudo systemctl start knowledgehub-zotero-sync.service
systemctl status --no-pager knowledgehub-zotero-cache-refresh.service
systemctl status --no-pager knowledgehub-zotero-sync.service
journalctl -u knowledgehub-zotero-cache-refresh.service \
  -u knowledgehub-zotero-sync.service -n 200 --no-pager
```

持续监控两个定时流水线：

```bash
journalctl -f \
  -u knowledgehub-zotero-cache-refresh.service \
  -u knowledgehub-zotero-sync.service \
  -u knowledgehub-zotero-rag-incremental.service
```

RAG service 会先完成 source sync 和 Qdrant core，然后调用 GPU 调度脚本。脚本读取
`nvidia-smi` 的每卡 used/free memory：两卡符合阈值时启动双卡 embedding；只有一张
符合时以 single 模式使用对应 GPU 0 或 GPU 1；都不符合时本次失败，4 小时后重试。
首次执行加两次重试共最多三次，第三次仍失败后等待下一天的 timer。

默认要求候选卡 `free >= 20000 MiB` 且 `used <= 1024 MiB`。可在
`/etc/knowledgehub/rag.env` 调整：

```dotenv
KH_RAG_SCHEDULER_GPU_IDS=0,1
KH_RAG_SCHEDULER_MIN_FREE_MB=20000
KH_RAG_SCHEDULER_MAX_USED_MB=1024
KH_RAG_RETRY_DELAY_SECONDS=14400
```

调度器会先检查当前 Compose 服务。如果目标 GPU 对应的 `embedding-gpu0` 或
`embedding-gpu1` 已在运行，则直接复用，不会把 embedding 自身的显存占用误判为
外部 workload。复用前必须通过对应 `/health`；健康的已有容器不会再执行
`docker compose up`，因而不会被 Compose 配置协调意外重建。如果目标 embedding
尚未运行，则该卡现有的其他容器、debug 或训练进程都按外部占用处理，必须通过显存
阈值。任务完成或失败后，仅停止本轮由调度器启动的 embedding 容器，不停止复用的
已有容器。手工立即验收：

```bash
sudo systemctl start knowledgehub-zotero-rag-incremental.service
journalctl -u knowledgehub-zotero-rag-incremental.service -n 200 --no-pager
systemctl show knowledgehub-zotero-rag-incremental.service \
  --property=NRestarts,Result,ActiveState,SubState
```

离线全量双卡构建不会由 timer 启动。

### 10. 更新或停用

修改 unit 后，重新安装对应文件并执行：

```bash
sudo systemctl daemon-reload
sudo systemctl restart \
  knowledgehub-zotero-sync.timer \
  knowledgehub-zotero-rag-incremental.timer
```

如需停止自动轮询但保留文件：

```bash
sudo systemctl disable --now \
  knowledgehub-zotero-sync.timer \
  knowledgehub-zotero-rag-incremental.timer
```

### 11. 生产收口检查

完成首次自动运行后，不要只检查 timer 为 `active`；还要确认 service 的退出状态和
最近触发时间：

```bash
systemctl show \
  knowledgehub-zotero-sync.timer \
  knowledgehub-zotero-rag-incremental.timer \
  --property=Id,UnitFileState,ActiveState,SubState,LastTriggerUSec,NextElapseUSecRealtime
systemctl show \
  knowledgehub-zotero-cache-refresh.service \
  knowledgehub-zotero-sync.service \
  knowledgehub-zotero-rag-incremental.service \
  --property=Id,Result,ExecMainCode,ExecMainStatus
journalctl -u knowledgehub-zotero-rag-incremental.service -n 200 --no-pager
knowledgehub --config configs/rag/default.yaml rag validate --qdrant
```

最终收口条件：

- 两个 timer 都是 `enabled`、`active`、`waiting`；
- hourly source timer 至少成功触发一次；
- daily RAG timer 至少成功完成一次 `sync → incremental ingest → validate`；
- 三个 service 最近一轮都是 `Result=success`、`ExecMainStatus=0`；
- `rag validate --qdrant` 返回 `valid: true`；
- hybrid 与 sparse-only 查询有结果；
- quality 查询未出现 reranker fallback；
- `search-api` 容器为 `Up`；只有明确启动在线 GPU workload 时，才要求
  `reranker-quality-gpu1` 为 `Up`。
