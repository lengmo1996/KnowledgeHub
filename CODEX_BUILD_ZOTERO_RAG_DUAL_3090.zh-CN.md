# 交给 Codex 的实现指令：在现有 Zotero 数据源基础上构建双 RTX 3090 本地 RAG

请在当前 `KnowledgeHub` 仓库中，在已经完成的 Zotero 增量同步模块基础上，继续实现真正可运行、可增量维护、可恢复、可查询的本地 RAG 数据管线。

本任务必须充分利用当前机器的硬件条件：

- NVIDIA RTX 3090 × 2；
- 每张显卡 24 GB 显存；
- 默认允许使用双卡完成离线 PDF 解析和 embedding；
- 必须保留单卡运行模式；
- 必须保留 CPU/无 GPU 降级模式；
- GPU 编号、任务分配和运行模式均必须可配置，不能写死；
- 不能仅通过 `gpus: all` 或让两个进程同时看到两张卡，就声称已经实现双卡并行；
- 必须通过明确的 GPU 绑定、任务分片、运行指标和测试证明两张卡确实承担了工作。

当前仓库已经具备或应当具备：

- Zotero Web API 元数据同步；
- 坚果云 WebDAV ZIP 附件定位和安全解压；
- Zotero parent item、attachment、collection 关系；
- Zotero snapshot manifest；
- Zotero delta manifest；
- Zotero source SQLite 状态；
- fingerprint 和幂等同步；
- Zotero source CLI；
- 相关测试和文档。

本任务不要重新实现 Zotero 同步，也不要建立第二套 Zotero 元数据镜像。

本任务实现：

```text
Zotero snapshot / delta manifest
        ↓
PDF parser
        ↓
结构化 parsed document
        ↓
确定性 chunk
        ↓
dense embedding + sparse embedding
        ↓
Qdrant hybrid index
        ↓
RRF hybrid retrieval
        ↓
可选 reranker
        ↓
CLI / Search API
```

最终必须生成一份中文逐步操作手册：

```text
docs/guides/BUILD_ZOTERO_RAG_DUAL_3090.zh-CN.md
```

该文件必须根据最终真实实现生成，不能写成与代码不一致的理论教程。

---

## 一、先审计当前仓库和旧版成功实现

开始编码前，必须先检查当前仓库。

至少检查：

1. 当前目录结构；
2. 顶层 `pyproject.toml`、requirements、锁文件；
3. 当前 Python 包名称和 `src` layout；
4. 当前配置系统；
5. 当前日志系统；
6. 当前 CLI；
7. 当前 SQLite 状态实现；
8. 当前 Zotero snapshot manifest 的真实路径和 schema；
9. 当前 Zotero delta manifest 的真实路径和 schema；
10. 当前 `document_id`、fingerprint、status 和 attachment 字段；
11. 当前 extracted PDF 的实际路径；
12. 当前已有 parsing、chunking、embedding、indexing、retrieval、API 代码；
13. 当前 Docker Compose、systemd 和 GPU 配置；
14. 当前实际 Python、PyTorch、CUDA、Docling、Transformers、Qdrant client 等版本；
15. 当前机器可见 GPU 数量、名称和显存。

同时检查我提供的旧版成功实现文件：

```text
rag_pipeline.py
search_api.py
reranker_service.py
docker-compose.yml
env.example
requirements.in
README.md
```

这些旧文件是行为参考，不是要求直接复制到新架构中的最终代码。

必须从旧版中识别并尽量复用或保留以下已经验证过的设计：

- Docling PDF 解析；
- Docling `HybridChunker`；
- 使用 embedding 模型 tokenizer 控制 chunk；
- 每篇文献独立保存解析结果和 chunk；
- chunk 保存为可重建索引的规范化中间产物；
- 使用 PDF SHA-256、元数据和配置 fingerprint 判断是否跳过；
- 使用 `Qwen/Qwen3-Embedding-4B`；
- 使用 MRL 截取 1024 维并重新 L2 normalize；
- 文档 embedding 和查询 embedding 使用同一模型、revision、维度和归一化规则；
- query 使用面向学术检索的英文 instruction；
- Qdrant 中同时保存 named dense vector 和 named sparse vector；
- sparse 模型使用 `Qdrant/bm25`；
- Qdrant sparse index 使用 IDF modifier；
- dense + BM25 通过 RRF 融合；
- 通过 attachment key 删除旧 points 后再 upsert；
- 支持按 attachment key、limit、offset、force、prune 运行；
- Qdrant snapshot；
- 按需启动 reranker；
- FastAPI 搜索接口；
- 服务只绑定 loopback；
- embedding/reranker 停止后仍可进行 BM25 查询；
- 中断后重复运行能够跳过已完成文档。

必须明确识别旧版限制：

- 旧版主要是单文件式实现；
- `parse --device cuda` 没有明确 GPU 编号；
- Docker 中的 `gpus: all` 只是让容器看见所有 GPU，不代表模型自动使用双卡；
- 旧版只有一个 TEI endpoint；
- 旧版没有双 GPU 请求池；
- 旧版没有针对 PDF 的确定性双进程分片；
- 旧版的 reranker 默认直接 `.cuda()`，没有 GPU 选择；
- 旧版 source manifest schema 与当前 KnowledgeHub manifest 可能不同；
- 旧版不得反向覆盖当前已经完成的 Zotero source 架构。

开始实现前，先输出：

1. 当前仓库审计摘要；
2. 当前 Zotero manifest 的实际 schema；
3. 旧版功能映射；
4. 可以复用的代码和行为；
5. 不能直接复用的部分；
6. 当前实现与旧版的差异；
7. 双 3090 扩展方案；
8. 计划新增文件；
9. 计划修改文件；
10. 新增依赖及原因；
11. 风险和假设。

同时生成：

```text
docs/design/LEGACY_ZOTERO_RAG_GAP_ANALYSIS.md
```

完成分析后直接实现，不要等待再次确认。

---

## 二、总体代码组织

Zotero 只能是统一 RAG pipeline 的一个 source，不得重新建立独立的完整 `zotero_rag` Python 项目。

如果当前仓库没有更成熟的组织，优先采用：

