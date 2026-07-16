"""Offline validation for immutable KnowledgeHub release manifests."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from knowledgehub.core.hashing import sha256_file

RELEASE_SCHEMA_NAME = "knowledgehub_release_manifest"
RELEASE_SCHEMA_VERSION = "2.0"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_KNOWLEDGE_BASES = ("literature", "code", "writing")


def validate_release_manifest(manifest_path: Path, *, repository_root: Path) -> dict[str, Any]:
    """Validate structure and repository-bound hashes without network access.

    Runtime services are deliberately not contacted. Their last verified state
    belongs in the manifest as evidence and is reported separately from this
    deterministic release-file check.
    """

    errors: list[str] = []
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return _result(manifest_path, None, 0, [f"cannot read manifest: {error}"])
    if not isinstance(value, Mapping):
        return _result(manifest_path, None, 0, ["manifest must be a JSON object"])

    if value.get("schema_name") != RELEASE_SCHEMA_NAME:
        errors.append(f"schema_name must be {RELEASE_SCHEMA_NAME}")
    if value.get("schema_version") != RELEASE_SCHEMA_VERSION:
        errors.append(f"schema_version must be {RELEASE_SCHEMA_VERSION}")
    release = value.get("release")
    if not isinstance(release, str) or not re.fullmatch(r"v2(?:\.\d+){1,2}", release):
        errors.append("release must be a V2 semantic release such as v2.0.0")
    if not _GIT_COMMIT.fullmatch(str(value.get("implementation_commit") or "")):
        errors.append("implementation_commit must be a full lowercase Git commit")
    _validate_timestamp(value.get("frozen_at"), errors)

    checked_hashes = _validate_config_hashes(
        value.get("config_hashes"), repository_root.resolve(), errors
    )
    _validate_embedding(value.get("embedding"), errors)
    _validate_indexes(value.get("indexes"), errors)
    _validate_tests(value.get("tests"), errors)
    _validate_integrity(value.get("runtime_integrity"), errors)
    _validate_interfaces(value.get("interfaces"), errors)
    _validate_sources(value.get("source_pins"), errors)
    _validate_known_limits(value.get("known_limits"), errors)

    return _result(
        manifest_path,
        release if isinstance(release, str) else None,
        checked_hashes,
        errors,
    )


def _validate_timestamp(value: object, errors: list[str]) -> None:
    if not isinstance(value, str):
        errors.append("frozen_at must be an ISO-8601 timestamp")
        return
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        errors.append("frozen_at must be an ISO-8601 timestamp")
        return
    if parsed.tzinfo is None:
        errors.append("frozen_at must include a timezone")


def _validate_config_hashes(value: object, repository_root: Path, errors: list[str]) -> int:
    if not isinstance(value, Mapping) or not value:
        errors.append("config_hashes must be a non-empty object")
        return 0
    checked = 0
    for raw_path, raw_digest in value.items():
        if not isinstance(raw_path, str) or not isinstance(raw_digest, str):
            errors.append("config_hashes keys and values must be strings")
            continue
        relative = Path(raw_path)
        if relative.is_absolute() or ".." in relative.parts:
            errors.append(f"config hash path escapes repository: {raw_path}")
            continue
        target = (repository_root / relative).resolve()
        if not target.is_relative_to(repository_root):
            errors.append(f"config hash path escapes repository: {raw_path}")
            continue
        if not _SHA256.fullmatch(raw_digest):
            errors.append(f"invalid SHA-256 for {raw_path}")
            continue
        if not target.is_file():
            errors.append(f"missing hashed config: {raw_path}")
            continue
        checked += 1
        if sha256_file(target) != raw_digest:
            errors.append(f"config hash mismatch: {raw_path}")
    return checked


def _validate_embedding(value: object, errors: list[str]) -> None:
    if not isinstance(value, Mapping):
        errors.append("embedding must be an object")
        return
    if not value.get("model"):
        errors.append("embedding.model is required")
    if not _GIT_COMMIT.fullmatch(str(value.get("revision") or "")):
        errors.append("embedding.revision must be a full pinned commit")
    dimension = value.get("dimension")
    if not isinstance(dimension, int) or isinstance(dimension, bool) or dimension <= 0:
        errors.append("embedding.dimension must be a positive integer")


def _validate_indexes(value: object, errors: list[str]) -> None:
    if not isinstance(value, Mapping):
        errors.append("indexes must be an object")
        return
    for knowledge_base in _KNOWLEDGE_BASES:
        item = value.get(knowledge_base)
        if not isinstance(item, Mapping):
            errors.append(f"indexes.{knowledge_base} is required")
            continue
        if not item.get("collection"):
            errors.append(f"indexes.{knowledge_base}.collection is required")
        points = item.get("points")
        if not isinstance(points, int) or isinstance(points, bool) or points < 0:
            errors.append(f"indexes.{knowledge_base}.points must be a non-negative integer")
        if not item.get("evidence"):
            errors.append(f"indexes.{knowledge_base}.evidence is required")


def _validate_tests(value: object, errors: list[str]) -> None:
    if not isinstance(value, Mapping):
        errors.append("tests must be an object")
        return
    passed = value.get("passed")
    failed = value.get("failed")
    if not isinstance(passed, int) or isinstance(passed, bool) or passed <= 0:
        errors.append("tests.passed must be a positive integer")
    if failed != 0:
        errors.append("tests.failed must be zero for a frozen release")
    for gate in ("ruff", "mypy", "diff_check"):
        if value.get(gate) != "passed":
            errors.append(f"tests.{gate} must be passed")


def _validate_integrity(value: object, errors: list[str]) -> None:
    if not isinstance(value, Mapping) or value.get("valid") is not True:
        errors.append("runtime_integrity.valid must be true")
        return
    checks = value.get("checks")
    if not isinstance(checks, Mapping):
        errors.append("runtime_integrity.checks must be an object")
        return
    for name in ("sources", "normalized", "writing"):
        count = checks.get(name)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            errors.append(f"runtime_integrity.checks.{name} must be non-negative")


def _validate_interfaces(value: object, errors: list[str]) -> None:
    if not isinstance(value, Mapping):
        errors.append("interfaces must be an object")
        return
    mcp = value.get("mcp_tools")
    if not isinstance(mcp, int) or isinstance(mcp, bool) or mcp <= 0:
        errors.append("interfaces.mcp_tools must be positive")
    if value.get("evidence_schema") != "query_result@2.0":
        errors.append("interfaces.evidence_schema must be query_result@2.0")


def _validate_sources(value: object, errors: list[str]) -> None:
    if not isinstance(value, list) or not value:
        errors.append("source_pins must be a non-empty list")
        return
    identities: set[tuple[str, str]] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            errors.append(f"source_pins[{index}] must be an object")
            continue
        identity = (str(item.get("library") or ""), str(item.get("version") or ""))
        if not all(identity):
            errors.append(f"source_pins[{index}] requires library and version")
        elif identity in identities:
            errors.append(f"duplicate source pin: {identity[0]} {identity[1]}")
        identities.add(identity)
        if not _GIT_COMMIT.fullmatch(str(item.get("commit") or "")):
            errors.append(f"source_pins[{index}].commit must be a full commit")


def _validate_known_limits(value: object, errors: list[str]) -> None:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item.strip() for item in value)
    ):
        errors.append("known_limits must be a non-empty list of strings")


def _result(
    manifest_path: Path, release: str | None, checked_hashes: int, errors: list[str]
) -> dict[str, Any]:
    return {
        "valid": not errors,
        "manifest": str(manifest_path),
        "release": release,
        "checked_config_hashes": checked_hashes,
        "runtime_services_contacted": False,
        "errors": errors,
    }


__all__ = [
    "RELEASE_SCHEMA_NAME",
    "RELEASE_SCHEMA_VERSION",
    "validate_release_manifest",
]
