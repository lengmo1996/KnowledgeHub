"""Deterministic Zotero collection hierarchy projection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class Collection:
    key: str
    name: str
    parent_key: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "Collection":
        """Accept either Zotero's ``{key, data: ...}`` or a flat mapping."""

        nested = value.get("data")
        data = nested if isinstance(nested, Mapping) else value
        key = value.get("key", data.get("key"))
        name = data.get("name", "")
        parent = data.get("parentCollection", data.get("parent_key"))
        if not isinstance(key, str) or not key:
            raise ValueError("collection key must be a non-empty string")
        if not isinstance(name, str):
            raise ValueError(f"collection {key!r} name must be a string")
        if parent in {None, False, ""}:
            parent_key = None
        elif isinstance(parent, str):
            parent_key = parent
        else:
            raise ValueError(f"collection {key!r} parent must be a string or null")
        return cls(key=key, name=name, parent_key=parent_key)


@dataclass(frozen=True, slots=True)
class CollectionValidationError:
    code: str
    collection_key: str
    detail: str


@dataclass(frozen=True, slots=True)
class CollectionPath:
    key: str
    name: str
    parent_key: str | None
    path: str


@dataclass(frozen=True, slots=True)
class CollectionPaths:
    by_key: Mapping[str, str]
    ordered: tuple[CollectionPath, ...]
    errors: tuple[CollectionValidationError, ...]


def _coerce_collection(value: Collection | Mapping[str, object]) -> Collection:
    return value if isinstance(value, Collection) else Collection.from_mapping(value)


def build_collection_paths(
    collections: Iterable[Collection | Mapping[str, object]],
) -> CollectionPaths:
    """Build stable paths using a three-colour DFS.

    Cycle members receive ``[cycle]/<key>/<name>``.  A node whose parent is
    absent receives ``[missing:<parent-key>]/<name>``.  Descendants append their
    own names to either fallback, so every input collection still has a useful,
    deterministic path.
    """

    nodes: dict[str, Collection] = {}
    for raw in collections:
        node = _coerce_collection(raw)
        if node.key in nodes:
            raise ValueError(f"duplicate collection key: {node.key}")
        nodes[node.key] = node

    # 0 = unseen, 1 = active, 2 = complete.
    colours: dict[str, int] = {key: 0 for key in nodes}
    paths: dict[str, str] = {}
    stack: list[str] = []
    error_index: dict[tuple[str, str], CollectionValidationError] = {}

    def add_error(code: str, key: str, detail: str) -> None:
        error_index[(code, key)] = CollectionValidationError(code, key, detail)

    def fallback_name(node: Collection) -> str:
        return node.name if node.name else f"[unnamed:{node.key}]"

    def visit(key: str) -> str:
        if key in paths and colours[key] == 2:
            return paths[key]
        if colours[key] == 1:
            cycle_start = stack.index(key)
            cycle = stack[cycle_start:]
            cycle_description = " -> ".join([*cycle, key])
            for cycle_key in cycle:
                cycle_node = nodes[cycle_key]
                paths[cycle_key] = f"[cycle]/{cycle_key}/{fallback_name(cycle_node)}"
                add_error("collection_cycle", cycle_key, cycle_description)
            return paths[key]

        colours[key] = 1
        stack.append(key)
        node = nodes[key]
        name = fallback_name(node)
        if node.parent_key is None:
            candidate = name
        elif node.parent_key not in nodes:
            candidate = f"[missing:{node.parent_key}]/{name}"
            add_error(
                "missing_collection_parent",
                key,
                f"parent collection {node.parent_key!r} is absent",
            )
        else:
            parent_path = visit(node.parent_key)
            candidate = f"{parent_path}/{name}"

        # A recursive back-edge may already have assigned a cycle fallback.
        if key not in paths:
            paths[key] = candidate
        stack.pop()
        colours[key] = 2
        return paths[key]

    for key in sorted(nodes):
        if colours[key] == 0:
            visit(key)

    stable_by_key = {key: paths[key] for key in sorted(paths)}
    ordered = tuple(
        sorted(
            (
                CollectionPath(
                    key=node.key,
                    name=node.name,
                    parent_key=node.parent_key,
                    path=paths[node.key],
                )
                for node in nodes.values()
            ),
            key=lambda value: (value.path, value.key),
        )
    )
    errors = tuple(
        sorted(
            error_index.values(),
            key=lambda value: (value.code, value.collection_key, value.detail),
        )
    )
    return CollectionPaths(by_key=stable_by_key, ordered=ordered, errors=errors)


def collection_paths(
    collections: Sequence[Collection | Mapping[str, object]],
) -> dict[str, str]:
    """Convenience wrapper returning only the key-to-path mapping."""

    return dict(build_collection_paths(collections).by_key)


__all__ = [
    "Collection",
    "CollectionPath",
    "CollectionPaths",
    "CollectionValidationError",
    "build_collection_paths",
    "collection_paths",
]