```text
KnowledgeHub/
├── pyproject.toml
├── .env.example
├── configs/
│   ├── sources/
│   │   └── zotero.yaml
│   └── rag/
│       ├── default.yaml
│       ├── dual_3090.yaml
│       └── single_3090.yaml
├── src/
│   └── knowledgehub/
│       ├── sources/
│       │   └── zotero/
│       ├── pipeline/
│       │   ├── models.py
│       │   ├── state.py
│       │   ├── invalidation.py
│       │   ├── orchestrator.py
│       │   ├── workers.py
│       │   └── validation.py
│       ├── parsing/
│       │   ├── base.py
│       │   ├── registry.py
│       │   ├── docling_parser.py
│       │   └── pymupdf_parser.py
│       ├── chunking/
│       │   ├── models.py
│       │   ├── tokenizer.py
│       │   ├── structural.py
│       │   └── fingerprints.py
│       ├── embeddings/
│       │   ├── base.py
│       │   ├── tei_client.py
│       │   ├── endpoint_pool.py
│       │   ├── local_transformer.py
│       │   └── models.py
│       ├── indexing/
│       │   ├── qdrant.py
│       │   ├── sparse.py
│       │   └── lifecycle.py
│       ├── retrieval/
│       │   ├── models.py
│       │   ├── fusion.py
│       │   ├── reranker.py
│       │   └── service.py
│       ├── services/
│       │   ├── search_api.py
│       │   └── reranker_api.py
│       └── cli/
├── tests/
├── scripts/
│   ├── bootstrap_zotero_rag.sh
│   ├── benchmark_rag_gpus.py
│   └── inspect_rag_environment.py
├── deploy/
│   ├── qdrant/
│   │   └── compose.yaml
│   ├── gpu/
│   │   └── compose.yaml
│   └── systemd/
└── docs/
    ├── design/
    │   └── LEGACY_ZOTERO_RAG_GAP_ANALYSIS.md
    └── guides/
        └── BUILD_ZOTERO_RAG_DUAL_3090.zh-CN.md
```

以上只是逻辑建议。

必须优先适配现有仓库：

- 已有 pipeline 抽象则直接扩展；
- 已有 parser registry 则直接注册；
- 已有 Qdrant adapter 则复用；
- 已有 CLI 则接入；
- 已有 FastAPI 服务则接入；
- 已有配置系统则复用；
- 已有日志、atomic write、locking、hashing、retry 工具则复用；
- 不创建第二套配置、日志、CLI、状态或 pipeline；
- 不新增独立 `pyproject.toml`；
- 不创建 `src/zotero_rag`；
- 不大规模重构无关代码。

---

## 三、双 RTX 3090 的运行模式

实现明确的 GPU 运行模式。

至少支持：

```text
auto
dual
single
cpu
```

### 3.1 auto

行为：

- 检测到两张可用 RTX 3090 时，离线全量构建默认使用双卡；
- 只检测到一张 GPU 时自动使用单卡；
- 没有 CUDA 时使用 CPU 或明确拒绝需要 GPU 的步骤；
- 自动模式的最终决策必须打印并写入 run summary；
- 不得静默改变模型、向量维度或 collection。

### 3.2 dual

要求：

- 明确绑定 GPU 0 和 GPU 1；
- PDF 解析阶段两个独立进程各绑定一张 GPU；
- embedding 阶段启动两个独立 TEI 实例，每个实例只看到一张 GPU；
- index coordinator 在两个 TEI endpoint 之间分配 batch；
- reranker 在线运行时默认绑定 GPU 1；
- embedding 在线服务默认绑定 GPU 0；
- 运行摘要记录每张 GPU 的任务量。

### 3.3 single

要求：

- 通过配置选择使用 GPU 0 或 GPU 1；
- PDF 解析仅使用指定 GPU；
- 只启动一个 TEI endpoint；
- reranker 可关闭；
- reranker 也可以与 embedding 使用同一张 GPU，但必须经过显存检查；
- 如果同时加载导致 OOM，应明确失败或按配置退化，不得静默杀死服务；
- 支持分阶段运行：先 parse，再 embedding，再 reranker/query。

### 3.4 cpu

要求：

- 能完成测试、小样本和故障恢复；
- 不要求 CPU 全量运行具备高吞吐；
- 模型太大而无法合理运行时给出明确说明；
- sparse-only 查询应能在 GPU 服务关闭时继续工作。

---

## 四、GPU 配置

至少支持以下配置，具体前缀可适配当前项目：

```text
KH_GPU_MODE=auto
KH_GPU_IDS=0,1

KH_PARSE_DEVICE=cuda
KH_PARSE_GPU_IDS=0,1
KH_PARSE_WORKERS_PER_GPU=1
KH_PARSE_CPU_THREADS_PER_WORKER=8

KH_EMBED_GPU_IDS=0,1
KH_EMBED_ENDPOINTS=http://127.0.0.1:8080,http://127.0.0.1:8082
KH_EMBED_REQUEST_STRATEGY=least_outstanding

KH_RERANK_GPU_ID=1
KH_RERANKER_ENABLED=true
KH_RERANKER_PROFILE=quality

KH_CUDA_ALLOW_TF32=true
KH_GPU_MEMORY_SAFETY_MARGIN_MB=2048
```

要求：

1. GPU ID 不得硬编码在 Python 业务逻辑中；
2. CLI 参数覆盖配置文件；
3. 环境变量覆盖 YAML；
4. `doctor` 输出逻辑 GPU ID、物理 GPU ID、型号、总显存和空闲显存；
5. 记录实际使用的 GPU UUID 或 PCI Bus ID；
6. 启动进程前设置 GPU 可见性；
7. 不要在模型已经 import/初始化后才修改 `CUDA_VISIBLE_DEVICES`；
8. 每个 parser worker 只看见一张 GPU；
9. 每个 TEI container 只获得一张明确的 GPU；
10. 每个 reranker 进程只获得一张明确的 GPU；
11. 不依赖 CUDA MPS；
12. 不依赖两个 3090 之间存在 NVLink；
13. 不要求 NCCL tensor parallel 才能实现双卡；
14. 双卡默认采用任务级 data parallelism，而不是把一个 4B 模型强制切到两张卡。

---

## 五、模型选择

### 5.1 默认 embedding 模型

默认：

```text
Qwen/Qwen3-Embedding-4B
```

默认理由和约束：

