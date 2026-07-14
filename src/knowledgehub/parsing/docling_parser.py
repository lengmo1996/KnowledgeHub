"""Docling PDF parser loaded lazily inside the assigned worker."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Any

from knowledgehub.chunking.fingerprints import document_parse_fingerprint
from knowledgehub.pipeline.models import ParsedDocument, SourceDocument


def _dump(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        result = value.model_dump(mode="json")
    elif hasattr(value, "export_to_dict"):
        result = value.export_to_dict()
    else:
        raise TypeError("Docling document does not expose a serializable representation")
    if not isinstance(result, dict):
        raise TypeError("Docling document serialization is not an object")
    return result


class DoclingParser:
    name = "docling"

    def __init__(self, *, device: str, ocr: bool, num_threads: int) -> None:
        try:
            self.version = version("docling")
        except PackageNotFoundError as exc:
            raise RuntimeError("Docling is not installed") from exc
        from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        devices = {
            "auto": AcceleratorDevice.AUTO,
            "cpu": AcceleratorDevice.CPU,
            "cuda": AcceleratorDevice.CUDA,
        }
        if device not in devices:
            raise ValueError(f"unsupported Docling device: {device}")
        options = PdfPipelineOptions()
        options.accelerator_options = AcceleratorOptions(
            num_threads=num_threads, device=devices[device]
        )
        options.do_ocr = ocr
        self._converter = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
        )
        self._config = {"device_semantics": device, "ocr": ocr, "num_threads": num_threads}

    def parse(self, document: SourceDocument) -> ParsedDocument:
        result = self._converter.convert(document.pdf_path)
        native = result.document
        structured = _dump(native)
        markdown = native.export_to_markdown()
        pages = getattr(native, "pages", {})
        page_count = len(pages) if hasattr(pages, "__len__") else 0
        fingerprint = document_parse_fingerprint(
            document,
            parser_name=self.name,
            parser_version=self.version,
            ocr=bool(self._config["ocr"]),
        )
        return ParsedDocument(
            document_id=document.document_id,
            parser_name=self.name,
            parser_version=self.version,
            parse_fingerprint=fingerprint,
            markdown=markdown,
            structured=structured,
            page_count=page_count,
            native=native,
        )
