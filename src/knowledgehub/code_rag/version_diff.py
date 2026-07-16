"""Deterministic symbol alignment and API signature differences."""

from __future__ import annotations

import ast
from typing import Any, Mapping


def compare_symbols(old: Mapping[str, Any] | None, new: Mapping[str, Any] | None) -> dict[str, Any]:
    if old is None:
        return {"status": "introduced", "confidence": 1.0, "changes": {}}
    if new is None:
        return {"status": "removed", "confidence": 1.0, "changes": {}}
    if old["ast_hash"] == new["ast_hash"]:
        status = "unchanged"
    elif old["signature"] != new["signature"]:
        status = "signature_changed"
    elif old["path"] != new["path"]:
        status = "moved"
    else:
        status = "modified"
    return {
        "status": status,
        "confidence": 1.0 if status in {"unchanged", "signature_changed"} else 0.8,
        "changes": signature_diff(str(old["signature"]), str(new["signature"])),
        "from": dict(old),
        "to": dict(new),
    }


def signature_diff(old: str, new: str) -> dict[str, list[Any]]:
    def arguments(value: str) -> dict[str, str | None]:
        try:
            node = ast.parse(f"def {value}:\n pass").body[0]
            assert isinstance(node, ast.FunctionDef)
        except (SyntaxError, AssertionError):
            return {}
        args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        defaults = [None] * (len(args) - len(node.args.defaults)) + list(node.args.defaults)
        return {arg.arg: ast.unparse(default) if default else None for arg, default in zip(args, defaults, strict=True)}
    before, after = arguments(old), arguments(new)
    return {
        "added_parameters": sorted(set(after) - set(before)),
        "removed_parameters": sorted(set(before) - set(after)),
        "renamed_parameters": [],
        "default_changes": [
            {"parameter": name, "from": before[name], "to": after[name]}
            for name in sorted(set(before) & set(after)) if before[name] != after[name]
        ],
        "type_changes": [],
        "return_changes": [],
    }