- 旧版已经成功完成构建；
- 4B 模型可以在单张 24 GB RTX 3090 上以 FP16/BF16 方式部署；
- 双卡时不需要对单个模型做 tensor parallel；
- 双卡通过两个相同模型副本提高离线 embedding 吞吐；
- 模型 revision 必须固定为 commit SHA；
- 模型名称、revision、pooling、输出维度、normalize、max length、query instruction 都必须参与 embedding fingerprint；
- 文档与查询必须使用同一模型、revision、输出维度和归一化方式。

默认 embedding 参数：

```text
KH_EMBEDDING_MODEL=Qwen/Qwen3-Embedding-4B
KH_EMBEDDING_REVISION=<fixed commit SHA>
KH_EMBEDDING_DTYPE=float16
KH_EMBEDDING_DIM=1024
KH_EMBEDDING_NORMALIZE=true
KH_EMBEDDING_MAX_LENGTH=8192
KH_EMBEDDING_BATCH_SIZE=16
KH_EMBEDDING_MAX_BATCH_TOKENS=8192
KH_EMBEDDING_QUERY_INSTRUCTION=Given a research question, retrieve relevant passages from academic papers that answer the question.
```

要求：

- 使用模型支持的 MRL 方式得到 1024 维；
- 截断到 1024 维后重新 L2 normalize；
- 不允许只改 collection 配置而不改实际向量；
- query 使用 instruction；
- document 不添加 query instruction；
- instruction 默认使用英文；
- 记录完整向量原始维度和最终维度；
- 记录 TEI 或本地 adapter 的实际 pooling；
- 当前 Transformers 版本必须满足模型要求；
- 不得通过升级全部环境来解决单一模型问题。

### 5.2 可选高质量 embedding profile

可提供：

```text
Qwen/Qwen3-Embedding-8B
```

但不得把它设为未经测试的默认模型。

要求：

- 先在单张 3090 上进行 1、20、100 文档 benchmark；
- 验证显存、batch、延迟和吞吐；
- 通过固定检索评测集比较 4B 与 8B；
- 只有收益明确时才推荐切换；
- 8B 必须使用新的 collection；
- 不得与 4B 向量混入同一个 collection；
- 不得原地覆盖已经验收通过的 4B collection；
- 文档中写清迁移和回滚步骤。

### 5.3 reranker

至少支持：

```text
off
light
quality
```

建议：

```text
light:
  Qwen/Qwen3-Reranker-0.6B

quality:
  Qwen/Qwen3-Reranker-4B
```

默认策略：

- 离线建库不启动 reranker；
- 双卡在线检索时：
  - GPU 0：Qwen3-Embedding-4B；
  - GPU 1：Qwen3-Reranker-4B；
- 单卡时默认使用：
  - embedding 4B；
  - reranker 0.6B 或关闭；
- quality reranker 的默认 `max_length` 从 2048 开始；
- batch size 从 4 开始 benchmark；
- 发生 OOM 时自动减小 reranker batch；
- 不自动更换模型；
- reranker 只处理融合后的有限候选；
- reranker 不是索引构建的必要条件；
- 保留旧版 0.6B reranker 作为稳定 fallback；
- reranker 模型、revision、max length、instruction 记录在查询响应和运行元数据中。

---

## 六、推荐的 GPU 调度策略

### 6.1 离线 PDF 解析阶段

运行布局：

```text
GPU 0:
  parser worker 0
  Docling model copy

GPU 1:
  parser worker 1
  Docling model copy

CPU:
  coordinator
  manifest reader
  task queue
  artifact validation
  state commit
```

要求：

1. 每个 parser worker 是独立进程；
2. 每个进程启动前设置自己的 `CUDA_VISIBLE_DEVICES`；
3. 默认每张 3090 一个 parser worker；
4. 不默认在同一 GPU 上创建多个 Docling worker；
5. coordinator 读取 snapshot/delta；
6. coordinator 进行确定性任务分片；
7. worker 只处理分配给自己的 document；
8. worker 写每文档临时产物；
9. coordinator 或安全事务层提交最终状态；
10. 同一 document 不能被两个 worker 同时处理；
11. 每个文档的输出路径独立；
12. 单文档失败不影响另一张 GPU；
13. 中断重跑时按 fingerprint 跳过；
14. 记录每个 worker 的文档数、页数、耗时和失败数。

任务分配可以采用：

```text
stable_hash(document_id) % num_workers
```

或持久化工作队列。

不得采用依赖 manifest 当前行号且在增量时不稳定的分片方法。

### 6.2 离线 embedding 阶段

运行布局：

```text
GPU 0:
  TEI embedding replica 0
  endpoint 127.0.0.1:8080

GPU 1:
  TEI embedding replica 1
  endpoint 127.0.0.1:8082

CPU:
  index coordinator
  FastEmbed sparse model
  Qdrant client
```

要求：

1. 两个 TEI 实例使用相同模型；
2. 使用相同 revision；
3. 使用相同 dtype；
4. 使用相同 pooling；
5. 使用相同 batch token 配置；
6. 使用同一模型缓存，但首次并发下载前必须避免 cache race；
7. 优先先执行模型预下载或先启动一个实例完成缓存；
8. index coordinator 支持 endpoint pool；
9. endpoint pool 至少支持 round-robin 和 least-outstanding；
10. endpoint 失败时有限重试；
11. 一个 endpoint 失败时可按配置降级到另一张卡；
12. 不得因为 endpoint 切换改变 embedding fingerprint；
13. dense vector 返回后由单一 coordinator 写 Qdrant；
14. 两个 worker 不得同时对同一 document 执行 delete + upsert；
15. 每个 document 的 Qdrant replace 操作保持一致性；
16. 记录每个 endpoint 处理的 batch、文本数、token 数、失败和平均延迟。

### 6.3 在线查询阶段

双卡推荐：

```text
GPU 0:
  embedding query service

GPU 1:
  reranker service

CPU:
  Qdrant
  Search API
  sparse query
  RRF
```

单卡推荐：

```text
GPU 0 或 GPU 1:
  embedding query service
  可选 light reranker

CPU:
  Qdrant
  Search API
  sparse query
  RRF
```

要求：

- Search API 不直接加载模型；
- Search API 调用 RetrievalService；
- embedding service 不可用时允许 `sparse` 模式；
- reranker 不可用时按配置退回未 rerank 的 hybrid 结果；
- 不得把 6333、8080、8081、8082、8090 暴露到公网；
- 默认只绑定 `127.0.0.1`。

---

## 七、Docker Compose 要求

