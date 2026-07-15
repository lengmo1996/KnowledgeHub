"""STDIO and Streamable HTTP transports for the read-only MCP server."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.stdio import stdio_server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from knowledgehub.mcp.catalog import ReadOnlyCatalog
from knowledgehub.mcp.config import MCPConfig
from knowledgehub.mcp.resilience import SlidingWindowLimiter
from knowledgehub.mcp.server import create_server
from knowledgehub.mcp.tokens import TokenStore, request_context
from knowledgehub.mcp.tools import ToolRegistry
from knowledgehub.pipeline.config import RagConfig
from knowledgehub.services.search_api import build_retrieval


def build_service(rag_config: RagConfig, mcp_config: MCPConfig) -> Any:
    service = build_retrieval(rag_config)
    service.catalog = ReadOnlyCatalog(
        rag_config.source_snapshot_path,
        rag_config.data_dir / "state" / "pipeline.sqlite3",
        mcp_config.zotero_state_path,
    )
    return service


class RequestPolicyMiddleware:
    def __init__(self, app: ASGIApp, *, trusted_proxy: bool) -> None:
        self.app = app
        self.trusted_proxy = trusted_proxy

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        peer = str((scope.get("client") or ("127.0.0.1", 0))[0])
        remote = peer
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        if self.trusted_proxy and ipaddress.ip_address(peer).is_loopback:
            forwarded = headers.get(b"x-forwarded-for", b"").decode("latin-1")
            if forwarded:
                candidate = forwarded.rsplit(",", 1)[-1].strip()
                try:
                    remote = str(ipaddress.ip_address(candidate))
                except ValueError:
                    remote = peer
            identity = headers.get(b"tailscale-user-login", b"").decode("latin-1", errors="replace")
            if identity:
                scope["kh_tailscale_identity"] = identity[:256]
        scope["kh_remote_ip"] = remote
        path = str(scope.get("root_path", "")) + str(scope.get("path", ""))
        token = request_context.set((remote, path.rstrip("/") or "/mcp"))
        try:
            await self.app(scope, receive, send)
        finally:
            request_context.reset(token)


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp, limiter: SlidingWindowLimiter) -> None:
        self.app = app
        self.limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            user = scope.get("user")
            peer = str(scope.get("kh_remote_ip") or (scope.get("client") or ("unknown", 0))[0])
            principal = str(getattr(user, "display_name", "") or "anonymous")
            allowed_principal = await self.limiter.allow(f"principal:{principal}")
            allowed_ip = await self.limiter.allow(f"ip:{peer}")
            if not (allowed_principal and allowed_ip):
                response = JSONResponse(
                    {"error": "rate_limited", "retryable": True}, status_code=429
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class AuditMiddleware:
    def __init__(self, app: ASGIApp, *, listener: str) -> None:
        self.app = app
        self.listener = listener
        self.logger = _audit_logger(listener)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        started = time.monotonic()
        status = 500

        async def audit_send(message: Any) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, audit_send)
        finally:
            if scope["type"] == "http":
                user = scope.get("user")
                record = {
                    "event": "mcp_http_request",
                    "listener": self.listener,
                    "method": str(scope.get("method", ""))[:16],
                    "path": str(scope.get("root_path", ""))[:128],
                    "principal": str(getattr(user, "display_name", "anonymous"))[:128],
                    "remote_ip": str(scope.get("kh_remote_ip", "unknown"))[:64],
                    "tailscale_identity": str(scope.get("kh_tailscale_identity", ""))[:256],
                    "status": status,
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                }
                self.logger.info(json.dumps(record, separators=(",", ":")))


def create_http_app(
    rag_config: RagConfig,
    mcp_config: MCPConfig,
    *,
    service: Any | None = None,
    token_store: TokenStore | None = None,
) -> Starlette:
    retrieval = service or build_service(rag_config, mcp_config)
    tokens = token_store or TokenStore(mcp_config.token_file)
    registry = ToolRegistry(retrieval, mcp_config, token_store=tokens)
    server = create_server(registry)
    manager = StreamableHTTPSessionManager(
        server,
        json_response=True,
        stateless=False,
        session_idle_timeout=mcp_config.session_idle_seconds,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(mcp_config.allowed_hosts),
            allowed_origins=list(mcp_config.allowed_origins),
        ),
    )
    protected: ASGIApp = RequireAuthMiddleware(manager.handle_request, required_scopes=["mcp:read"])
    protected = RateLimitMiddleware(protected, SlidingWindowLimiter(mcp_config.requests_per_minute))
    protected = AuthenticationMiddleware(protected, backend=BearerAuthBackend(tokens))
    protected = RequestPolicyMiddleware(protected, trusted_proxy=mcp_config.trusted_proxy)
    protected = AuditMiddleware(protected, listener=mcp_config.listener)

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield
        await retrieval.aclose()

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "listener": mcp_config.listener})

    async def ready(_: Request) -> JSONResponse:
        components: dict[str, Any] = {"token_store": tokens.readiness()}
        try:
            dependency_status = await retrieval.areadiness()
            components["catalog"] = dependency_status["catalog"]
            components["qdrant"] = dependency_status["collection"]
            components["embedding"] = dependency_status["embedding"]
            components["sparse"] = dependency_status["sparse"]
            components["reranker"] = dependency_status["reranker"]
        except Exception as exc:
            components["retrieval"] = {"status": "not_ready", "error": type(exc).__name__}
        statuses = [value.get("status") for value in components.values() if isinstance(value, dict)]
        status = (
            "not_ready"
            if "not_ready" in statuses
            else "degraded"
            if "degraded" in statuses
            else "ready"
        )
        return JSONResponse(
            {"status": status, "components": components},
            status_code=503 if status == "not_ready" else 200,
        )

    return Starlette(
        routes=[
            Route("/healthz", health, methods=["GET"]),
            Route("/readyz", ready, methods=["GET"]),
            # Handle the documented endpoint without relying on Starlette's
            # slash redirect.  Behind Tailscale Serve the ASGI scheme is HTTP,
            # so an automatic /mcp -> /mcp/ redirect would incorrectly emit an
            # insecure http:// public Location and strict clients reject it.
            Route("/mcp", endpoint=protected, methods=["GET", "POST", "DELETE"]),
            Mount("/mcp", app=protected),
        ],
        lifespan=lifespan,
    )


async def run_stdio(rag_config: RagConfig, mcp_config: MCPConfig) -> None:
    service = build_service(rag_config, mcp_config)
    registry = ToolRegistry(service, mcp_config)
    server = create_server(registry)
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
                raise_exceptions=False,
            )
    finally:
        await service.aclose()


def _audit_logger(listener: str) -> logging.Logger:
    logger = logging.getLogger(f"knowledgehub.mcp.audit.{listener}")
    if logger.handlers:
        return logger
    path = Path(os.environ.get("KH_MCP_AUDIT_LOG", f"/tmp/knowledgehub-mcp-{listener}-audit.log"))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.FileHandler(path, encoding="utf-8")
    except OSError:
        handler = logging.NullHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger
