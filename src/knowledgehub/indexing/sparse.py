"""FastEmbed BM25 adapter."""

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
            threads=config.parse_cpu_threads_per_worker,
            cuda=False,
            lazy_load=True,
        )

    def encode(self, texts: Sequence[str]) -> list[tuple[list[int], list[float]]]:
        result: list[tuple[list[int], list[float]]] = []
        for value in self._model.embed(list(texts)):
            result.append(
                (
                    [int(index) for index in value.indices.tolist()],
                    [float(weight) for weight in value.values.tolist()],
                )
            )
        return result

    def query(self, text: str) -> tuple[list[int], list[float]]:
        values = list(self._model.query_embed(text))
        if len(values) != 1:
            raise RuntimeError("sparse encoder returned an unexpected query batch")
        return (
            [int(index) for index in values[0].indices.tolist()],
            [float(weight) for weight in values[0].values.tolist()],
        )