旧版的：

```yaml
gpus: all
```

不能作为最终双卡方案。

必须建立明确的 GPU service。

建议逻辑：

```text
qdrant
embedding-gpu0
embedding-gpu1
reranker-light-gpu1
reranker-quality-gpu1
search-api
```

提供 Compose profiles，例如：

```text
core
embed-single
embed-dual
rerank-light
rerank-quality
online-dual
```

要求：

1. Qdrant 不申请 GPU；
2. `embedding-gpu0` 只访问 GPU 0；
3. `embedding-gpu1` 只访问 GPU 1；
4. reranker 只访问指定 GPU；
5. GPU 绑定使用 Docker Compose 明确的 `device_ids`；
6. 不同时设置互斥的 `count` 和 `device_ids`；
7. 每个 GPU service 必须设置 `capabilities: [gpu]`；
8. 端口只绑定 loopback；
9. 模型 cache 路径可配置；
10. Qdrant storage 路径可配置；
11. model revision 从 `.env` 读取；
12. API key 从 `.env` 读取；
13. 不在 Compose 中写真实密钥；
14. 增加 health check；
15. 增加清晰的 profile 启动命令；
16. 不自动启动全部 GPU 服务；
17. 不自动安装 Docker 或 NVIDIA Container Toolkit；
18. 生成 `docker compose config` 验证测试。

双卡 embedding 的示意结构应接近：

```yaml
services:
  embedding-gpu0:
    # ...
    ports:
      - "127.0.0.1:8080:80"
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              device_ids: ["0"]
              capabilities: [gpu]

  embedding-gpu1:
    # ...
    ports:
      - "127.0.0.1:8082:80"
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              device_ids: ["1"]
              capabilities: [gpu]
```

最终内容必须以当前 Docker Compose 版本实际支持的语法为准并执行验证。

---

## 八、运行数据目录

Zotero source 数据与 RAG 派生数据必须分离。

默认：

```text
/data/KnowledgeHub/zotero
```

保存 Zotero source 状态和 manifest。

默认：

```text
/data/KnowledgeHub/rag/zotero
```

保存 RAG 派生数据。

建议：

```text
/data/KnowledgeHub/rag/zotero/
├── state/
│   └── pipeline.sqlite3
├── parsed/
│   ├── json/
│   └── markdown/
├── chunks/
│   └── <safe_document_id>.parquet
├── build/
│   ├── parser_config.json
│   ├── chunk_config.json
│   ├── embedding_config.json
│   ├── container_digests.txt
│   └── benchmarks/
├── runs/
│   └── <run_id>/
├── failures/
│   └── failures.jsonl
├── snapshots/
└── logs/
```

Qdrant：

```text
/data/KnowledgeHub/qdrant
```

模型缓存：

```text
/data/KnowledgeHub/model-cache
```

禁止提交到 Git：

- PDF；
- ZIP；
- parsed；
- chunks；
- Qdrant storage；
- Qdrant snapshot；
- SQLite state；
- model cache；
- logs；
- runs；
- `.env`；
- benchmark 大型输出。

---

## 九、Pipeline state

使用独立于 Zotero source 的 pipeline state。

优先使用当前已有状态抽象；否则使用 `sqlite3`。

至少记录：

### pipeline_documents

```text
source
document_id
attachment_key
source_document_fingerprint
source_metadata_fingerprint
source_content_fingerprint
source_status
pdf_path
pdf_sha256

parse_status
parse_fingerprint
parser_name
parser_version

chunk_status
chunk_fingerprint
chunk_count

embedding_status
embedding_fingerprint
embedding_model
embedding_revision
embedding_dim

dense_index_status
sparse_index_status

assigned_parse_worker
last_processed_at
last_error
```

### chunks

```text
chunk_id
document_id
attachment_key
chunk_index
text_sha256
chunk_fingerprint
page_start
page_end
section_path
token_count
active
updated_at
```

### pipeline_runs

除普通字段外增加：

```text
gpu_mode
gpu_ids
parser_worker_count
embedding_endpoints
per_gpu_documents
per_gpu_pages
per_gpu_embedding_batches
per_gpu_embedding_texts
per_gpu_errors
```

### consumed_deltas

记录：

```text
source
sync_id
delta_path
delta_sha256
consumed_at
status
```

必须支持：

- 幂等；
- 断点恢复；
- 单文档失败隔离；
- 阶段失效；
- delta 去重；
- 两 GPU worker 不重复处理；
- coordinator 崩溃后能够恢复；
- GPU 1 故障后可在 GPU 0 继续未完成任务。

---

## 十、失效规则

统一定义：

```text
source
→ parsing
→ chunking
→ embedding
→ dense_index
→ sparse_index
```

规则：

1. 新 ready 文档：
   - parsing 及全部下游执行。

2. PDF SHA-256 或 content fingerprint 变化：
   - parsing 及全部下游失效。

3. 仅 metadata fingerprint 变化：
   - 默认不重新解析；
   - 默认不重新 chunk；
   - 默认不重新 embedding；
   - 更新 Qdrant payload；
   - 如果 embedding template 包含发生变化的 metadata，则 embedding 及 dense index 失效。

4. parser name/version/config 变化：
   - parsing 及全部下游失效。

5. chunker version/config/tokenizer 变化：
   - chunking 及全部下游失效。

6. embedding model/revision/pooling/dim/normalize/template 变化：
   - embedding 和 dense index 失效；
   - sparse index 是否失效取决于 sparse 输入和模型。

7. sparse model/version/config 变化：
   - sparse index 失效。

8. 文档从非 ready 变为 ready：
   - parsing 及全部下游执行。

9. 文档从 ready 变为 unavailable/deleted：
   - 删除 active chunks；
   - 删除 dense points；
   - 删除 sparse points；
   - 保留审计状态。

10. GPU 调度变化：
    - 不应导致内容 fingerprint 变化；
    - 不应导致 chunk_id 或 embedding 变化；
    - runtime GPU ID 不参与语义 fingerprint；
    - dtype、模型实现或数值策略若可能改变向量，必须参与 embedding fingerprint。

---

## 十一、PDF 解析

实现可插拔 Parser。

主 parser：

```text
Docling
```

fallback：

```text
PyMuPDF
```

要求：

