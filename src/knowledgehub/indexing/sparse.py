"""FastEmbed BM25 sparse encoding."""

from __future__ import annotations

import re
from typing import Any, Sequence

from knowledgehub.pipeline.config import RagConfig

SPARSE_PREPROCESSING_VERSION = "cjk-bigram-v1"
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")


def prepare_sparse_text(text: str) -> str:
    """Preserve source text and append deterministic CJK tokens for BM25.

    FastEmbed's default BM25 tokenizer treats long CJK runs as a few opaque
    tokens, so a semantically exact Chinese query can share no sparse terms
    with its source passage.  Space-delimited character bigrams provide a
    language-local lexical path while retaining every original token for
    backwards compatibility with already indexed content.
    """

    tokens: list[str] = []
    seen: set[str] = set()
    for match in _CJK_RUN.finditer(text):
        value = match.group(0)
        candidates = [value] if len(value) == 1 else [value[index : index + 2] for index in range(len(value) - 1)]
        for token in candidates:
            if token not in seen:
                seen.add(token)
                tokens.append(token)
    if not tokens:
        return text
    return f"{text}\n__kh_cjk_bigrams__ {' '.join(tokens)}"


class SparseEncoder:
    def __init__(self, config: RagConfig) -> None:
        from fastembed import SparseTextEmbedding

        self.model_name = config.sparse_model
        self._model: Any = SparseTextEmbedding(
            model_name=config.sparse_model,
            cache_dir=str(config.model_cache_dir / "fastembed"),
            providers=["CPUExecutionProvider"],
            lazy_load=True,
        )

    def encode(self, texts: Sequence[str]) -> list[tuple[list[int], list[float]]]:
        result: list[tuple[list[int], list[float]]] = []
        prepared = [prepare_sparse_text(text) for text in texts]
        for value in self._model.embed(prepared):
            result.append(
                ([int(item) for item in value.indices], [float(item) for item in value.values])
            )
        if len(result) != len(texts):
            raise RuntimeError("sparse encoder returned an unexpected batch size")
        return result
