"""Explicit read-only MCP tool registry and execution facade."""

from __future__ import annotations

import dataclasses
import json
import re
from importlib.metadata import version
from typing import Any, Awaitable, Callable

import anyio
from pydantic import ValidationError

from knowledgehub.mcp.config import MCPConfig
from knowledgehub.mcp.resilience import CircuitBreaker
from knowledgehub.mcp.schemas import INPUT_MODELS, SearchInput
from knowledgehub.retrieval.models import SearchRequest
from mcp import types

_INJECTION = re.compile(
    r"(?i)(ignore (all|any|the) (previous|prior) instructions|system prompt|developer message|"
    r"you are (chatgpt|an ai)|exfiltrat|reveal.*secret|执行.*指令|忽略.*指令)"
)
_PAYLOAD_FIELDS = {
    "attachment_key",
    "chunk_id",
    "chunk_index",
    "collection_keys",
    "collection_paths",
    "document_id",
    "doi",
    "page_end",
    "page_start",
    "section_path",
    "source",
    "tags",
    "text",
    "text_sha256",
    "title",
    "token_count",
    "year",
}

TOOL_DESCRIPTIONS = {
    "rag_search": "Search indexed document chunks with dense, sparse, or Qdrant RRF hybrid retrieval.",
    "rag_get_chunk": "Read one already-indexed chunk by its known chunk ID.",
    "rag_get_document": "Read document metadata and a paginated chunk index; full text is excluded.",
    "rag_get_neighbors": "Read bounded neighboring chunks around a known chunk ID.",
    "rag_resolve_reference": "Resolve exactly one DOI, citation key, Zotero key, or title to candidates.",
    "rag_list_facets": "List paginated collection, tag, year, or source values and counts.",
    "rag_status": "Return sanitized listener, dependency, limits, and build status.",
}