1. 参考旧版已成功的 Docling 转换；
2. 评估当前 Docling 是否支持 threaded standard pipeline；
3. 先以旧版稳定结果为 correctness baseline；
4. 只有 benchmark 和输出一致性验证通过后才切换 pipeline；
5. 解析输出保留 Docling JSON；
6. 同时输出 Markdown 供人工检查；
7. 保留页码、heading、section、table、list；
8. 默认不启用 OCR；
9. 文本过少时标记 `needs_ocr`；
10. 只对失败或扫描件单独 OCR；
11. 不对整个文献库默认 OCR；
12. PDF SHA、parser 配置未变化时跳过；
13. 输出使用临时文件和原子替换；
14. 同一文档只由一个 GPU worker写；
15. parser worker 失败时记录 GPU ID；
16. `safe_document_id` 统一编码；
17. 不把 attachment key 当作唯一通用 document ID；
18. current manifest 中的 `document_id` 是主标识。

输出：

```text
parsed/json/<safe_document_id>.json
parsed/markdown/<safe_document_id>.md
```

---

## 十二、Chunk

优先参考旧版：

```text
Docling HybridChunker
+
Qwen3 embedding tokenizer
```

默认从旧版已验证参数开始，而不是立即切换到完全不同的 chunker。

建议起点：

```text
max_tokens = 768
merge_peers = true
```

同时允许配置：

```text
target_tokens
max_tokens
overlap_tokens
min_tokens
```

要求：

- 必须使用真实 tokenizer token 数；
- 不用字符数冒充 token 数；
- 保留 Docling headings；
- 保留 page numbers；
- 保留原始 text；
- 保存用于 embedding 的 contextualized text；
- 不随意切断表格；
- chunk 顺序稳定；
- chunk ID 确定性；
- GPU worker 数量不影响 chunk 内容和 ID；
- 每篇文献保存独立 Parquet；
- Parquet 是可重建 Qdrant 的规范化中间层；
- schema 版本明确；
- 同时提供读取和完整性验证。

每个 chunk 至少包含：

```text
schema_version
source
document_id
parent_item_key
attachment_key
chunk_id
chunk_index
text
embedding_text
content_sha256
headings
page_numbers
title
authors
year
doi
citation_key
collection_keys
collection_paths
tags
pdf_path
pdf_sha256
parser_version
chunk_config_hash
source_document_fingerprint
```

---

## 十三、Embedding 实现

优先保留旧版 TEI 方案。

实现统一 `EmbeddingBackend`：

```text
TEIEmbeddingBackend
LocalTransformersEmbeddingBackend
FakeEmbeddingBackend
```

默认生产使用 TEI。

`EndpointPool` 至少支持：

- 单 endpoint；
- 多 endpoint；
- round-robin；
- least-outstanding；
- health check；
- timeout；
- retry；
- endpoint quarantine；
- 单 endpoint 降级；
- 运行统计。

要求：

1. 双 GPU 时两个 TEI 副本；
2. 单 GPU 时一个 TEI 副本；
3. 每个副本模型 revision 完全一致；
4. 服务启动时读取 health/model 信息；
5. index 前验证 endpoint 模型一致；
6. 验证返回向量原始维度；
7. 取前 1024 维；
8. 重新 L2 normalize；
9. 验证无 NaN/Inf；
10. 验证非零向量；
11. batch size 可配置；
12. OOM 或请求失败时降低 client batch；
13. TEI `max-batch-tokens` 与 client batch 分开配置；
14. 单批失败不得丢失整个文档状态；
15. 已成功 embedding 的 chunk 不重复计算；
16. 两个 endpoint 的输出应进行抽样一致性测试；
17. 同一个测试文本在两个副本上的向量余弦相似度应接近 1；
18. 如果两个副本输出不一致超过容差，停止正式构建。

---

## 十四、Sparse 和 Qdrant

旧版已经成功使用：

```text
Qdrant named dense vector
+
Qdrant named sparse vector
+
Qdrant/bm25
+
IDF modifier
+
Qdrant RRF
```

如果当前新仓库还没有确定 sparse backend，默认继续采用这一方案。

不得同时创建两套权威 sparse index，例如：

```text
Qdrant sparse
+
SQLite FTS5
```

然后让两套状态互相漂移。

规则：

- 如果当前仓库已经完成并验证 SQLite FTS5，则先做差异分析；
- 只有明确需要时保留多 backend adapter；
- 生产默认必须只有一个 authoritative sparse backend；
- 本任务默认优先 Qdrant sparse，因为旧版已成功完成；
- SQLite FTS5 可以作为可选 fallback，不作为默认重复索引。

Qdrant collection 至少包含：

```text
dense
bm25
```

要求：

1. dense size 与配置一致；
2. distance 使用 cosine；
3. dense 可 on-disk；
4. sparse index 可 on-disk；
5. sparse modifier 使用 IDF；
6. chunk ID 转为确定性 point ID；
7. payload 支持 attachment、collection、tag、year 过滤；
8. 按 document_id 或 attachment key 删除；
9. 文档变更时先删除旧 points，再 upsert 新 points；
10. delete + upsert 由单一 coordinator 执行；
11. 重复 upsert 幂等；
12. Qdrant 不可用时不提交 index success；
13. collection schema 不一致时停止；
14. 模型变化时创建新 collection；
15. 不在普通增量中使用 recreate；
16. 支持 snapshot；
17. snapshot 不是 chunks 的替代品。

collection 默认可采用：

```text
zotero_papers_qwen3_4b_1024_v2
```

不要覆盖旧版已存在的 `v1`，除非用户明确要求。

---

## 十五、Hybrid Retrieval

实现：

```text
dense top-k
+
BM25 top-k
→ RRF
→ optional reranker
```

默认：

```text
dense_prefetch = 50
sparse_prefetch = 50
fusion_top_k = 30
final_top_k = 10
```

要求：

- 复用 Qdrant FusionQuery/RRF，如果当前 client 版本支持；
- dense 和 sparse 结果去重；
- filter 同时应用于两个 prefetch；
- 支持 source、collection、tag、year、DOI、document_id；
- 结果包含标题、作者、页码、section、attachment key、PDF 路径；
- query instruction 与旧版保持一致，除非评测证明需要修改；
- instruction 变化必须记录；
- sparse-only 模式不依赖 GPU；
- reranker 不可用时可回退 hybrid；
- 返回 dense/sparse/fusion/rerank 的可解释信息，如果底层可取得。

