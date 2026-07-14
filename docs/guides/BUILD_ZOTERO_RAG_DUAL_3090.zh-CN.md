# 在双 RTX 3090 工作站构建 KnowledgeHub Zotero RAG

本手册对应仓库内实际 CLI。Zotero source 负责同步和附件安全解压；RAG
pipeline 只消费 snapshot、delta catalog 和 ready PDF，不直接读取 source
SQLite 或 WebDAV ZIP。

## 架构与目录

```text
Zotero Web API + 只读 WebDAV
  -> /data/KnowledgeHub/zotero/manifests
  -> parse -> chunk -> dense+sparse -> Qdrant
  -> RRF -> optional light/quality reranker
```

| 内容 | 默认位置 |
| --- | --- |
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

在 `~/.config/knowledgehub/zotero.env` 写入 `ZOTERO_API_KEY` 并设为 0600：

```bash
set -a
source ~/.config/knowledgehub/zotero.env
set +a
knowledgehub --config configs/sources/zotero.yaml zotero sync --full
knowledgehub --config configs/sources/zotero.yaml zotero validate
```

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
- delta sequence/hash 错误：停止 incremental，修复 source 后 reconcile。
- GPU ID unavailable：用 doctor 对照逻辑 ID、UUID 和容器 runtime。
- collection schema mismatch：创建新 collection，禁止原地混入不同向量。
- TEI OOM：减小 batch/token，不更换模型或维度。
- reranker OOM：服务把 batch 减半至 1；仍失败时显式选择 light/off。
- 端口冲突：检查 6333、8080、8081、8082、8090。

systemd 文件只是示例，不自动安装。source timer 每 10 分钟启动 source service；
该 service 先依赖 `knowledgehub-zotero-cache-refresh.service` 用 `rclone sync`
刷新 `/data/KnowledgeHub/zotero_cache`，成功后才同步 API/manifest。首次运行会填充
完整本地镜像，之后只传输 rclone 检测到的变化。RAG timer 每日串行执行一次 source
sync、incremental ingest 和 validate。离线全量双卡构建不由 timer 启动。
