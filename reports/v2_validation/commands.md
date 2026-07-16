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

Search API 0.2.5 容器切换命令因 `/etc/knowledgehub/rag.env` root 权限而未执行成功；管理员应运行仓库现有 systemd/compose 启动命令。
