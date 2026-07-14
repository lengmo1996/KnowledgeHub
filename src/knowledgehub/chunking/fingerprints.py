"""Semantic fingerprints for parser, chunker and embedding stages."""

from __future__ import annotations

from knowledgehub.core.hashing import sha256_json
from knowledgehub.pipeline.config import RagConfig
from knowledgehub.pipeline.models import SourceDocument


def parser_config_fingerprint(config: RagConfig, parser_version: str) -> str:
    return sha256_json(
        {
            "ocr": config.ocr_enabled,
            "parser": config.parser_name,
            "parser_fallback": config.parser_fallback,
            "parser_version": parser_version,
        }
    )


def chunk_config_fingerprint(config: RagConfig, tokenizer_revision: str | None = None) -> str:
    return sha256_json(
        {
            "chunker": "docling.HybridChunker",
            "max_tokens": config.chunk_max_tokens,
            "merge_peers": config.chunk_merge_peers,
            "tokenizer": config.embedding_model,
            "tokenizer_revision": tokenizer_revision or config.embedding_revision,
        }
    )


def embedding_config_fingerprint(config: RagConfig) -> str:
    return sha256_json(
        {
            "dim": config.embedding_dim,
            "dtype": config.embedding_dtype,
            "max_length": config.embedding_max_length,
            "model": config.embedding_model,
            "normalize": config.embedding_normalize,
            "pooling": "model-default-last-token",
            "query_instruction": config.embedding_query_instruction,
            "revision": config.embedding_revision,
            "template": "chunk.text.v1",
        }
    )


def sparse_config_fingerprint(config: RagConfig) -> str:
    return sha256_json({"idf": True, "model": config.sparse_model, "text": "chunk.text.v1"})


def document_parse_fingerprint(
    document: SourceDocument,
    *,
    parser_name: str,
    parser_version: str,
    ocr: bool,
) -> str:
    return sha256_json(
        {
            "document_content": document.source_content_fingerprint,
            "ocr": ocr,
            "parser": parser_name,
            "parser_version": parser_version,
        }
    )


def document_chunk_fingerprint(config: RagConfig, parse_fingerprint: str) -> str:
    return sha256_json(
        {"chunk_config": chunk_config_fingerprint(config), "parse": parse_fingerprint}
    )


def document_embedding_fingerprint(config: RagConfig, chunk_fingerprint: str) -> str:
    return sha256_json(
        {"chunk": chunk_fingerprint, "embedding_config": embedding_config_fingerprint(config)}
    )
