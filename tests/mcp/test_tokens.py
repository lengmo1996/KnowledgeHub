from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from knowledgehub.mcp.tokens import TokenStore, add_token, list_tokens, rotate_token, set_disabled

KEY = b"x" * 32


def test_token_lifecycle_and_no_plaintext_at_rest(tmp_path) -> None:
    path = tmp_path / "tokens.json"
    token_id, token = add_token(path, label="laptop", cidrs=["10.0.0.0/8"], key=KEY)
    assert token not in path.read_text()
    store = TokenStore(path, key=KEY)
    assert store.verify(token, remote_ip="10.1.2.3", path="/mcp") is not None
    assert store.verify(token, remote_ip="192.0.2.1", path="/mcp") is None
    assert "token_hash" not in list_tokens(path)[0]

    replacement = rotate_token(path, token_id, key=KEY)
    assert store.verify(token, remote_ip="10.1.2.3", path="/mcp") is None
    assert store.verify(replacement, remote_ip="10.1.2.3", path="/mcp") is not None
    set_disabled(path, token_id, disabled=True)
    assert store.verify(replacement, remote_ip="10.1.2.3", path="/mcp") is None


def test_expired_and_reload_failure_retains_last_good_snapshot(tmp_path) -> None:
    path = tmp_path / "tokens.json"
    _, token = add_token(path, label="expired", cidrs=[], key=KEY)
    payload = json.loads(path.read_text())
    payload["tokens"][0]["expires_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=5)
    ).isoformat()
    path.write_text(json.dumps(payload))
    store = TokenStore(path, key=KEY)
    assert store.verify(token, remote_ip="127.0.0.1", path="/mcp") is None

    payload["tokens"][0]["expires_at"] = None
    path.write_text(json.dumps(payload))
    assert store.verify(token, remote_ip="127.0.0.1", path="/mcp") is not None
    path.write_text("not-json")
    assert store.verify(token, remote_ip="127.0.0.1", path="/mcp") is not None
    assert store.readiness()["status"] == "degraded"


def test_single_token_compatibility_mode_does_not_require_file(tmp_path) -> None:
    store = TokenStore(tmp_path / "missing.json", key=KEY, legacy_token="compat-secret")
    principal = store.verify("compat-secret", remote_ip="192.0.2.1", path="/mcp")
    assert principal is not None
    assert store.readiness()["mode"] == "compatibility"


def test_rotation_preserves_token_file_mode(tmp_path) -> None:
    path = tmp_path / "tokens.json"
    token_id, _ = add_token(path, label="mode", cidrs=[], key=KEY)
    path.chmod(0o640)
    rotate_token(path, token_id, key=KEY)
    assert path.stat().st_mode & 0o777 == 0o640
