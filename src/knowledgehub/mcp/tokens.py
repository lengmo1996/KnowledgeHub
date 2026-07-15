"""Opaque bearer-token administration and SDK token verification."""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, cast

from mcp.server.auth.provider import AccessToken, TokenVerifier

request_context: contextvars.ContextVar[tuple[str, str]] = contextvars.ContextVar(
    "kh_mcp_request_context", default=("127.0.0.1", "/mcp")
)


def _hmac_key(environ: Mapping[str, str] | None = None) -> bytes:
    env = os.environ if environ is None else environ
    value = env.get("KH_MCP_TOKEN_HMAC_KEY", "")
    if len(value) < 32:
        raise ValueError("KH_MCP_TOKEN_HMAC_KEY must contain at least 32 characters")
    return value.encode()


def token_digest(token: str, key: bytes) -> str:
    return hmac.new(key, token.encode(), hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class TokenPrincipal:
    token_id: str
    label: str
    expires_at: int | None
    paths: tuple[str, ...]
    cidrs: tuple[str, ...]


class TokenStore(TokenVerifier):
    """Reloadable token store that retains the last valid snapshot on errors."""

    def __init__(
        self,
        path: Path,
        *,
        key: bytes | None = None,
        legacy_token: str | None = None,
    ) -> None:
        self.path = path
        self.key = key or _hmac_key()
        self._stat_signature = (-1, -1)
        self._records: dict[str, dict[str, Any]] = {}
        self.last_error: str | None = None
        compatibility = (
            legacy_token if legacy_token is not None else os.environ.get("KH_MCP_BEARER_TOKEN", "")
        )
        self._legacy_only = bool(compatibility)
        if compatibility:
            self._records[token_digest(compatibility, self.key)] = {
                "id": "legacy-compatibility-token",
                "label": "legacy-compatibility-token",
                "token_hash": token_digest(compatibility, self.key),
                "disabled": False,
                "paths": ["/mcp"],
                "cidrs": [],
            }

    def _reload(self) -> None:
        if self._legacy_only:
            return
        try:
            stat = self.path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            if signature == self._stat_signature:
                return
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("version") != 1 or not isinstance(payload.get("tokens"), list):
                raise ValueError("unsupported token store format")
            records: dict[str, dict[str, Any]] = {}
            for record in payload["tokens"]:
                if not isinstance(record, dict):
                    raise ValueError("invalid token record")
                digest = str(record.get("token_hash") or "")
                if len(digest) != 64:
                    raise ValueError("invalid token hash")
                bytes.fromhex(digest)
                if not record.get("id") or not isinstance(record.get("disabled", False), bool):
                    raise ValueError("invalid token identity")
                paths = record.get("paths", ["/mcp"])
                cidrs = record.get("cidrs", [])
                if (
                    not isinstance(paths, list)
                    or not paths
                    or any(
                        not isinstance(value, str) or not value.startswith("/") for value in paths
                    )
                ):
                    raise ValueError("invalid token paths")
                if not isinstance(cidrs, list):
                    raise ValueError("invalid token CIDRs")
                for value in cidrs:
                    ipaddress.ip_network(str(value))
                _timestamp(record.get("expires_at"))
                if digest in records:
                    raise ValueError("duplicate token hash")
                records[digest] = record
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.last_error = type(exc).__name__
            if not self._records:
                raise
            return
        self._records = records
        self._stat_signature = signature
        self.last_error = None

    def verify(self, token: str, *, remote_ip: str, path: str) -> TokenPrincipal | None:
        try:
            self._reload()
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        digest = token_digest(token, self.key)
        matched: dict[str, Any] | None = None
        for candidate, record in self._records.items():
            if hmac.compare_digest(candidate, digest):
                matched = record
        if matched is None or bool(matched.get("disabled")):
            return None
        expires_at = _timestamp(matched.get("expires_at"))
        if expires_at is not None and expires_at <= int(time.time()):
            return None
        paths = tuple(str(value) for value in matched.get("paths", ["/mcp"]))
        cidrs = tuple(str(value) for value in matched.get("cidrs", []))
        if not any(path == prefix or path.startswith(prefix.rstrip("/") + "/") for prefix in paths):
            return None
        if cidrs:
            try:
                allowed = any(
                    ipaddress.ip_address(remote_ip) in ipaddress.ip_network(value)
                    for value in cidrs
                )
            except ValueError:
                return None
            if not allowed:
                return None
        return TokenPrincipal(
            token_id=str(matched["id"]),
            label=str(matched.get("label") or matched["id"]),
            expires_at=expires_at,
            paths=paths,
            cidrs=cidrs,
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        remote_ip, path = request_context.get()
        principal = self.verify(token, remote_ip=remote_ip, path=path)
        if principal is None:
            return None
        return AccessToken(
            token="redacted",
            client_id=principal.token_id,
            subject=principal.label,
            scopes=["mcp:read"],
            expires_at=principal.expires_at,
            claims={"iss": "knowledgehub-local-token-store"},
        )

    def readiness(self) -> dict[str, Any]:
        try:
            self._reload()
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        return {
            "status": (
                "not_ready" if not self._records else "degraded" if self.last_error else "ready"
            ),
            "loaded_tokens": len(self._records),
            "reload_error": self.last_error,
            "mode": "compatibility" if self._legacy_only else "per_device_file",
        }


def initialize_store(path: Path) -> None:
    if not path.exists():
        _write(path, {"version": 1, "tokens": []})


def add_token(
    path: Path,
    *,
    label: str,
    cidrs: list[str],
    paths: list[str] | None = None,
    expires_at: str | None = None,
    key: bytes | None = None,
) -> tuple[str, str]:
    if not label.strip() or len(label) > 128:
        raise ValueError("token label must contain 1 to 128 characters")
    for value in cidrs:
        ipaddress.ip_network(value)
    if expires_at is not None:
        _timestamp(expires_at)
    secret = "khmcp_" + secrets.token_urlsafe(32)
    token_id = secrets.token_hex(8)
    payload = _read_admin(path)
    payload["tokens"].append(
        {
            "id": token_id,
            "label": label,
            "token_hash": token_digest(secret, key or _hmac_key()),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": expires_at,
            "disabled": False,
            "paths": paths or ["/mcp"],
            "cidrs": cidrs,
        }
    )
    _write(path, payload)
    return token_id, secret


def set_disabled(path: Path, token_id: str, *, disabled: bool) -> None:
    payload = _read_admin(path)
    for record in payload["tokens"]:
        if record.get("id") == token_id:
            record["disabled"] = disabled
            _write(path, payload)
            return
    raise KeyError(token_id)


def rotate_token(path: Path, token_id: str, *, key: bytes | None = None) -> str:
    payload = _read_admin(path)
    secret = "khmcp_" + secrets.token_urlsafe(32)
    for record in payload["tokens"]:
        if record.get("id") == token_id:
            record["token_hash"] = token_digest(secret, key or _hmac_key())
            record["rotated_at"] = datetime.now(timezone.utc).isoformat()
            record["disabled"] = False
            _write(path, payload)
            return secret
    raise KeyError(token_id)


def list_tokens(path: Path) -> list[dict[str, Any]]:
    payload = _read_admin(path)
    return [
        {key: value for key, value in record.items() if key != "token_hash"}
        for record in payload["tokens"]
    ]


def _read_admin(path: Path) -> dict[str, Any]:
    initialize_store(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != 1 or not isinstance(payload.get("tokens"), list):
        raise ValueError("invalid token store")
    return cast("dict[str, Any]", payload)


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        existing = path.stat()
        mode = existing.st_mode & 0o777
        owner = (existing.st_uid, existing.st_gid)
    except FileNotFoundError:
        mode = 0o600
        owner = None
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        if owner is not None:
            os.chown(temporary, *owner)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _timestamp(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    if isinstance(value, int):
        return value
    return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
