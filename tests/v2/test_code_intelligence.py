from __future__ import annotations

from pathlib import Path

from knowledgehub.code_rag.adapters import adapter_for
from knowledgehub.code_rag.symbols import SymbolIndex
from knowledgehub.code_rag.version_diff import compare_symbols, signature_diff
from knowledgehub.code_rag.versioning import NormalizedVersion


def test_version_object_and_five_adapters() -> None:
    value = NormalizedVersion.parse("2.6.0+cu124")
    assert value.normalized == "2.6.0" and value.local_build == "cu124"
    assert NormalizedVersion.parse("v2.0.0rc1").release_type == "prerelease"
    for name in ("pytorch", "transformers", "diffusers", "accelerate", "lightning"):
        assert adapter_for(name).name == name


def test_symbol_relations_and_signature_diff(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    path = root / "src" / "pkg" / "model.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "import torch\n\nclass Model(Base):\n    @property\n    def name(self):\n        return 'x'\n    def forward(self, x, scale=1):\n        return helper(x)\n",
        encoding="utf-8",
    )
    index = SymbolIndex(tmp_path / "symbols.sqlite3")
    result = index.build("pkg", "1.0", root, [path])
    assert result["symbols"] >= 4 and result["relations"] >= 2
    symbol = index.inspect("pkg", "1.0", "Model.forward")
    assert symbol is not None
    assert any(item["relation"] == "calls" for item in symbol["relations"])
    changes = signature_diff("run(x, old=1)", "run(x, new=1)")
    assert changes["added_parameters"] == ["new"]
    typed = signature_diff(
        "run(x: Config) -> Model", "run(x: Config | str) -> Model | None"
    )
    assert typed["type_changes"] == [
        {"parameter": "x", "from": "Config", "to": "Config | str"}
    ]
    assert typed["return_changes"] == [
        {"from": "Model", "to": "Model | None"}
    ]
    assert compare_symbols(symbol, {**symbol, "signature": "forward(x, flag=False)", "ast_hash": "different"})["status"] == "signature_changed"


def test_duplicate_conditional_symbol_has_one_stable_identity(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    path = root / "src" / "api.py"
    path.parent.mkdir(parents=True)
    path.write_text("if True:\n    pass\n\ndef run(x):\n    return x\n\ndef run(x, flag=False):\n    return x\n", encoding="utf-8")
    index = SymbolIndex(tmp_path / "symbols.sqlite3")
    assert index.build("pkg", "1", root, [path])["symbols"] == 1
    value = index.inspect("pkg", "1", "run")
    assert value is not None and "flag" in value["signature"]
