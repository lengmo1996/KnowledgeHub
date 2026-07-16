"""Explicit read-only MCP tool registry and execution facade."""

from __future__ import annotations

import dataclasses
import json
import os
import re
from importlib.metadata import version
from pathlib import Path
from typing import Any, Awaitable, Callable

import anyio
from pydantic import ValidationError

from knowledgehub.mcp.config import MCPConfig
from knowledgehub.mcp.resilience import CircuitBreaker
from knowledgehub.mcp.schemas import INPUT_MODELS, SearchFilters, SearchInput
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
    "knowledge_base",
    "library",
    "package",
    "version",
    "source_type",
    "repository",
    "path",
    "module",
    "symbol",
    "symbol_type",
    "section",
    "task_tags",
    "source_url",
    "tag",
    "commit",
    "evidence_role",
    "inference",
    "intent",
    "writing_function",
    "research_domain",
    "abstract_pattern",
    "rhetorical_structure",
    "usage_notes",
    "source_excerpt",
    "quality_score",
    "confidence",
    "return_mode",
    "original_text",
    "writing_id",
    "source_location",
    "source_collections",
    "venue",
    "paragraph_pattern",
    "moves",
    "transition_relations",
    "sentence_roles",
    "usage_context",
    "expression_strength",
    "tone",
    "paragraph_word_count",
    "contains_math",
    "feedback_adjustment",
    "adjusted_quality_score",
}

