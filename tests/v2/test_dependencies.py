from __future__ import annotations

import json
from pathlib import Path

import yaml

from knowledgehub.cli.main import build_parser
from knowledgehub.code_rag.dependencies import DependencyManifestService
from knowledgehub.code_rag.registry import CodeSourceRegistry
from knowledgehub.governance.validation import HubValidator


def test_pinned_dependency_manifest_preserves_scope_and_evidence(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.yaml"
    registry_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "defaults": {"include": ["**"], "version_strategy": ["latest"]},
                "libraries": {
                    "demo": {
                        "enabled": True,
                        "package_name": "demo-package",
                        "repository": "owner/demo",
                    },
                    "transformers": {
                        "package_name": "transformers",
                        "repository": "huggingface/transformers",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    data = tmp_path / "code"
    root = tmp_path / "source"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        """[build-system]
requires = ["setuptools>=68"]
[project]
dependencies = ["transformers>=5", "numpy>=2"]
[project.optional-dependencies]
test = ["pytest>=8"]
""",
        encoding="utf-8",
    )
    (root / "setup.py").write_text(
        "from setuptools import setup\n_deps = ['datasets>=3']\nsetup(install_requires=['torch>=2'])\n",
        encoding="utf-8",
    )
    marker = data / "sources" / "repositories" / "demo" / "1.0" / "current.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(
        json.dumps(
            {
                "source_path": str(root),
                "commit": "a" * 40,
                "tag": "v1.0",
                "retrieved_at": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    service = DependencyManifestService(CodeSourceRegistry.load(registry_path), data)
    result = service.capture("demo", "1.0")
    assert result["status"] == "success"
    dependencies = {item["normalized_package"]: item for item in result["dependencies"]}
    assert dependencies["transformers"]["target_library"] == "transformers"
    assert dependencies["transformers"]["evidence_kind"] == "declared"
    assert dependencies["pytest"]["scope"] == "optional"
    assert dependencies["setuptools"]["scope"] == "build"
    assert dependencies["torch"]["source"] == "setup.py:setup.install_requires"
    assert dependencies["datasets"]["scope"] == "catalog"
    assert dependencies["datasets"]["relation"] == "lists_dependency"
    assert all(item["inference"] is False for item in dependencies.values())
    assert Path(result["manifest"]).is_file()


def test_dependency_dry_run_and_cli_are_non_mutating(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        ["source", "dependencies", "transformers", "--version", "5.13.1", "--dry-run"]
    )
    assert args.hub_source_command == "dependencies"
    assert args.dry_run is True


def test_dependency_manifest_validation_detects_marker_and_hash_drift(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "registry.yaml"
    registry_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "defaults": {"include": ["**"], "version_strategy": ["latest"]},
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
    data = tmp_path / "code"
    source = tmp_path / "source"
    source.mkdir()
    (source / "requirements.txt").write_text("numpy>=2\n", encoding="utf-8")
    marker = data / "sources/repositories/demo/1.0/current.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(
        json.dumps(
            {
                "source_path": str(source),
                "commit": "a" * 40,
                "tag": "v1.0",
                "retrieved_at": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    result = DependencyManifestService(
        CodeSourceRegistry.load(registry_path), data
    ).capture("demo", "1.0")
    validator = HubValidator(data, tmp_path / "writing")
    assert validator.dependencies()["valid"] is True

    manifest = Path(result["manifest"])
    value = json.loads(manifest.read_text(encoding="utf-8"))
    value["content_hash"] = "0" * 64
    manifest.write_text(json.dumps(value), encoding="utf-8")
    invalid = validator.dependencies()
    assert invalid["valid"] is False
    assert any("content_hash mismatch" in error for error in invalid["errors"])
