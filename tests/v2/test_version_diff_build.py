from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

from knowledgehub.cli.main import build_parser
from knowledgehub.code_rag.diffs import VersionDiffBuildService
from knowledgehub.code_rag.registry import CodeSourceRegistry
from knowledgehub.code_rag.symbols import SymbolIndex
from knowledgehub.pipeline.config import RagConfig


class CapturingIndexer:
    def __init__(self) -> None:
        self.values: list[Any] = []

    def build(self, values: list[Any], **_kwargs: Any) -> SimpleNamespace:
        self.values = list(values)
        chunks = sum(len(value.chunks) for value in values)
        return SimpleNamespace(
            to_dict=lambda: {
                "status": "success",
                "selected": len(values),
                "indexed": len(values),
                "skipped": 0,
                "tombstoned": 0,
                "chunks": chunks,
                "dry_run": bool(_kwargs.get("dry_run")),
                "failures": [],
                "knowledge_base": "code",
            }
        )

    def close(self) -> None:
        pass


def _service(tmp_path: Path) -> tuple[VersionDiffBuildService, CapturingIndexer]:
    registry_path = tmp_path / "registry.yaml"
    registry_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "defaults": {"include": ["src/**"], "version_strategy": ["latest"]},
                "libraries": {
                    "demo": {
                        "enabled": True,
                        "package_name": "demo-package",
                        "repository": "owner/demo",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    data_root = tmp_path / "code"
    catalog = SymbolIndex(data_root / "state" / "symbols.sqlite3")
    for version, content, commit in (
        ("1.0", "class Model:\n    def run(self, value):\n        return value\n", "a" * 40),
        (
            "2.0",
            "class Model:\n    def run(self, value, strict=False):\n        return value\n",
            "b" * 40,
        ),
    ):
        root = tmp_path / f"source-{version}"
        path = root / "src" / "demo.py"
        path.parent.mkdir(parents=True)
        path.write_text(content, encoding="utf-8")
        catalog.build("demo", version, root, [path])
        marker = data_root / "sources" / "repositories" / "demo" / version / "current.json"
        marker.parent.mkdir(parents=True)
        marker.write_text(
            json.dumps(
                {
                    "library": "demo",
                    "version": version,
                    "tag": f"v{version}",
                    "commit": commit,
                    "source_path": str(root),
                    "retrieved_at": "2026-01-01T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
    indexer = CapturingIndexer()
    service = VersionDiffBuildService(
        CodeSourceRegistry.load(registry_path),
        data_root,
        RagConfig(data_dir=tmp_path / "rag", gpu_mode="cpu", embedding_dim=2),
        catalog=SymbolIndex(data_root / "state" / "symbols.sqlite3", read_only=True),
        indexer=indexer,  # type: ignore[arg-type]
    )
    return service, indexer


def test_version_diff_build_emits_pinned_structured_evidence(tmp_path: Path) -> None:
    service, indexer = _service(tmp_path)
    result = service.build(
        "demo",
        "1.0",
        "2.0",
        symbols=("Model.run",),
        dry_run=False,
    )
    assert result["diff_documents"] == 1
    assert result["change_statuses"] == {"signature_changed": 1}
    value = indexer.values[0]
    document = value.document
    assert document.source_type == "version_diff"
    assert document.metadata["from_commit"] == "a" * 40
    assert document.metadata["to_commit"] == "b" * 40
    assert document.metadata["changes"]["added_parameters"] == ["strict"]
    assert "```diff" in document.content
    assert "strict=False" in document.content
    normalized = Path(result["normalized_manifest"])
    assert normalized.is_file()
    stored = json.loads(normalized.read_text(encoding="utf-8").splitlines()[0])
    assert stored["metadata"]["source_type"] == "version_diff"


def test_changed_pair_catalog_and_diff_cli_are_bounded(tmp_path: Path) -> None:
    service, _indexer = _service(tmp_path)
    pairs = service.catalog.changed_pairs("demo", "1.0", "2.0", limit=1)
    assert len(pairs) == 1
    assert pairs[0][0]["qualified_name"] == pairs[0][1]["qualified_name"]
    args = build_parser().parse_args(
        [
            "build",
            "diff",
            "--library",
            "demo",
            "--from-version",
            "1.0",
            "--to-version",
            "2.0",
            "--limit",
            "3",
            "--dry-run",
        ]
    )
    assert args.build_domain == "diff"
    assert args.limit == 3 and args.dry_run is True
