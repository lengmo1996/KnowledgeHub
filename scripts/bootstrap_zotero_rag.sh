#!/usr/bin/env bash
set -euo pipefail

apply=false
preseed=false
download_quality=false
for arg in "$@"; do
  case "$arg" in
    --apply) apply=true ;;
    --preseed-model-cache) preseed=true ;;
    --download-quality) download_quality=true ;;
    --dry-run) ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

root=/data/KnowledgeHub
legacy=/home/lengmo/zotero_rag/model-cache/tei/models--Qwen--Qwen3-Embedding-4B
target="$root/model-cache/tei/models--Qwen--Qwen3-Embedding-4B"
legacy_light=/home/lengmo/.cache/huggingface/hub/models--Qwen--Qwen3-Reranker-0.6B

echo "mode=$([[ $apply == true ]] && echo apply || echo dry-run)"
echo "create=$root/{zotero,rag/zotero,qdrant,model-cache}"
echo "legacy_embedding_cache=$legacy"
echo "target_embedding_cache=$target"

if [[ $apply != true ]]; then
  exit 0
fi

mkdir -p "$root/zotero" "$root/rag/zotero" "$root/qdrant" "$root/model-cache/tei"
if [[ $preseed == true ]]; then
  [[ -d $legacy ]] || { echo "legacy embedding cache missing" >&2; exit 1; }
  cp -a --no-preserve=ownership "$legacy" "$root/model-cache/tei/"
  if [[ -d $legacy_light ]]; then
    mkdir -p "$root/model-cache/huggingface/hub"
    cp -a --no-preserve=ownership "$legacy_light" "$root/model-cache/huggingface/hub/"
  fi
fi

if [[ $download_quality == true ]]; then
  /home/lengmo/anaconda3/envs/rag/bin/hf download \
    Qwen/Qwen3-Reranker-4B \
    --revision 22e683669bc0f0bd69640a1354a6d0aebcfeede5 \
    --cache-dir "$root/model-cache/huggingface/hub"
fi

/home/lengmo/anaconda3/bin/conda run -n rag --no-capture-output \
  knowledgehub --config configs/rag/default.yaml rag doctor
