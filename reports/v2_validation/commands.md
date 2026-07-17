# 实际执行命令

以下均使用 `$CONDA_PREFIX/bin/knowledgehub` 或同环境 Python；本机环境绝对路径已脱敏。

## 基线与测试

```bash
git status --short --branch
knowledgehub release validate
knowledgehub validate all --offline
python -m pytest tests/core tests/deploy tests/sources -q
python -m pytest tests/rag -q
python -m pytest tests/multi_rag -q
python -m pytest tests/v2 -q
python -m pytest tests/mcp -q
python -m pytest -q
ruff check src tests scripts
mypy src/knowledgehub
```

## 数据、环境与查询

```bash
knowledgehub source list
knowledgehub environment capture --name v2-validation-20260717
knowledgehub sync code --library transformers --version 5.13.1   # 连续两次
knowledgehub query literature "retrieval augmented generation" --top-k 3
knowledgehub query code "..." --library transformers --version 5.13.1 --evidence-envelope
knowledgehub query writing "..." --section Introduction --writing-function research_gap
knowledgehub symbol inspect transformers 5.13.1 PreTrainedModel.from_pretrained
knowledgehub symbol compare transformers 5.13.0 5.13.1 transformers.models.auto.auto_factory._LazyAutoMapping.register
knowledgehub evaluate run --mode live --profile v1
knowledgehub evaluate run --mode live --profile v2
knowledgehub evaluate compare <v1.json> <v2.json> --thresholds configs/evaluation/v2.yaml
```

## 隔离派生、候选和回滚

```bash
knowledgehub --hub-config reports/v2_validation/fixtures/knowledgehub_validation.yaml derive writing --limit 3
knowledgehub --hub-config reports/v2_validation/fixtures/knowledgehub_validation.yaml build code --library transformers --version 5.13.1 --limit 10 --candidate-collection knowledgehub_code_validation_20260717
knowledgehub index snapshot code
knowledgehub index stage code knowledgehub_code_validation_20260717
knowledgehub index promote code --yes
knowledgehub index rollback-alias code --yes
knowledgehub index rollback code 20260716T172110-knowledgehub_code_qwen3_4b_1024_v1-8688692131812382-2026-07-16-17-21-09.snapshot --yes
knowledgehub validate all
```

上面的 snapshot rollback 是初轮验收时执行的旧 Qdrant-only 契约。P1-1 修复后必须提供全新 target collection；legacy snapshot 还需显式 `--allow-qdrant-only`。

## P1-1 修复后实机命令

```bash
knowledgehub build code --library transformers --version 5.13.1 --limit 1 \
  --candidate-collection knowledgehub_code_atomic_validation_20260717_01 --dry-run
knowledgehub build code --library transformers --version 5.13.1 --limit 1 \
  --candidate-collection knowledgehub_code_atomic_validation_20260717_01
knowledgehub index validate-candidate code knowledgehub_code_atomic_validation_20260717_01
knowledgehub index alias-status code
knowledgehub validate all
pytest -q
ruff check .
mypy --strict src/knowledgehub
```

维护窗口实际执行：

```bash
# 此路径发现会扩大 active source scope，已安全中断并保留 failed candidate：
knowledgehub build code --all \
  --candidate-collection knowledgehub_code_release_20260717_maintenance_01

# 等价复制当前完整 active release：
knowledgehub index bootstrap-candidate code knowledgehub_code_release_20260717_maintenance_02
knowledgehub index stage code knowledgehub_code_release_20260717_maintenance_02 \
  --release-manifest /data/KnowledgeHub/code/releases/code/knowledgehub_code_release_20260717_maintenance_02/release.json
knowledgehub index promote code --yes
knowledgehub validate all
knowledgehub query code "Where is PreTrainedModel.from_pretrained defined?" \
  --library transformers --version 5.13.1 --symbol PreTrainedModel.from_pretrained \
  --top-k 3 --evidence-envelope
knowledgehub index snapshot code
knowledgehub index rollback-alias code --yes
knowledgehub validate all
knowledgehub index rollback code \
  20260716T192046-knowledgehub_code_release_20260717_maintenance_02-8688692131812382-2026-07-16-19-20-46.snapshot \
  --target-collection knowledgehub_code_recovery_20260717_maintenance_03 --yes
knowledgehub index validate-candidate code knowledgehub_code_recovery_20260717_maintenance_03
```

## 仓库、Debug、MCP 与服务

```bash
knowledgehub repository validate <cloneofsimo/lora> --output-root /data/KnowledgeHub/reports/v23
knowledgehub repository validate <state-spaces/s4> --output-root /data/KnowledgeHub/reports/v23
knowledgehub repository debug-log <repo> --log-file reports/v2_validation/fixtures/<case>.log
knowledgehub mcp doctor
knowledgehub mcp status
knowledgehub mcp validate
knowledgehub mcp tools
docker compose -f deploy/qdrant/compose.yaml -f deploy/gpu/compose.yaml ps -a
docker compose -f deploy/qdrant/compose.yaml -f deploy/gpu/compose.yaml build search-api
```

Search API 0.2.5 容器已由管理员执行以下命令完成切换，容器状态为 healthy；三库鉴权 smoke 已于 10:16 通过：

```bash
sudo docker compose --env-file /etc/knowledgehub/rag.env \
  -f deploy/gpu/compose.yaml --profile online-dual \
  up -d --no-deps --force-recreate search-api
```

本轮 P2/P3 source 修复后已重建 image，并再次执行上面的 force-recreate：

```bash
sudo docker compose --env-file /etc/knowledgehub/rag.env \
  -f deploy/gpu/compose.yaml --profile online-dual \
  build search-api
```

editable metadata 对齐命令已执行，随后已重启两个 MCP listener：

```bash
/home/lengmo/anaconda3/envs/rag/bin/python -m pip install --no-deps -e .
sudo systemctl restart knowledgehub-mcp-lan.service knowledgehub-mcp-tailscale.service
```