---

## 十六、增量消费

支持：

### full

读取当前 snapshot，构建完整目标状态。

### incremental

顺序消费未处理 delta。

### reconcile

对比：

```text
snapshot
pipeline state
chunk artifacts
Qdrant dense
Qdrant sparse
```

要求：

- 每个 sync ID 只成功消费一次；
- delta hash 固定；
- 中途失败不标记成功；
- 重试幂等；
- 删除事件清理 Qdrant；
- missing delta 时停止并要求 reconcile；
- 重新扫描 attachment 不得错误改变 Zotero library version；
- metadata-only 更新尽量只更新 payload；
- content 变化重新 parse/chunk/embed/index。

---

## 十七、CLI

接入当前统一 CLI。

至少提供等价命令：

```bash
knowledgehub rag doctor --source zotero

knowledgehub rag plan \
  --source zotero \
  --gpu-mode dual

knowledgehub rag ingest \
  --source zotero \
  --full \
  --gpu-mode dual \
  --gpu-ids 0,1 \
  --limit 5

knowledgehub rag ingest \
  --source zotero \
  --incremental \
  --gpu-mode dual

knowledgehub rag ingest \
  --source zotero \
  --resume

knowledgehub rag parse \
  --source zotero \
  --gpu-mode dual \
  --gpu-ids 0,1

knowledgehub rag embed \
  --source zotero \
  --endpoints http://127.0.0.1:8080,http://127.0.0.1:8082

knowledgehub rag query \
  "红外小目标检测中如何提高低信噪比下的检测性能？" \
  --source zotero \
  --mode hybrid \
  --reranker quality \
  --top-k 10

knowledgehub rag validate --source zotero

knowledgehub rag benchmark \
  --source zotero \
  --stage parsing \
  --compare single,dual \
  --limit 100

knowledgehub rag benchmark \
  --source zotero \
  --stage embedding \
  --compare single,dual \
  --limit 100
```

单卡：

```bash
knowledgehub rag ingest \
  --source zotero \
  --full \
  --gpu-mode single \
  --gpu-ids 0
```

CPU：

```bash
knowledgehub rag ingest \
  --source zotero \
  --full \
  --gpu-mode cpu \
  --limit 2
```

要求：

- 具体命令适配当前仓库；
- 最终手册必须使用真实命令；
- CLI 只做参数解析；
- 支持 `--limit`；
- 支持 `--document-id`；
- 支持 `--attachment-key`；
- 支持 `--dry-run`；
- 支持 `--force`；
- 支持 `--prune`；
- 支持 `--resume`；
- 支持 `--gpu-mode`；
- 支持 `--gpu-ids`；
- 退出码可靠。

---

## 十八、Search API 和 Reranker API

参考旧版接口，但接入统一 service。

Search API 至少支持：

```text
query
mode
limit
prefetch_limit
collection_key
tag
year_from
year_to
use_reranker
reranker_profile
```

要求：

- 认证 key；
- loopback；
- `/health`；
- `/search`；
- Search API 不直接加载 embedding 模型；
- Search API 不直接加载 reranker；
- 调用统一 RetrievalService；
- hybrid、sparse 模式；
- reranker profile；
- endpoint health；
- API key 不进日志；
- 返回模型和 collection 版本信息；
- 旧版请求格式尽量向后兼容。

Reranker API：

- 明确 `--device cuda:0` 或通过可见 GPU 绑定；
- 不直接调用无参数 `.cuda()`；
- 支持 0.6B 和 4B；
- 支持 batch 自动回退；
- 支持 max length；
- 支持 health；
- 支持 API key；
- 记录实际 GPU；
- 支持优雅退出。

---

## 十九、Benchmark 和双卡验收

创建：

```text
scripts/benchmark_rag_gpus.py
```

或统一 CLI benchmark。

至少运行：

### 19.1 parser benchmark

样本：

```text
1 篇
20 篇
100 篇
```

比较：

```text
single GPU 0
single GPU 1
dual GPU 0,1
```

记录：

- 文档数；
- PDF 页数；
- 总耗时；
- pages/s；
- documents/s；
- GPU 0 利用率；
- GPU 1 利用率；
- 峰值显存；
- 失败数；
- 输出 fingerprint 一致性。

### 19.2 embedding benchmark

比较：

```text
1 TEI endpoint
2 TEI endpoints
```

记录：

- chunk 数；
- token 数；
- batches；
- texts/s；
- tokens/s；
- 平均延迟；
- P95 延迟；
- 每 endpoint 工作量；
- 峰值显存；
- 失败和重试；
- 向量一致性。

### 19.3 online benchmark

比较：

```text
embedding only
hybrid
hybrid + reranker 0.6B
hybrid + reranker 4B
```

记录：

- query latency；
- embedding latency；
- Qdrant latency；
- reranker latency；
- top-k；
- GPU 显存；
- GPU 利用率。

要求：

- 不预设双卡一定达到 2 倍；
- 双卡必须证明两张 GPU 都有工作量；
- 如果提升不明显，分析 CPU tokenization、PDF IO、Qdrant 写入或 batch 太小等瓶颈；
- benchmark 结果写入：

```text
RAG_DATA_DIR/build/benchmarks/
```

- 生成 JSON 和 Markdown 摘要；
- 不因为 benchmark 修改正式 collection；
- 使用独立 smoke/benchmark collection。

---

## 二十、测试

普通单元测试不得依赖真实 GPU。

使用：

- fake parser；
- fake embedder；
- fake endpoint pool；
- fake Qdrant adapter；
- mock manifest；
- mock delta；
- 临时 SQLite；
- 小 PDF fixture。

至少覆盖：

1. full ingest；
2. 第二次运行全跳过；
3. content changed；
4. metadata changed；
5. delete；
6. parser version changed；
7. chunk config changed；
8. embedding revision changed；
9. deterministic chunk ID；
10. deterministic Qdrant point ID；
11. single GPU config；
12. dual GPU config；
13. invalid GPU ID；
14. one GPU unavailable；
15. dual parser deterministic partition；
16. 两 worker 不重复文档；
17. parser worker crash；
18. parser worker resume；
19. endpoint pool round-robin；
20. endpoint pool least-outstanding；
21. endpoint failure quarantine；
22. dual endpoint failover；
23. 两 endpoint vector consistency mock；
24. batch reduction；
25. dense + sparse upsert；
26. delete by document；
27. RRF；
28. reranker fallback；
29. sparse-only no GPU；
30. API key redaction；
31. path safety；
32. reconcile；
33. delta exactly-once；
34. state transaction；
35. old manifest adapter，如果确实需要；
36. current manifest adapter；
37. old CLI behavior compatibility where retained。

