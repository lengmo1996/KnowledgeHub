from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import yaml

from knowledgehub.code_rag.chunking import CodeChunker
from knowledgehub.code_rag.environment import EnvironmentCapture
from knowledgehub.code_rag.maintenance import OnDemandVersionImporter, ReleaseWatchService
from knowledgehub.code_rag.registry import CodeSourceRegistry
from knowledgehub.code_rag.sync import CodeSyncService
from knowledgehub.core.hashing import sha256_text
from knowledgehub.core.models import KnowledgeDocument


def _document(content: str, *, source_type: str, path: str) -> KnowledgeDocument:
    return KnowledgeDocument(
        document_id=f"code:example/repo@1.0:{path}",
        knowledge_base="code",
        source_type=source_type,
        title=path,
        content_hash=sha256_text(content),
        source_url="https://example.test",
        retrieved_at="2026-01-01T00:00:00Z",
        content=content,
        metadata={"path": path, "library": "example", "version": "1.0"},
    ).validate()


def test_python_ast_chunking_retains_symbols_and_lines() -> None:
    content = "import os\n\nclass Model:\n    def forward(self, value):\n        return value\n"
    chunks = CodeChunker().chunk(_document(content, source_type="source_code", path="src/pkg/model.py"))
    symbols = {chunk.metadata.get("symbol") for chunk in chunks}
    assert "src.pkg.model.Model" in symbols
    assert "src.pkg.model.Model.forward" in symbols
    method = next(chunk for chunk in chunks if chunk.metadata.get("symbol_type") == "method")
    assert method.metadata["start_line"] == 4
    assert method.metadata["source_type"] == "source_code"


def test_document_and_release_chunking() -> None:
    markdown = "# API\nUse `Model.run`.\n\n## Example\n```python\nModel().run()\n```"
    chunks = CodeChunker().chunk(_document(markdown, source_type="api_documentation", path="docs/api.md"))
    assert any(chunk.metadata.get("section") == "API > Example" for chunk in chunks)
    release = CodeChunker().chunk(
        _document("# Breaking changes\nThe old argument is deprecated.", source_type="release_note", path="release")
    )
    assert release[0].metadata["change_category"] == "breaking_changes"
    assert "deprecation" in release[0].metadata["task_tags"]


def test_environment_capture_is_sanitized_and_dry_run(tmp_path: Path, monkeypatch) -> None:
    capture = EnvironmentCapture(tmp_path)
    monkeypatch.setattr(
        capture,
        "_pip",
        lambda *args: "[]" if "--format=json" in args else "pkg @ https://user:secret@example.test/a.whl?token=x\n",
    )
    result = capture.capture(name="test", packages=("pytest",), dry_run=True)
    assert "secret" not in result["pip_freeze"][0]
    assert "token=x" not in result["pip_freeze"][0]
    assert not (tmp_path / "state" / "environments" / "test.json").exists()


def test_sync_fixed_tag_from_temporary_git_repository(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    subprocess.run(["git", "init"], cwd=upstream, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.test"], cwd=upstream, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=upstream, check=True)
    (upstream / "README.md").write_text("# Example\n", encoding="utf-8")
    (upstream / "src").mkdir()
    (upstream / "src" / "api.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=upstream, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=upstream, check=True, capture_output=True)
    subprocess.run(["git", "tag", "v1.0.0"], cwd=upstream, check=True)
    config = {
        "schema_version": 1,
        "defaults": {
            "enabled": True,
            "version_strategy": ["explicit"],
            "max_file_bytes": 10000,
            "max_files": 10,
            "include": ["README*", "src/**"],
            "exclude": [],
            "releases": {"enabled": False, "limit": 0},
            "issues": {"enabled": False, "limit": 0},
        },
        "libraries": {
            "example": {
                "package_name": "example",
                "repository": str(upstream),
                "tag_patterns": ["v{version}"],
            }
        },
    }
    registry_path = tmp_path / "registry.yaml"
    registry_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    service = CodeSyncService(CodeSourceRegistry.load(registry_path), tmp_path / "data")
    result = service.sync("example", version="1.0.0")
    item = result["results"][0]
    assert item["tag"] == "v1.0.0"
    assert len(item["commit"]) == 40
    assert (Path(item["source_path"]) / "src" / "api.py").is_file()
    assert not (Path(item["source_path"]) / ".git").exists()


def test_release_rate_limit_is_explicit_partial_success(tmp_path: Path, monkeypatch) -> None:
    config = {
        "schema_version": 1,
        "defaults": {
            "enabled": True,
            "version_strategy": ["explicit"],
            "max_file_bytes": 10000,
            "max_files": 10,
            "include": ["README*"],
            "exclude": [],
            "releases": {"enabled": True, "limit": 1},
            "issues": {"enabled": False, "limit": 0},
        },
        "libraries": {
            "example": {
                "package_name": "example",
                "repository": "owner/example",
                "tag_patterns": ["v{version}"],
            }
        },
    }
    registry_path = tmp_path / "registry.yaml"
    registry_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    service = CodeSyncService(CodeSourceRegistry.load(registry_path), tmp_path / "data")
    monkeypatch.setattr(service, "_remote_tags", lambda _library: ["v1.0.0"])
    monkeypatch.setattr(
        service,
        "_sync_version",
        lambda library, version, tag: SimpleNamespace(
            to_dict=lambda: {
                "library": library.name,
                "version": version,
                "tag": tag,
                "commit": "a" * 40,
                "source_path": "/tmp/source",
                "status": "synced",
            }
        ),
    )
    request = httpx.Request("GET", "https://api.github.test/releases")
    response = httpx.Response(403, request=request)
    monkeypatch.setattr(
        service,
        "_sync_releases",
        lambda _library: (_ for _ in ()).throw(
            httpx.HTTPStatusError("rate limit", request=request, response=response)
        ),
    )
    result = service.sync("example", version="1.0.0")
    assert result["status"] == "partial"
    assert result["release_error"] == "github_releases_http_403"
    assert result["results"][0]["commit"] == "a" * 40


def test_release_watch_never_downloads_and_on_demand_requires_permission(
    tmp_path: Path, monkeypatch
) -> None:
    config_value = {
        "schema_version": 1,
        "defaults": {"include": ["README*"], "version_strategy": ["latest"]},
        "libraries": {
            "example": {
                "enabled": True,
                "package_name": "example-not-installed",
                "repository": "owner/example",
            }
        },
    }
    registry_path = tmp_path / "registry.yaml"
    registry_path.write_text(yaml.safe_dump(config_value), encoding="utf-8")
    registry = CodeSourceRegistry.load(registry_path)
    config = SimpleNamespace(
        code=SimpleNamespace(
            data_root=tmp_path / "data",
            github_token_env="GITHUB_TOKEN",
            timeout_seconds=1,
            max_retries=0,
        )
    )
    monkeypatch.setattr(
        CodeSyncService, "_remote_tags", lambda _self, _library: ["v1.0.0", "v2.0.0rc1"]
    )
    watched = ReleaseWatchService(config, registry).check("example")
    assert watched["latest_tag"] == "v1.0.0"
    assert watched["action"] == "notify" and watched["auto_downloaded"] is False
    assert (tmp_path / "data" / "state" / "release-watch" / "example.json").is_file()
    denied = OnDemandVersionImporter(config, registry).import_version(
        "example", "1.0.0", allowed=False
    )
    assert denied["status"] == "permission_required"
