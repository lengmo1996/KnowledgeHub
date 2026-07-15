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
`webdav_request_interval_seconds: 0.5` 将 PROPFIND、GET 和重试请求限制为每秒
最多启动 2 次；请求耗时和服务端退避已计入间隔。只有完整列举和下载全部成功后才更新本地索引
并清理远端已不存在的本地 ZIP/PROP。每个对象下载成功后还会原子更新权限为 0600
的临时进度索引；刷新中断后，下一次完整列举会复核远端 size/ETag/mtime 和本地文件，
安全跳过已完成对象，并在成功汇总的 `resumed` 字段中计数。完整刷新成功后删除临时
进度索引。列举完成后会打印远端对象总数；处理每个对象后打印 `当前/总数` 以及累计
`downloaded`、`resumed`、`unchanged`。调试时可用 `--no-prune` 保留本地旧文件。

确认 `documents.jsonl`、`delta-catalog.jsonl` 和 `deltas/*.jsonl` 位于
`/data/KnowledgeHub/zotero/manifests/`。catalog 包含 304 空 delta；pipeline
验证序号、前驱、版本、SHA-256 和行数，发现缺口时停止并要求 reconcile。

## Compose 服务

只验证配置：

```bash
docker compose \
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
GPU 0 embedding 和 GPU 1 quality reranker：

```bash
docker compose \
  -f deploy/qdrant/compose.yaml \
  -f deploy/gpu/compose.yaml \
  --profile core --profile online-dual up -d
```

6333/6334、8080/8081/8082、8090 均只绑定 `127.0.0.1`。
如端口被旧服务占用，可通过 `KH_QDRANT_HTTP_PORT`、`KH_QDRANT_GRPC_PORT`、
`KH_EMBED_GPU0_PORT`、`KH_EMBED_GPU1_PORT`、`KH_RERANKER_PORT` 和
`KH_SEARCH_PORT` 改写主机端口，不必停止旧容器。受保护的 TEI 使用
`KH_EMBEDDING_API_KEY`；客户端只通过 `Authorization: Bearer` header 发送，
不会把密钥写入 URL、日志或运行摘要。

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

systemd 文件只是示例，不自动安装。source timer 每 10 分钟启动 source service；
该 service 先依赖 `knowledgehub-zotero-cache-refresh.service` 运行分页
`zotero refresh-cache`，成功后才同步 API/manifest。首次运行填充完整本地镜像，
之后只下载分页远端索引判定发生变化的对象。RAG timer 每日串行执行一次 source
sync、incremental ingest 和 validate。离线全量双卡构建不由 timer 启动。
