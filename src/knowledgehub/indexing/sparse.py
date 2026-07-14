"""FastEmbed BM25 sparse encoding."""

from __future__ import annotations

from typing import Any, Sequence

from knowledgehub.pipeline.config import RagConfig


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
        for value in self._model.embed(list(texts)):
            result.append(
                ([int(item) for item in value.indices], [float(item) for item in value.values])
            )
        if len(result) != len(texts):
            raise RuntimeError("sparse encoder returned an unexpected batch size")
        return result
