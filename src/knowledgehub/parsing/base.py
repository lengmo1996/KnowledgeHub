"""Parser protocol and registry."""

from __future__ import annotations

from typing import Protocol

from knowledgehub.pipeline.models import ParsedDocument, SourceDocument


class PDFParser(Protocol):
    name: str
    version: str

    def parse(self, document: SourceDocument) -> ParsedDocument: ...


def create_parser(
    name: str,
    *,
    device: str,
    ocr: bool,
    num_threads: int,
) -> PDFParser:
    if name == "docling":
        from knowledgehub.parsing.docling_parser import DoclingParser

        return DoclingParser(device=device, ocr=ocr, num_threads=num_threads)
    if name == "pymupdf":
        from knowledgehub.parsing.pymupdf_parser import PyMuPDFParser

        return PyMuPDFParser()
    raise ValueError(f"unknown parser: {name}")
