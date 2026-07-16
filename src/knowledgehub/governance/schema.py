"""Strict V2 schema envelopes and explicit V1-to-V2 migrations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

SCHEMAS: dict[str, str] = {
    "source_document": "2.0",
    "normalized_document": "2.0",
    "code_chunk": "2.0",
    "writing_entry": "2.0",
    "environment_profile": "2.0",
    "task_manifest": "2.0",
    "query_result": "2.0",
}


@dataclass(frozen=True, slots=True)
class SchemaEnvelope:
    schema_name: str
    schema_version: str
    data: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "data": dict(self.data),
        }


class SchemaRegistry:
    def __init__(self) -> None:
        self.versions = dict(SCHEMAS)
        self._migrations: dict[tuple[str, str, str], Callable[[Mapping[str, Any]], Mapping[str, Any]]] = {}
        for name in self.versions:
            self._migrations[(name, "1.0", "2.0")] = lambda value: dict(value)

    def validate(self, value: Mapping[str, Any], *, expected: str | None = None) -> SchemaEnvelope:
        name = str(value.get("schema_name") or "")
        version = str(value.get("schema_version") or "")
        data = value.get("data")
        if expected and name != expected:
            raise ValueError(f"expected schema {expected}, got {name or 'missing'}")
        if name not in self.versions:
            raise ValueError(f"unknown schema: {name or 'missing'}")
        if version != self.versions[name]:
            raise ValueError(f"incompatible {name} schema version: {version or 'missing'}")
        if not isinstance(data, Mapping):
            raise ValueError("schema envelope data must be an object")
        self._validate_required(name, data)
        return SchemaEnvelope(name, version, data)

    def migrate(
        self, name: str, value: Mapping[str, Any], *, from_version: str = "1.0"
    ) -> SchemaEnvelope:
        target = self.versions.get(name)
        if target is None:
            raise ValueError(f"unknown schema: {name}")
        migration = self._migrations.get((name, from_version, target))
        if migration is None:
            raise ValueError(f"no migration for {name} {from_version} -> {target}")
        data = migration(value)
        self._validate_required(name, data)
        return SchemaEnvelope(name, target, data)

    @staticmethod
    def _validate_required(name: str, data: Mapping[str, Any]) -> None:
        required = {
            "source_document": {"document_id", "source_type", "content_hash"},
            "normalized_document": {"document_id", "knowledge_base", "content_hash"},
            "code_chunk": {"chunk_id", "document_id", "library", "version", "source_type"},
            "writing_entry": {"writing_id", "source_paper_id", "writing_function", "content_hash"},
            "environment_profile": {"name", "python_version", "packages", "captured_at"},
            "task_manifest": {"task_id", "task_type", "status", "started_at"},
            "query_result": {"answer_context", "sources", "confidence", "warnings"},
        }[name]
        missing = sorted(required - set(data))
        if missing:
            raise ValueError(f"{name} missing required fields: {', '.join(missing)}")
