"""Cross-domain integrity checks that never repair or delete data implicitly."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from knowledgehub.core.hashing import sha256_file


class HubValidator:
    def __init__(self, code_root: Path, writing_root: Path) -> None:
        self.code_root = code_root
        self.writing_root = writing_root

    def sources(self) -> dict[str, Any]:
        errors: list[str] = []
        checked = 0
        for marker in self.code_root.glob("sources/repositories/*/*/current.json"):
            checked += 1
            try:
                value = json.loads(marker.read_text(encoding="utf-8"))
                source = Path(str(value["source_path"]))
                if not source.is_dir() or len(str(value.get("commit") or "")) != 40:
                    errors.append(f"invalid source marker: {marker}")
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                errors.append(f"unreadable source marker: {marker}")
        return self._result("sources", checked, errors)

    def normalized(self) -> dict[str, Any]:
        errors: list[str] = []
        duplicates: set[str] = set()
        seen: set[str] = set()
        checked = 0
        for manifest in self.code_root.glob("normalized/**/*.jsonl"):
            if (
                manifest.parent == self.code_root / "normalized"
                and (manifest.parent / manifest.stem).is_dir()
            ):
                # V1 wrote one library-level manifest. V2 version-scoped
                # manifests supersede it without deleting the frozen file.
                continue
            for line_number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                checked += 1
                try:
                    value = json.loads(line)
                    document_id = str(value["document_id"])
                    metadata = value["metadata"]
                    if document_id in seen:
                        duplicates.add(document_id)
                    seen.add(document_id)
                    for field in ("library", "version", "source_type", "source_url", "commit"):
                        if field == "source_url":
                            present = value.get(field)
                        else:
                            present = metadata.get(field)
                        if not present:
                            errors.append(f"{manifest}:{line_number} missing {field}")
                    path = value.get("content_path")
                    if path and Path(path).is_file() and sha256_file(path) != value.get("content_hash"):
                        errors.append(f"{manifest}:{line_number} content hash mismatch")
                except (ValueError, KeyError, json.JSONDecodeError):
                    errors.append(f"{manifest}:{line_number} invalid record")
        errors.extend(f"duplicate document_id: {value}" for value in sorted(duplicates))
        return self._result("normalized", checked, errors)

    def writing(self) -> dict[str, Any]:
        path = self.writing_root / "derived" / "writing_entries.jsonl"
        errors: list[str] = []
        checked = 0
        if path.is_file():
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                checked += 1
                try:
                    value = json.loads(line)
                    for field in ("writing_id", "source_paper_id", "writing_function", "abstract_pattern", "content_hash"):
                        if not value.get(field):
                            errors.append(f"{path}:{line_number} missing {field}")
                except json.JSONDecodeError:
                    errors.append(f"{path}:{line_number} invalid JSON")
        else:
            errors.append(f"missing writing manifest: {path}")
        return self._result("writing", checked, errors)

    def all(self) -> dict[str, Any]:
        checks = [self.sources(), self.normalized(), self.writing()]
        return {"valid": all(item["valid"] for item in checks), "checks": checks}

    @staticmethod
    def _result(name: str, checked: int, errors: list[str]) -> dict[str, Any]:
        return {"check": name, "valid": not errors, "checked": checked, "errors": errors[:200]}
