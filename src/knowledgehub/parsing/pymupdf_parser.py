"""Explicit PyMuPDF fallback for PDFs Docling cannot parse."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Any

from knowledgehub.core.hashing import sha256_json
from knowledgehub.pipeline.models import ParsedDocument, SourceDocument


class PyMuPDFParser:
    name = "pymupdf"

    def __init__(self) -> None:
        try:
            self.version = version("PyMuPDF")
            import pymupdf
        except (PackageNotFoundError, ImportError) as exc:
            raise RuntimeError("PyMuPDF fallback is not installed") from exc
        self._pymupdf: Any = pymupdf

    def parse(self, document: SourceDocument) -> ParsedDocument:
        pages: list[dict[str, Any]] = []
        markdown_parts: list[str] = []
        with self._pymupdf.open(document.pdf_path) as pdf:
            for index, page in enumerate(pdf):
                text = page.get_text("text").strip()
                pages.append({"page_no": index + 1, "text": text})
                if text:
                    markdown_parts.append(f"<!-- page:{index + 1} -->\n{text}")
            page_count = len(pdf)
        fingerprint = sha256_json(
            {
                "document_content": document.source_content_fingerprint,
                "parser": self.name,
                "parser_version": self.version,
            }
        )
        return ParsedDocument(
            document_id=document.document_id,
            parser_name=self.name,
            parser_version=self.version,
            parse_fingerprint=fingerprint,
            markdown="\n\n".join(markdown_parts),
            structured={"pages": pages},
            page_count=page_count,
        )