GPU integration test 单独标记：

```text
integration
gpu
dual_gpu
manual
```

不得阻塞普通 CI。

---

## 二十一、真实小规模 Smoke Test

实现后，使用真实 current Zotero snapshot：

### 21.1 一篇

- 双卡模式只需要验证调度可启动；
- parse 一篇；
- chunk；
- 两个 TEI endpoint health；
- embedding；
- Qdrant benchmark collection；
- hybrid query；
- rerank；
- 第二次运行跳过。

### 21.2 20 篇

- 检查双 parser worker；
- 检查两个 TEI endpoint 都收到请求；
- 检查 Qdrant point 数；
- 检查失败恢复。

### 21.3 100 篇

- 比较 single 与 dual；
- 记录性能；
- 检查输出一致性；
- 人工使用至少 20 个研究问题验证 top-10。

不得在开发阶段自动对全部真实文献运行 embedding。

正式 collection 与 smoke collection 分离，例如：

```text
zotero_papers_qwen3_4b_1024_v2
zotero_papers_qwen3_4b_1024_smoke
```

---

## 二十二、初始化和环境检查脚本

创建：

```text
scripts/bootstrap_zotero_rag.sh
scripts/inspect_rag_environment.py
```

检查：

- Python；
- PyTorch；
- CUDA；
- 两张 3090；
- 每张显存；
- Docker；
- Docker Compose；
- NVIDIA Container Toolkit；
- 容器内 GPU 0；
- 容器内 GPU 1；
- Docling；
- Transformers；
- Qdrant client；
- FastEmbed；
- source manifest；
- ready PDF 数；
- 磁盘空间；
- 端口占用；
- Qdrant；
- TEI endpoints；
- reranker；
- `.env`；
- 写权限。

支持：

```text
--dry-run
```

不得：

- 自动 sudo；
- 自动安装系统组件；
- 自动删除；
- 自动跑全量；
- 自动下载 4B/8B 模型，除非用户显式确认；
- 修改 WebDAV 源。

---

## 二十三、中文逐步操作手册

必须创建：

```text
docs/guides/BUILD_ZOTERO_RAG_DUAL_3090.zh-CN.md
```

主 README 增加链接。

该手册必须是用户可逐步执行的实际操作文件。

必须包括：

### 23.1 当前最终架构

画出：

```text
Zotero source
→ snapshot/delta
→ dual GPU parser
→ Parquet chunks
→ dual TEI embedding
→ Qdrant dense+BM25
→ RRF
→ GPU reranker
→ Search API
```

### 23.2 目录说明

明确：

- Git 代码目录；
- Zotero source 目录；
- RAG data 目录；
- Qdrant 目录；
- model cache；
- parsed；
- chunks；
- state；
- snapshot；
- logs；
- 哪些只读；
- 哪些可重建；
- 哪些不能删除。

### 23.3 变量替换表

至少包含：

```text
配置键
所在文件
作用
如何获得
示例
是否必须修改
是否敏感
双卡/单卡影响
```

必须覆盖：

```text
KH_GPU_MODE
KH_GPU_IDS
KH_PARSE_GPU_IDS
KH_EMBED_GPU_IDS
KH_EMBED_ENDPOINTS
KH_RERANK_GPU_ID

KH_ZOTERO_SNAPSHOT_PATH
KH_ZOTERO_DELTA_DIR
KH_RAG_DATA_DIR
KH_QDRANT_STORAGE_DIR
KH_MODEL_CACHE_DIR

KH_EMBEDDING_MODEL
KH_EMBEDDING_REVISION
KH_EMBEDDING_DIM
KH_EMBEDDING_BATCH_SIZE
KH_EMBEDDING_MAX_BATCH_TOKENS

KH_RERANKER_MODEL_LIGHT
KH_RERANKER_MODEL_QUALITY
KH_RERANKER_BATCH_SIZE

KH_QDRANT_URL
KH_QDRANT_COLLECTION
KH_TEI_API_KEY
KH_RERANKER_API_KEY
KH_SEARCH_API_KEY
```

不得通过“修改第几行”说明。

### 23.4 新建目录

给出准确命令。

说明：

- 哪些可由程序自动创建；
- 哪些需要 sudo；
- 如何 chown；
- 如何确认 resolved path；
- 如何确认没有写入 `/data/Nutstore/zotero`。

### 23.5 识别两张 GPU

给出：

```bash
nvidia-smi -L
nvidia-smi
python -c ...
docker run ...
```

分别验证 GPU 0 和 GPU 1。

### 23.6 配置 dual profile

写清：

- `.env.example` 复制；
- `dual_3090.yaml`；
- GPU 0/1；
- 两个 TEI endpoint；
- reranker GPU；
- model revision；
- collection；
- API keys；
- 配置优先级；
- 配置检查命令。

### 23.7 配置 single profile

分别给出：

```text
仅 GPU 0
仅 GPU 1
```

说明如何停掉第二个 TEI 和 reranker。

### 23.8 启动 Qdrant

包括：

- compose；
- health；
- storage；
- 日志；
- stop；
- snapshot；
- 恢复；
- loopback 安全。

### 23.9 模型预下载

说明如何避免两个 TEI 容器同时首次下载产生冲突。

建议流程：

1. 启动 embedding-gpu0；
2. 等待模型缓存完成；
3. 停止或保持；
4. 启动 embedding-gpu1；
5. 检查两个 health。

### 23.10 一篇冒烟测试

真实命令：

- source doctor；
- source sync；
- source validate；
- RAG doctor；
- plan；
- parse；
- inspect Markdown；
- start dual TEI；
- embed/index；
- query；
- rerank；
- rerun idempotency。

### 23.11 20/100 篇 benchmark

精确命令和输出路径。

说明如何判断：

- 两张卡都在工作；
- 是否 CPU/IO 限制；
- batch 是否过大；
- 是否 OOM；
- single 与 dual 的区别。

### 23.12 全量双卡构建

精确顺序：

