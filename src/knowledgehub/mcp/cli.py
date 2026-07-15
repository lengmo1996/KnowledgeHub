"""KnowledgeHub MCP operator and token-administration CLI."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import secrets
from pathlib import Path
from typing import Any

import uvicorn

from knowledgehub.mcp.config import MCPConfig
from knowledgehub.mcp.runtime import build_service, create_http_app, run_stdio
from knowledgehub.mcp.schemas import INPUT_MODELS
from knowledgehub.mcp.tokens import add_token, list_tokens, rotate_token, set_disabled
from knowledgehub.mcp.tools import ToolRegistry
from knowledgehub.pipeline.config import RagConfig
from knowledgehub.retrieval.models import SearchRequest


def add_mcp_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser("mcp", help="Read-only MCP service")
    parser.add_argument("--rag-config", type=Path, default=Path("configs/rag/default.yaml"))
    commands = parser.add_subparsers(dest="mcp_command", required=True)
    commands.add_parser("doctor")
    serve = commands.add_parser("serve")
    serve.add_argument("--transport", choices=("http", "stdio"), default="http")
    commands.add_parser("tools")
    commands.add_parser("validate")
    commands.add_parser("status")
    test_search = commands.add_parser("test-search")
    test_search.add_argument("query")
    test_search.add_argument("--mode", choices=("dense", "sparse", "hybrid"), default="hybrid")
    commands.add_parser("print-codex-config")
    token = commands.add_parser("token")
    token_commands = token.add_subparsers(dest="token_command", required=True)
    add = token_commands.add_parser("add")
    add.add_argument("--label", required=True)
    add.add_argument("--cidr", action="append", default=[])
    add.add_argument("--expires-at")
    rotate = token_commands.add_parser("rotate")
    rotate.add_argument("token_id")
    revoke = token_commands.add_parser("revoke")
    revoke.add_argument("token_id")
    token_commands.add_parser("list")


def run_mcp_command(args: argparse.Namespace) -> int:
    mcp = MCPConfig.load()
    command = args.mcp_command
    if command == "token":
        return _token_command(args, mcp)
    rag = RagConfig.load(args.rag_config)
    if command == "serve":
        if args.transport == "stdio":
            asyncio.run(run_stdio(rag, dataclasses.replace(mcp, listener="stdio")))
        else:
            if mcp.listener == "stdio":
                raise SystemExit("set KH_MCP_LISTENER=lan or tailscale for HTTP")
            uvicorn.run(
                create_http_app(rag, mcp),
                host=mcp.host,
                port=mcp.port,
                access_log=False,
                proxy_headers=False,
                server_header=False,
            )
        return 0
    if command == "tools":
        service = build_service(rag, mcp)
        try:
            for tool in ToolRegistry(service, mcp).definitions():
                print(f"{tool.name}\t{tool.description}")
        finally:
            service.endpoint_pool.close()
        return 0
    if command == "validate":
        schemas = {name: model.model_json_schema() for name, model in INPUT_MODELS.items()}
        _assert_strict(schemas)
        print(json.dumps({"status": "ok", "tools": len(schemas), "listener": mcp.listener}))
        return 0
    if command == "print-codex-config":
        print(_codex_config())
        return 0
    service = build_service(rag, mcp)
    try:
        if command == "doctor":
            result = {
                "status": "ok",
                "qdrant": service.index.status(),
                "catalog": service.catalog.status(),
                "embedding": service.endpoint_pool.health(),
                "reranker_profile": rag.reranker_profile,
            }
        elif command == "status":
            result = asyncio.run(
                _structured(ToolRegistry(service, mcp), "rag_status", {"verbose": True})
            )
        elif command == "test-search":
            result = dataclasses.asdict(
                service.search(SearchRequest(query=args.query, mode=args.mode, limit=3))
            )
        else:
            raise AssertionError(command)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    finally:
        service.endpoint_pool.close()
        if service.reranker:
            service.reranker.close()
    return 0


async def _structured(registry: ToolRegistry, name: str, arguments: dict[str, Any]) -> Any:
    return (await registry.call(name, arguments)).structuredContent


def _token_command(args: argparse.Namespace, config: MCPConfig) -> int:
    if args.token_command == "add":
        token_id, secret = add_token(
            config.token_file,
            label=args.label,
            cidrs=args.cidr,
            expires_at=args.expires_at,
        )
        print(json.dumps({"id": token_id, "token": secret, "warning": "shown_once"}))
    elif args.token_command == "rotate":
        secret = rotate_token(config.token_file, args.token_id)
        print(json.dumps({"id": args.token_id, "token": secret, "warning": "shown_once"}))
    elif args.token_command == "revoke":
        set_disabled(config.token_file, args.token_id, disabled=True)
        print(json.dumps({"id": args.token_id, "disabled": True}))
    else:
        print(json.dumps(list_tokens(config.token_file), ensure_ascii=False, indent=2))
    return 0


def _assert_strict(value: Any) -> None:
    if isinstance(value, dict):
        if value.get("type") == "object" and value.get("additionalProperties") is not False:
            raise ValueError("object schema is not strict")
        for child in value.values():
            _assert_strict(child)
    elif isinstance(value, list):
        for child in value:
            _assert_strict(child)


def _codex_config() -> str:
    placeholder = "KH_MCP_BEARER_TOKEN"
    nonce = secrets.token_hex(4)
    return f'''# LAN client: ~/.codex/config.toml
[mcp_servers.knowledgehub_lan]
url = "http://10.249.44.27:8091/mcp"
bearer_token_env_var = "{placeholder}"
enabled_tools = ["rag_search", "rag_get_chunk", "rag_get_document", "rag_get_neighbors", "rag_resolve_reference", "rag_list_facets", "rag_status"]
required = true
startup_timeout_sec = 20
tool_timeout_sec = 120

# Tailscale client: ~/.codex/config.toml
[mcp_servers.knowledgehub_tailnet]
url = "https://server-ai-00.tail02a76b.ts.net/mcp"
bearer_token_env_var = "{placeholder}"
enabled_tools = ["rag_search", "rag_get_chunk", "rag_get_document", "rag_get_neighbors", "rag_resolve_reference", "rag_list_facets", "rag_status"]
required = true
startup_timeout_sec = 20
tool_timeout_sec = 120

# Equivalent commands ({nonce} is output-only, not a credential):
# codex mcp add knowledgehub_lan --url http://10.249.44.27:8091/mcp --bearer-token-env-var {placeholder}
# codex mcp add knowledgehub_tailnet --url https://server-ai-00.tail02a76b.ts.net/mcp --bearer-token-env-var {placeholder}
'''
