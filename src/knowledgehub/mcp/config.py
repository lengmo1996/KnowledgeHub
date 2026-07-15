"""Environment-only MCP runtime configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


def _csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _integer(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        value = int(env.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


@dataclass(frozen=True, slots=True)
class MCPConfig:
    listener: str = "stdio"
    host: str = "127.0.0.1"
    port: int = 8092
    public_url: str = "http://127.0.0.1:8092/mcp"
    token_file: Path = Path("/etc/knowledgehub/mcp-tokens.json")
    zotero_state_path: Path = Path("/data/KnowledgeHub/zotero/state/zotero.sqlite3")
    allowed_hosts: tuple[str, ...] = ("127.0.0.1:8092", "localhost:8092")
    allowed_origins: tuple[str, ...] = ()
    trusted_proxy: bool = False
    request_timeout_seconds: int = 120
    session_idle_seconds: int = 900
    max_response_bytes: int = 1_048_576
    max_concurrent_requests: int = 8
    max_concurrent_embeddings: int = 2
    max_concurrent_rerankers: int = 1
    requests_per_minute: int = 60
    max_text_chars: int = 120_000

    @classmethod
    def load(cls, environ: Mapping[str, str] | None = None) -> "MCPConfig":
        env = os.environ if environ is None else environ
        listener = env.get("KH_MCP_LISTENER", "stdio").strip().lower()
        if listener not in {"stdio", "lan", "tailscale"}:
            raise ValueError("KH_MCP_LISTENER must be stdio, lan, or tailscale")
        host = env.get("KH_MCP_HOST", "10.249.44.27" if listener == "lan" else "127.0.0.1")
        port = _integer(env, "KH_MCP_PORT", 8091 if listener == "lan" else 8092)
        default_hosts = (
            f"{host}:{port},server-ai-00.tail02a76b.ts.net"
            if listener == "tailscale"
            else f"{host}:{port}"
        )
        public_url = env.get(
            "KH_MCP_PUBLIC_URL",
            "https://server-ai-00.tail02a76b.ts.net/mcp"
            if listener == "tailscale"
            else f"http://{host}:{port}/mcp",
        )
        return cls(
            listener=listener,
            host=host,
            port=port,
            public_url=public_url,
            token_file=Path(env.get("KH_MCP_TOKEN_FILE", "/etc/knowledgehub/mcp-tokens.json")),
            zotero_state_path=Path(
                env.get(
                    "KH_MCP_ZOTERO_STATE_PATH",
                    "/data/KnowledgeHub/zotero/state/zotero.sqlite3",
                )
            ),
            allowed_hosts=_csv(env.get("KH_MCP_ALLOWED_HOSTS", default_hosts)),
            allowed_origins=_csv(env.get("KH_MCP_ALLOWED_ORIGINS", "")),
            trusted_proxy=listener == "tailscale",
            request_timeout_seconds=_integer(env, "KH_MCP_REQUEST_TIMEOUT_SECONDS", 120),
            session_idle_seconds=_integer(env, "KH_MCP_SESSION_IDLE_SECONDS", 900),
            max_response_bytes=_integer(env, "KH_MCP_MAX_RESPONSE_BYTES", 1_048_576),
            max_concurrent_requests=_integer(env, "KH_MCP_MAX_CONCURRENT_REQUESTS", 8),
            max_concurrent_embeddings=_integer(env, "KH_MCP_MAX_CONCURRENT_EMBEDDINGS", 2),
            max_concurrent_rerankers=_integer(env, "KH_MCP_MAX_CONCURRENT_RERANKERS", 1),
            requests_per_minute=_integer(env, "KH_MCP_REQUESTS_PER_MINUTE", 60),
            max_text_chars=_integer(env, "KH_MCP_MAX_TEXT_CHARS", 120_000),
        )