```text
停止在线 embedding/reranker
启动 dual parser
完成 parse/chunk
停止 parser
启动两个 embedding replica
完成 dense+sparse index
validate
snapshot
启动在线 profile
```

说明：

- 如何 tmux；
- 如何查看进度；
- 如何中断；
- 如何 resume；
- 如何查看失败；
- 如何重试；
- 不估计固定耗时。

### 23.13 单卡全量构建

同样提供完整顺序。

### 23.14 日常增量

真实顺序：

```text
Zotero source sync
→ resolve attachments
→ source validate
→ RAG incremental ingest
→ RAG validate
→ optional snapshot
```

说明 metadata-only 和 content changed 的区别。

### 23.15 在线双卡服务

推荐：

```text
GPU 0 embedding
GPU 1 reranker
CPU Qdrant/Search API
```

给出启动、health、query、停止命令。

### 23.16 debug/训练模式

说明如何释放两张 3090：

- stop embedding-gpu0；
- stop embedding-gpu1；
- stop reranker；
- Qdrant 可继续；
- sparse-only 可继续。

### 23.17 模型切换

分别说明：

- 4B → 8B；
- reranker 0.6B → 4B；
- 1024 → 其他 MRL 维度；
- 新 collection；
- benchmark；
- API 切换；
- 回滚；
- 不混用向量。

### 23.18 故障排查

至少覆盖：

- 只识别一张 3090；
- GPU ID 顺序变化；
- Docker 看不到 GPU 1；
- 两个 TEI 都跑到 GPU 0；
- 双卡只有一张有利用率；
- model cache race；
- TEI health 失败；
- embedding OOM；
- reranker OOM；
- Docling OOM；
- worker crash；
- parser 输出不一致；
- endpoint 输出不一致；
- Qdrant dimension mismatch；
- collection 已存在但 schema 不同；
- delta 缺失；
- state lock；
- 磁盘不足；
- query 无结果；
- BM25 有结果而 dense 无结果；
- 中文检索差；
- reranker 变差；
- 第二次运行仍重复处理。

每个问题必须有：

```text
检查命令
可能原因
修复步骤
是否需要重建
```

### 23.19 最终 checklist

至少包括：

```text
[ ] 两张 RTX 3090 均被识别
[ ] GPU 0 容器只看到 GPU 0
[ ] GPU 1 容器只看到 GPU 1
[ ] 两个 parser worker 都完成任务
[ ] 两个 TEI endpoint 都完成 embedding
[ ] 双卡和单卡输出 fingerprint 一致
[ ] Qwen3-Embedding-4B revision 固定
[ ] 1024 维向量重新归一化
[ ] Qdrant dense+BM25 数量正确
[ ] hybrid query 有结果
[ ] quality reranker 可用
[ ] single GPU profile 可用
[ ] sparse-only 模式可用
[ ] 第二次 ingest 幂等
[ ] delta delete 能清理 points
[ ] snapshot 已生成
[ ] chunks 已备份
[ ] .env 未提交
```

---

## 二十四、systemd

提供但不自动安装：

```text
knowledgehub-zotero-source.service
knowledgehub-zotero-rag-incremental.service
knowledgehub-zotero-rag-incremental.timer
knowledgehub-zotero-search.service
```

离线全量双卡构建不建议由短周期 timer 自动触发。

日常增量应串行：

```text
source sync
→ RAG incremental ingest
→ validate
```

要求：

- EnvironmentFile；
- 绝对路径；
- oneshot；
- lock；
- timeout；
- 不含真实 key；
- 不自动 sudo；
- 不让两个 ingest 并发；
- 在线 GPU service 与离线全量任务冲突时明确停止或拒绝运行。

---

## 二十五、验收标准

实现后必须实际执行：

1. 格式化；
2. lint；
3. 类型检查；
4. 单元测试；
5. mock E2E；
6. fake dual GPU scheduler test；
7. Docker Compose config validation；
8. 单卡 GPU 0 smoke；
9. 单卡 GPU 1 smoke；
10. 双卡 parser smoke；
11. 双 TEI smoke；
12. 两 TEI 向量一致性；
13. 1 文档 E2E；
14. 20 文档 E2E；
15. 100 文档 single/dual benchmark；
16. hybrid query；
17. light reranker；
18. quality reranker；
19. sparse-only；
20. 第二次 ingest 幂等；
21. metadata changed；
22. content changed；
23. delete；
24. resume；
25. reconcile；
26. validate；
27. Qdrant snapshot；
28. Git status；
29. 确认未提交 PDF、ZIP、模型、Qdrant、SQLite、logs、`.env`。

最终报告必须包括：

- 当前架构；
- 旧版复用内容；
- 旧版被替换内容；
- 新增文件；
- 修改文件；
- 新增依赖；
- 默认 embedding 和 reranker；
- 双卡任务分配；
- 单卡运行方式；
- Compose profiles；
- CLI；
- 测试结果；
- 单卡 benchmark；
- 双卡 benchmark；
- GPU 0/1 实际工作量；
- 显存峰值；
- Qdrant point 数；
- chunk 数；
- 幂等验证；
- 增量验证；
- 查询示例；
- 未实现内容；
- 当前限制；
- 用户第一条应执行的命令；
- 中文指导文件路径。

---

## 二十六、最终边界

必须保持：

```text
Zotero source
    负责同步、附件和 manifest

统一 RAG pipeline
    负责 parse、chunk、embedding、index、retrieval

GPU services
    负责模型推理

Qdrant
    负责 dense+sparse 在线索引
```

不得：

- 重新实现 Zotero source；
- 创建第二套 Zotero manifest；
- 直接从 WebDAV ZIP 建索引；
- 把旧版单文件整体复制为新架构；
- 仅用 `gpus: all` 假装实现双卡；
- 在两个 GPU worker 中重复处理同一文档；
- 让两个 index worker并发执行同文档 delete/upsert；
- 将 4B 和 8B 向量混入同一 collection；
- 将不同 revision 的向量混入同一 collection；
- 将不同维度的向量混入同一 collection；
- 自动对全部 PDF 执行 OCR；
- 自动对全部真实文献开始 embedding；
- 自动修改系统配置；
- 自动 sudo；
- 自动删除 source 数据；
- 大规模重构无关代码；
- 只输出设计文档而不实现；
- 留下空接口和 `pass`。

完成仓库分析后，直接实现代码、测试、Compose、systemd 示例、benchmark 和中文操作手册。
