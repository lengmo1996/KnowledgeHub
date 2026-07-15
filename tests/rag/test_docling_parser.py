from __future__ import annotations

from types import SimpleNamespace

import pytest

from knowledgehub.parsing.docling_parser import (
    DoclingConversionError,
    DoclingParser,
    _conversion_error,
)


def test_clean_docling_conversion_is_accepted() -> None:
    result = SimpleNamespace(status=SimpleNamespace(value="success"), errors=[])

    assert _conversion_error(result) is None


@pytest.mark.parametrize("status", ["partial_success", "failure", "skipped", "pending"])
def test_non_success_docling_conversion_is_rejected(status: str) -> None:
    result = SimpleNamespace(status=SimpleNamespace(value=status), errors=[])

    assert _conversion_error(result) == (
        f"Docling conversion was not clean (status={status}, errors=0)"
    )


def test_docling_conversion_with_errors_is_rejected_even_if_status_is_success() -> None:
    error = SimpleNamespace(page_no=9, error_message="malformed PDF operation: rg")
    result = SimpleNamespace(status=SimpleNamespace(value="success"), errors=[error])

    assert _conversion_error(result) == (
        "Docling conversion was not clean (status=success, errors=1): "
        "page 9: malformed PDF operation: rg"
    )


def test_parser_raises_before_using_partial_docling_document() -> None:
    parser = object.__new__(DoclingParser)
    parser._converter = SimpleNamespace(
        convert=lambda _path: SimpleNamespace(
            status=SimpleNamespace(value="partial_success"),
            errors=[SimpleNamespace(page_no=9, error_message="page preprocessing failed")],
            document=object(),
        )
    )
    document = SimpleNamespace(pdf_path="broken.pdf")

    with pytest.raises(DoclingConversionError, match="page 9: page preprocessing failed"):
        parser.parse(document)