TOOL_DESCRIPTIONS = {
    "rag_search": "Search indexed document chunks with dense, sparse, or Qdrant RRF hybrid retrieval.",
    "rag_get_chunk": "Read one already-indexed chunk by its known chunk ID.",
    "rag_get_document": "Read document metadata and a paginated chunk index; full text is excluded.",
    "rag_get_neighbors": "Read bounded neighboring chunks around a known chunk ID.",
    "rag_resolve_reference": "Resolve exactly one DOI, citation key, Zotero key, or title to candidates.",
    "rag_list_facets": "List paginated collection, tag, year, or source values and counts.",
    "rag_status": "Return sanitized listener, dependency, limits, and build status.",
    "rag_compare_versions": "Retrieve version-labelled official evidence for a code compatibility comparison.",
    "writing_patterns": "Retrieve transferable academic writing patterns with provenance and usage guidance.",
    "writing_task": "Plan a supported academic writing task and retrieve structured evidence; final prose remains caller-owned.",
    "knowledge_inspect_symbol": "Inspect one version-pinned Python symbol and its static relations.",
    "knowledge_compare_symbols": "Compare one Python symbol across two indexed library versions.",
    "knowledge_analyze_repository": "Statically inspect an allowed local repository without executing or modifying it.",
    "knowledge_submit_feedback": "Record explicit quality feedback for one derived writing pattern.",
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
            "rag_compare_versions": self._compare_versions,
            "writing_patterns": self._writing_patterns,
            "writing_task": self._writing_task,
            "knowledge_inspect_symbol": self._inspect_symbol,
            "knowledge_compare_symbols": self._compare_symbols,
            "knowledge_analyze_repository": self._analyze_repository,
            "knowledge_submit_feedback": self._submit_feedback,
        }

    def definitions(self) -> list[types.Tool]:
        return [
            types.Tool(
                name=name,
                title=name.replace("rag_", "KnowledgeHub ").replace("_", " ").title(),
                description=TOOL_DESCRIPTIONS[name],
                inputSchema=model.model_json_schema(),
                annotations=types.ToolAnnotations(
                    readOnlyHint=name != "knowledge_submit_feedback",
                    destructiveHint=False,
                    idempotentHint=name != "knowledge_submit_feedback",
                    openWorldHint=False,
                ),
            )
            for name, model in INPUT_MODELS.items()
        ]

    async def call(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        model_type = INPUT_MODELS.get(name)
        handler = self._handlers.get(name)
        if model_type is None or handler is None:
            return self._error("unknown_tool", "Unknown tool.", recoverable=False)
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
        if value.knowledge_base != "literature":
            return await self._hub_search(value)
        reranker = value.reranker
        if reranker == "auto":
            reranker = self.service.config.reranker_profile
        use_reranker = reranker in {"light", "quality"}
        filters = value.filters
        request = SearchRequest(
            query=value.query,
            knowledge_base=value.knowledge_base,
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
            intent=value.intent,
            library=filters.library,
            package=filters.package,
            version=filters.version,
            installed_version=filters.installed_version,
            target_version=filters.target_version,
            source_types=filters.source_types,
            repository=filters.repository,
            path=filters.path,
            symbol=filters.symbol,
            section=filters.section,
            writing_function=filters.writing_function,
            research_domain=filters.research_domain,
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

    async def _hub_search(self, value: SearchInput) -> dict[str, Any]:
        if value.neighbors.before or value.neighbors.after:
            raise ToolError(
                "unsupported_expansion",
                "Neighbor expansion is currently available only for Literature results.",
            )
        from knowledgehub.hub.config import HubConfig
        from knowledgehub.hub.query import HubQueryRequest, HubQueryService

        filters = value.filters.model_dump(exclude_none=True)
        if value.knowledge_base != "literature" and filters.get("source") == "zotero":
            filters.pop("source", None)
        config = HubConfig.load(os.environ.get("KH_HUB_CONFIG", "configs/knowledgehub.yaml"))
        response = await anyio.to_thread.run_sync(
            lambda: HubQueryService(config).search(
                HubQueryRequest(
                    knowledge_base=value.knowledge_base,
                    query=value.query,
                    intent=value.intent,
                    filters=filters,
                    top_k=value.limit,
                    prefetch_limit=value.prefetch_limit,
                    mode=value.mode,
                    return_mode=value.return_mode,
                    reranker=value.reranker if value.reranker != "auto" else "off",
                )
            )
        )
        raw = dataclasses.asdict(response)
        warnings = list(raw["warnings"])
        for hit in raw["hits"]:
            hit["payload"] = _mark_content(dict(hit["payload"]), value.max_chars_per_hit, warnings)
        raw["warnings"] = sorted(set(warnings))
        return {"ok": True, "result": raw}

    async def _compare_versions(self, value: Any) -> dict[str, Any]:
        search = SearchInput(
            knowledge_base="code",
            query=value.query,
            intent="compatibility",
            limit=value.limit,
            filters=SearchFilters(
                **{
                    "library": value.library,
                    "installed_version": value.installed_version,
                    "target_version": value.target_version,
                    "source_types": (
                        "migration_guide",
                        "release_note",
                        "api_documentation",
                        "source_code",
                    ),
                    "source": None,
                }
            ),
        )
        return await self._hub_search(search)

    async def _writing_patterns(self, value: Any) -> dict[str, Any]:
        filters = {
            key: item
            for key, item in {
                "section": value.section,
                "writing_function": value.writing_function,
                "research_domain": value.research_domain,
                "venue": value.venue,
                "expression_strength": value.expression_strength,
                "tone": value.tone,
                "paragraph_words_min": value.paragraph_words_min,
                "paragraph_words_max": value.paragraph_words_max,
                "contains_math": value.contains_math,
                "source": None,
            }.items()
            if item is not None
        }
        search = SearchInput(
            knowledge_base="writing",
            query=value.query,
            limit=value.limit,
            return_mode=value.return_mode,
            filters=SearchFilters(**filters),
        )
        return await self._hub_search(search)

    async def _writing_task(self, value: Any) -> dict[str, Any]:
        from knowledgehub.hub.config import HubConfig
        from knowledgehub.writing_rag.v2 import WritingTaskPlanner, similarity_risk

        filters = {
            key: item
            for key, item in {
                "section": value.section,
                "writing_function": value.writing_function,
                "research_domain": value.research_domain,
                "venue": value.venue,
                "expression_strength": value.expression_strength,
                "tone": value.tone,
                "paragraph_words_min": value.paragraph_words_min,
                "paragraph_words_max": value.paragraph_words_max,
                "contains_math": value.contains_math,
            }.items()
            if item is not None
        }
        plan = WritingTaskPlanner().plan(
            value.task,
            objective=value.query,
            text=value.text,
            filters=filters,
        )
        retrieval = await self._writing_patterns(value)
        result = {"plan": plan, "retrieval": retrieval["result"]}
        if value.task == "audit_source_similarity":
            config = HubConfig.load(os.environ.get("KH_HUB_CONFIG", "configs/knowledgehub.yaml"))
            path = config.writing.data_root / "derived" / "writing_entries.jsonl"
            if not path.is_file():
                raise ToolError("not_found", "Writing entries were not found.")
            entries = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            sources = [
                {"source_id": item["writing_id"], "text": item["original_text"]}
                for item in entries
            ]
            result["similarity_audit"] = similarity_risk(value.text, sources)
        return {"ok": True, "result": result}

    async def _inspect_symbol(self, value: Any) -> dict[str, Any]:
        from knowledgehub.code_rag.symbols import SymbolIndex
        from knowledgehub.hub.config import HubConfig

        config = HubConfig.load(os.environ.get("KH_HUB_CONFIG", "configs/knowledgehub.yaml"))
        path = config.code.data_root / "state" / "symbols.sqlite3"
        result = SymbolIndex(path, read_only=True).inspect(
            value.library, value.version, value.symbol
        )
        if result is None:
            raise ToolError("not_found", "Symbol was not found.")
        return {"ok": True, "result": result}

    async def _compare_symbols(self, value: Any) -> dict[str, Any]:
        from knowledgehub.code_rag.symbols import SymbolIndex
        from knowledgehub.code_rag.version_diff import compare_symbols
        from knowledgehub.hub.config import HubConfig

        config = HubConfig.load(os.environ.get("KH_HUB_CONFIG", "configs/knowledgehub.yaml"))
        path = config.code.data_root / "state" / "symbols.sqlite3"

        def compare() -> dict[str, Any]:
            catalog = SymbolIndex(path, read_only=True)
            old = catalog.inspect(value.library, value.from_version, value.symbol)
            new = catalog.inspect(value.library, value.to_version, value.symbol)
            return compare_symbols(old, new)

        return {"ok": True, "result": compare()}

    async def _analyze_repository(self, value: Any) -> dict[str, Any]:
        from knowledgehub.hub.config import HubConfig
        from knowledgehub.workflows.repository import RepositoryIntake

        allowed = Path(os.environ.get("KH_REPOSITORY_ROOT", Path.cwd())).resolve(strict=True)
        requested = Path(value.repository)
        if requested.is_absolute():
            raise ToolError(
                "invalid_repository", "Repository must be relative to KH_REPOSITORY_ROOT."
            )
        repository = (allowed / requested).resolve(strict=True)
        try:
            repository.relative_to(allowed)
        except ValueError as exc:
            raise ToolError(
                "invalid_repository", "Repository is outside the allowed root."
            ) from exc
        if not repository.is_dir():
            raise ToolError("invalid_repository", "Repository must be a directory.")
        config = HubConfig.load(os.environ.get("KH_HUB_CONFIG", "configs/knowledgehub.yaml"))
        environment_path = (
            config.code.data_root / "state" / "environments" / f"{value.environment}.json"
        )
        if not environment_path.is_file():
            raise ToolError("not_found", "Environment profile was not found.")
        environment = json.loads(environment_path.read_text(encoding="utf-8"))
        result = RepositoryIntake(repository).inspect(environment)
        profile = result["profile"]
        api_usage = profile["api_usage"]
        profile["api_usage"] = [
            {
                **item,
                "imports": item["imports"][:50],
                "symbols": item["symbols"][:50],
                "files": item["files"][:50],
                "call_sites": item["call_sites"][:100],
            }
            for item in api_usage[:100]
        ]
        profile["api_usage_truncated"] = len(api_usage) > 100
        return {"ok": True, "result": result}

    async def _submit_feedback(self, value: Any) -> dict[str, Any]:
        from knowledgehub.hub.config import HubConfig
        from knowledgehub.writing_rag.v2 import WritingFeedbackStore

        config = HubConfig.load(os.environ.get("KH_HUB_CONFIG", "configs/knowledgehub.yaml"))
        result = WritingFeedbackStore(
            config.writing.data_root / "state" / "feedback.sqlite3"
        ).submit(
            value.writing_id,
            value.label,
            value.context.model_dump(exclude_none=True),
        )
        return {"ok": True, "result": result}

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
    for field in ("text", "original_text", "source_excerpt"):
        text = payload.get(field)
        if not isinstance(text, str):
            continue
        if _INJECTION.search(text):
            warnings.append("possible_prompt_injection_in_retrieved_content")
        if len(text) > max_chars:
            payload[field] = text[:max_chars]
            payload[f"{field}_truncated"] = True
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
