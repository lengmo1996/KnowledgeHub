from __future__ import annotations

import json
from pathlib import Path

from knowledgehub import __version__
from knowledgehub.cli.main import build_parser
from knowledgehub.core.hashing import sha256_file
from knowledgehub.governance.release import validate_release_manifest


def _manifest(root: Path) -> dict[str, object]:
    config = root / "config.yaml"
    config.write_text("value: 1\n", encoding="utf-8")
    return {
        "schema_name": "knowledgehub_release_manifest",
        "schema_version": "2.0",
        "release": "v2.0.0",
        "frozen_at": "2026-07-16T22:48:48+08:00",
        "implementation_commit": "a" * 40,
        "config_hashes": {"config.yaml": sha256_file(config)},
        "embedding": {"model": "embedding", "revision": "b" * 40, "dimension": 1024},
        "indexes": {
            name: {"collection": f"collection-{name}", "points": 1, "evidence": "test"}
            for name in ("literature", "code", "writing")
        },
        "tests": {
            "passed": 1,
            "failed": 0,
            "ruff": "passed",
            "mypy": "passed",
            "diff_check": "passed",
        },
        "runtime_integrity": {
            "valid": True,
            "checks": {"sources": 1, "normalized": 1, "writing": 1},
        },
        "interfaces": {"mcp_tools": 15, "evidence_schema": "query_result@2.0"},
        "source_pins": [{"library": "demo", "version": "1.0", "commit": "c" * 40}],
        "known_limits": ["No live service is required for offline validation."],
    }


def test_release_manifest_validates_without_runtime_services(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    result = validate_release_manifest(path, repository_root=tmp_path)
    assert result["valid"] is True
    assert result["checked_config_hashes"] == 1
    assert result["runtime_services_contacted"] is False


def test_release_manifest_rejects_hash_drift(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "config.yaml").write_text("value: 2\n", encoding="utf-8")
    result = validate_release_manifest(path, repository_root=tmp_path)
    assert result["valid"] is False
    assert "config hash mismatch: config.yaml" in result["errors"]


def test_release_manifest_rejects_repository_escape(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    manifest["config_hashes"] = {"../outside.yaml": "d" * 64}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    result = validate_release_manifest(path, repository_root=tmp_path)
    assert result["valid"] is False
    assert "config hash path escapes repository: ../outside.yaml" in result["errors"]


def test_release_cli_is_offline_and_has_safe_defaults() -> None:
    args = build_parser().parse_args(["release", "validate"])
    assert args.manifest == Path("state/releases/v2_manifest.json")
    assert args.repository_root == Path(".")


def test_package_version_marks_current_v2_patch() -> None:
    assert __version__ == "0.2.1"
