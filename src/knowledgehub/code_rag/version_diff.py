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
    def signature(value: str) -> tuple[dict[str, dict[str, str | None]], str | None]:
        try:
            node = ast.parse(f"def {value}:\n pass").body[0]
            assert isinstance(node, ast.FunctionDef)
        except (SyntaxError, AssertionError):
            return {}, None
        positional = [*node.args.posonlyargs, *node.args.args]
        positional_defaults = [None] * (
            len(positional) - len(node.args.defaults)
        ) + list(node.args.defaults)
        values: dict[str, dict[str, str | None]] = {}
        for argument, default in zip(positional, positional_defaults, strict=True):
            values[argument.arg] = {
                "annotation": ast.unparse(argument.annotation)
                if argument.annotation
                else None,
                "default": ast.unparse(default) if default else None,
            }
        for argument, default in zip(
            node.args.kwonlyargs, node.args.kw_defaults, strict=True
        ):
            values[argument.arg] = {
                "annotation": ast.unparse(argument.annotation)
                if argument.annotation
                else None,
                "default": ast.unparse(default) if default else None,
            }
        for optional_argument in (node.args.vararg, node.args.kwarg):
            if optional_argument is not None:
                values[optional_argument.arg] = {
                    "annotation": ast.unparse(optional_argument.annotation)
                    if optional_argument.annotation
                    else None,
                    "default": None,
                }
        returns = ast.unparse(node.returns) if node.returns else None
        return values, returns

    before, before_return = signature(old)
    after, after_return = signature(new)
    shared = sorted(set(before) & set(after))
    return {
        "added_parameters": sorted(set(after) - set(before)),
        "removed_parameters": sorted(set(before) - set(after)),
        "renamed_parameters": [],
        "default_changes": [
            {
                "parameter": name,
                "from": before[name]["default"],
                "to": after[name]["default"],
            }
            for name in shared
            if before[name]["default"] != after[name]["default"]
        ],
        "type_changes": [
            {
                "parameter": name,
                "from": before[name]["annotation"],
                "to": after[name]["annotation"],
            }
            for name in shared
            if before[name]["annotation"] != after[name]["annotation"]
        ],
        "return_changes": (
            [{"from": before_return, "to": after_return}]
            if before_return != after_return
            else []
        ),
    }