class ToolError(RuntimeError):
    def __init__(self, code: str, message: str, *, recoverable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.recoverable = recoverable


class ToolRegistry:
    def __init__(self, service: Any, config: MCPConfig, *, token_store: Any | None = None) -> None:
        self.service = service
        self.config = config
        self.token_store = token_store
        self.request_semaphore = anyio.Semaphore(config.max_concurrent_requests)
        self.embedding_semaphore = anyio.Semaphore(config.max_concurrent_embeddings)
        self.reranker_semaphore = anyio.Semaphore(config.max_concurrent_rerankers)
        self.qdrant_breaker = CircuitBreaker()
        self.embedding_breaker = CircuitBreaker()
        self.reranker_breaker = CircuitBreaker()
        self.service.circuit_breakers = {
            "qdrant": self.qdrant_breaker,
            "embedding": self.embedding_breaker,
            "reranker": self.reranker_breaker,
        }
        self._handlers: dict[str, Callable[[Any], Awaitable[dict[str, Any]]]] = {
            "rag_search": self._search,
            "rag_get_chunk": self._get_chunk,
            "rag_get_document": self._get_document,
            "rag_get_neighbors": self._get_neighbors,
            "rag_resolve_reference": self._resolve_reference,
            "rag_list_facets": self._list_facets,
            "rag_status": self._status,
        }

    def definitions(self) -> list[types.Tool]:
        annotations = types.ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
        return [
            types.Tool(
                name=name,
                title=name.replace("rag_", "KnowledgeHub ").replace("_", " ").title(),
                description=TOOL_DESCRIPTIONS[name],
                inputSchema=model.model_json_schema(),
                annotations=annotations,
            )
            for name, model in INPUT_MODELS.items()
        ]

    async def call(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        model_type = INPUT_MODELS.get(name)
        handler = self._handlers.get(name)
        if model_type is None or handler is None:
            return self._error("unknown_tool", "Unknown read-only tool.", recoverable=False)
        try:
            parsed = model_type.model_validate(arguments)
            with anyio.fail_after(self.config.request_timeout_seconds):
                async with self.request_semaphore:
                    payload = await handler(parsed)
            payload = self._bound(payload)
        except ValidationError as exc:
            return self._error("invalid_arguments", exc.errors()[0]["msg"], recoverable=True)
        except TimeoutError:
            return self._error("deadline_exceeded", "The request deadline elapsed.")
        except ToolError as exc:
            return self._error(exc.code, str(exc), recoverable=exc.recoverable)
        except Exception as exc:
            return self._error(_safe_code(exc), "The requested operation is unavailable.")
        text = _fallback(name, payload)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)], structuredContent=payload
        )

    async def _search(self, value: SearchInput) -> dict[str, Any]:
        reranker = value.reranker
        if reranker == "auto":
            reranker = self.service.config.reranker_profile
        use_reranker = reranker in {"light", "quality"}
        filters = value.filters
        request = SearchRequest(
            query=value.query,
            mode=value.mode,
            limit=value.limit,
            prefetch_limit=value.prefetch_limit,
            collection_key=filters.collection,
            tag=filters.tag,
            year_from=filters.year_from,
            year_to=filters.year_to,
            doi=filters.doi,
            document_id=filters.document_id,
            attachment_key=filters.attachment_key,
            source=filters.source,
            use_reranker=use_reranker,
            reranker_profile=str(reranker),
            fallback_policy=value.fallback,
        )
        async with self.embedding_semaphore:
            if use_reranker:
                async with self.reranker_semaphore:
                    response = await self.service.asearch(request)
            else:
                response = await self.service.asearch(request)
        raw = dataclasses.asdict(response)
        warnings = list(raw["warnings"])
        hits = []
        for hit in raw["hits"]:
            payload = _mark_content(dict(hit["payload"]), value.max_chars_per_hit, warnings)
            hit["payload"] = payload
            if value.neighbors.before or value.neighbors.after:
                point_id = hit["point_id"]
                neighbors = await self.service.aget_neighbors(
                    point_id,
                    before=value.neighbors.before,
                    after=value.neighbors.after,
                )
                hit["neighbors"] = [
                    _mark_content(dict(item), value.max_chars_per_hit, warnings)
                    for item in neighbors
                ]
            hits.append(hit)
        raw["hits"] = hits
        raw["warnings"] = sorted(set(warnings))
        return {"ok": True, "result": raw}

    async def _get_chunk(self, value: Any) -> dict[str, Any]:
        chunk = await self.service.aget_chunk(value.chunk_id)
        if chunk is None:
            raise ToolError("not_found", "Chunk was not found.")
        warnings: list[str] = []
        return {
            "ok": True,
            "chunk": _mark_content(chunk, value.max_chars, warnings),
            "warnings": warnings,
        }

    async def _get_document(self, value: Any) -> dict[str, Any]:
        document = await self.service.aget_document(value.document_id)
        if document is None:
            raise ToolError("not_found", "Document was not found.")
        if not value.include_abstract:
            document["abstract"] = None
        chunks = document.pop("chunks", [])
        document["chunk_index"] = chunks[
            value.chunk_cursor : value.chunk_cursor + value.chunk_limit
        ]
        document["next_cursor"] = (
            value.chunk_cursor + value.chunk_limit
            if value.chunk_cursor + value.chunk_limit < len(chunks)
            else None
        )
        return {"ok": True, "document": document}

    async def _get_neighbors(self, value: Any) -> dict[str, Any]:
        chunks = await self.service.aget_neighbors(
            value.chunk_id, before=value.before, after=value.after
        )
        if not chunks:
            raise ToolError("not_found", "Chunk was not found.")
        warnings: list[str] = []
        return {
            "ok": True,
            "chunks": [
                _mark_content(value_, value.max_chars_per_chunk, warnings) for value_ in chunks
            ],
            "warnings": sorted(set(warnings)),
        }

    async def _resolve_reference(self, value: Any) -> dict[str, Any]:
        candidates = self.service.resolve_reference(**value.model_dump())
        return {
            "ok": True,
            "resolution": "not_found"
            if not candidates
            else "unique"
            if len(candidates) == 1
            else "ambiguous",
            "candidates": candidates,
        }

    async def _list_facets(self, value: Any) -> dict[str, Any]:
        result = self.service.list_facets(value.facet, cursor=value.cursor, limit=value.limit)
        return {"ok": True, "result": result}

    async def _status(self, value: Any) -> dict[str, Any]:
        dependency_status = await self.service.astatus()
        catalog = dependency_status["catalog"]
        qdrant = dependency_status["collection"]
        token = self.token_store.readiness() if self.token_store else {"status": "not_applicable"}
        return {
            "ok": True,
            "status": {
                "service_version": version("knowledgehub"),
                "mcp_sdk_version": version("mcp"),
                "protocol": "2025-11-25",
                "listener": self.config.listener,
                "collection": qdrant,
                "catalog": catalog,
                "token_store": token,
                "circuits": {
                    "qdrant": self.qdrant_breaker.state,
                    "embedding": self.embedding_breaker.state,
                    "reranker": self.reranker_breaker.state,
                },
                "limits": {
                    "request_timeout_seconds": self.config.request_timeout_seconds,
                    "max_response_bytes": self.config.max_response_bytes,
                    "max_concurrent_requests": self.config.max_concurrent_requests,
                    "requests_per_minute": self.config.requests_per_minute,
                },
                "reranker_profile": self.service.config.reranker_profile,
            },
        }

    def _bound(self, payload: dict[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        envelope_reserve = min(16_384, self.config.max_response_bytes)
        if len(encoded) <= self.config.max_response_bytes - envelope_reserve:
            return payload
        raise ToolError("response_too_large", "The response exceeds the configured final cap.")

    @staticmethod
    def _error(code: str, message: str, *, recoverable: bool = True) -> types.CallToolResult:
        payload = {
            "ok": False,
            "error": {"code": code, "message": message, "recoverable": recoverable},
        }
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"{code}: {message}")],
            structuredContent=payload,
            isError=True,
        )


def _mark_content(payload: dict[str, Any], max_chars: int, warnings: list[str]) -> dict[str, Any]:
    payload = {key: value for key, value in payload.items() if key in _PAYLOAD_FIELDS}
    text = payload.get("text")
    if isinstance(text, str):
        if _INJECTION.search(text):
            warnings.append("possible_prompt_injection_in_retrieved_content")
        if len(text) > max_chars:
            payload["text"] = text[:max_chars]
            payload["text_truncated"] = True
            warnings.append("retrieved_text_truncated")
    payload["content_origin"] = "retrieved_document"
    payload["trusted_as_instruction"] = False
    start, end = payload.get("page_start"), payload.get("page_end")
    payload["page_numbers"] = (
        list(range(int(start), int(end) + 1)) if start is not None and end is not None else []
    )
    return payload


def _fallback(name: str, payload: dict[str, Any]) -> str:
    if name == "rag_search":
        result = payload.get("result", {})
        return f"{len(result.get('hits', []))} hits; mode={result.get('mode')}; degraded={result.get('degraded')}"
    if name == "rag_resolve_reference":
        return f"resolution={payload.get('resolution')}; candidates={len(payload.get('candidates', []))}"
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:2000]


def _safe_code(exc: Exception) -> str:
    value = str(exc)
    return (
        value
        if value in {"catalog_unavailable", "embedding_unavailable", "circuit_open"}
        else "unavailable"
    )
