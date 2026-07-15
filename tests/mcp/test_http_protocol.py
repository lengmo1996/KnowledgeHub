from __future__ import annotations

import json
from types import SimpleNamespace

import anyio
import httpx

from knowledgehub.mcp.config import MCPConfig
from knowledgehub.mcp.runtime import create_http_app
from knowledgehub.mcp.tokens import TokenStore, add_token
from knowledgehub.pipeline.config import RagConfig

KEY = b"h" * 32


class FakeHTTPService:
    def __init__(self) -> None:
        self.config = SimpleNamespace(reranker_profile="off")
        self.catalog = SimpleNamespace(status=lambda: {"documents": 1, "active_chunks": 1})
        self.index = SimpleNamespace(
            status=lambda: {"collection": "test", "points": 1, "status": "green"}
        )
        self.endpoint_pool = SimpleNamespace(close=lambda: None)
        self.reranker = None

    async def aclose(self) -> None:
        return None

    async def areadiness(self):  # type: ignore[no-untyped-def]
        return {
            "catalog": self.catalog.status(),
            "collection": self.index.status(),
            "embedding": {"status": "ready"},
            "sparse": {"status": "ready"},
            "reranker": {"status": "not_required"},
        }


def test_streamable_http_initialize_auth_origin_and_session_binding(tmp_path) -> None:
    token_path = tmp_path / "tokens.json"
    _, first = add_token(token_path, label="first", cidrs=[], key=KEY)
    _, second = add_token(token_path, label="second", cidrs=[], key=KEY)
    config = MCPConfig(
        listener="tailscale",
        host="127.0.0.1",
        port=8092,
        allowed_hosts=("testserver",),
        allowed_origins=("https://client.example",),
    )
    app = create_http_app(
        RagConfig(data_dir=tmp_path),
        config,
        service=FakeHTTPService(),
        token_store=TokenStore(token_path, key=KEY),
    )
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1"},
        },
    }
    headers = {
        "authorization": f"Bearer {first}",
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                health = await client.get("/healthz")
                assert health.json() == {"status": "ok", "listener": "tailscale"}
                assert (await client.post("/mcp/", json=initialize)).status_code == 401
                assert (
                    await client.post(
                        "/mcp/",
                        json=initialize,
                        headers={**headers, "authorization": "Bearer wrong"},
                    )
                ).status_code == 401
                assert (
                    await client.post(
                        "/mcp/", json=initialize, headers={**headers, "host": "evil.example"}
                    )
                ).status_code == 421
                rejected = await client.post(
                    "/mcp/",
                    json=initialize,
                    headers={**headers, "origin": "https://evil.example"},
                )
                assert rejected.status_code == 403
                response = await client.post(
                    "/mcp", content=json.dumps(initialize), headers=headers
                )
                assert response.status_code == 200
                assert response.history == []
                assert response.json()["result"]["protocolVersion"] == "2025-11-25"
                session_id = response.headers["mcp-session-id"]
                wrong = await client.get(
                    "/mcp/",
                    headers={
                        "authorization": f"Bearer {second}",
                        "accept": "text/event-stream",
                        "mcp-session-id": session_id,
                    },
                )
                assert wrong.status_code in {403, 404}

    anyio.run(exercise)
