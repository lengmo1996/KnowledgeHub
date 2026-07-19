from __future__ import annotations

from types import SimpleNamespace

from knowledgehub.indexing.sparse import SparseEncoder, prepare_sparse_text


def test_sparse_preprocessing_preserves_non_cjk_text() -> None:
    assert prepare_sparse_text("report the AUROC improvement") == "report the AUROC improvement"


def test_sparse_preprocessing_appends_stable_unique_cjk_bigrams() -> None:
    prepared = prepare_sparse_text("红外小目标, 红外目标")

    assert prepared.startswith("红外小目标, 红外目标\n__kh_cjk_bigrams__ ")
    tokens = prepared.split("__kh_cjk_bigrams__ ", 1)[1].split()
    assert tokens == ["红外", "外小", "小目", "目标", "外目"]


def test_sparse_encoder_applies_same_preprocessing_to_documents_and_queries() -> None:
    observed: list[str] = []

    class Model:
        def embed(self, texts: list[str]):  # type: ignore[no-untyped-def]
            observed.extend(texts)
            return [SimpleNamespace(indices=[1], values=[1.0]) for _ in texts]

    encoder = SparseEncoder.__new__(SparseEncoder)
    encoder.model_name = "fixture"
    encoder._model = Model()

    assert encoder.encode(["红外目标", "plain text"]) == [([1], [1.0]), ([1], [1.0])]
    assert observed == [
        "红外目标\n__kh_cjk_bigrams__ 红外 外目 目标",
        "plain text",
    ]
