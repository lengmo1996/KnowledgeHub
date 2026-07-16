from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import anyio

from knowledgehub.code_rag.symbols import SymbolIndex
from knowledgehub.mcp.config import MCPConfig
from knowledgehub.mcp.tools import ToolRegistry


class Service:
    def __init__(self) -> None:
        self.config = SimpleNamespace(reranker_profile="off")


def _config(tmp_path: Path) -> Path:
    code = tmp_path / "code"
    writing = tmp_path / "writing"
    environment = code / "state" / "environments"
    environment.mkdir(parents=True)
    (environment / "test.json").write_text(
        json.dumps({"name": "test", "packages": {"demo": "1.5"}}), encoding="utf-8"
    )
    path = tmp_path / "hub.yaml"
    base = (Path(__file__).parents[2] / "configs" / "rag" / "default.yaml").resolve()
    registry = (Path(__file__).parents[2] / "configs" / "sources" / "code.yaml").resolve()
    path.write_text(
        f"""schema_version: 1
base_rag_config: {base}
knowledge_bases:
  literature: {{data_dir: {tmp_path}/literature, collection: literature, query_instruction: q}}
  code: {{data_dir: {tmp_path}/rag-code, collection: code, query_instruction: q}}
  writing: {{data_dir: {tmp_path}/rag-writing, collection: writing, query_instruction: q}}
code:
  data_root: {code}
  registry: {registry}
writing:
  data_root: {writing}
  literature_data_dir: {tmp_path}/literature
""",
        encoding="utf-8",
    )
    return path


def test_symbol_repository_and_feedback_tools(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    config = _config(tmp_path)
    monkeypatch.setenv("KH_HUB_CONFIG", str(config))
    monkeypatch.setenv("KH_REPOSITORY_ROOT", str(tmp_path))

    source_root = tmp_path / "symbol-source"
    source_root.mkdir()
    old = source_root / "old.py"
    old.write_text("def run(x):\n    return x\n", encoding="utf-8")
    catalog = SymbolIndex(tmp_path / "code" / "state" / "symbols.sqlite3")
    catalog.build("demo", "1.0", source_root, [old])
    old.write_text("def run(x, flag=False):\n    return x\n", encoding="utf-8")
    catalog.build("demo", "2.0", source_root, [old])

    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "requirements.txt").write_text("demo>=1,<2\n", encoding="utf-8")
    (repository / "main.py").write_text("import demo\ndemo.run()\n", encoding="utf-8")
    tools = ToolRegistry(Service(), MCPConfig(max_response_bytes=200_000))

    async def exercise() -> None:
        inspected = await tools.call(
            "knowledge_inspect_symbol",
            {"library": "demo", "version": "1.0", "symbol": "run"},
        )
        assert inspected.structuredContent["result"]["version"] == "1.0"
        compared = await tools.call(
            "knowledge_compare_symbols",
            {
                "library": "demo",
                "from_version": "1.0",
                "to_version": "2.0",
                "symbol": "run",
            },
        )
        assert compared.structuredContent["result"]["status"] == "signature_changed"
        analyzed = await tools.call(
            "knowledge_analyze_repository",
            {"repository": "repository", "environment": "test"},
        )
        assert analyzed.structuredContent["result"]["profile"]["repository"] == "repository"
        feedback = await tools.call(
            "knowledge_submit_feedback",
            {"writing_id": "w1", "label": "useful", "context": {"rank": 1}},
        )
        assert feedback.structuredContent["result"]["label"] == "useful"

    anyio.run(exercise)
    assert not (tmp_path / "repository" / "repository_profile.json").exists()


def test_repository_tool_rejects_absolute_path(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("KH_HUB_CONFIG", str(_config(tmp_path)))
    monkeypatch.setenv("KH_REPOSITORY_ROOT", str(tmp_path))
    tools = ToolRegistry(Service(), MCPConfig())
    result = anyio.run(
        tools.call,
        "knowledge_analyze_repository",
        {"repository": str(tmp_path), "environment": "test"},
    )
    assert result.isError
    assert result.structuredContent["error"]["code"] == "invalid_repository"
