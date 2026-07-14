from __future__ import annotations

import pytest

from knowledgehub.sources.zotero.collections import (
    Collection,
    build_collection_paths,
    collection_paths,
)


def test_builds_stable_paths_from_zotero_mappings() -> None:
    raw = [
        {"key": "CHILD", "data": {"name": "Papers", "parentCollection": "ROOT"}},
        {"key": "ROOT", "data": {"name": "Research", "parentCollection": False}},
        {"key": "LEAF", "data": {"name": "2026", "parentCollection": "CHILD"}},
    ]

    result = build_collection_paths(reversed(raw))

    assert result.by_key == {
        "CHILD": "Research/Papers",
        "LEAF": "Research/Papers/2026",
        "ROOT": "Research",
    }
    assert [(item.path, item.key) for item in result.ordered] == sorted(
        (item.path, item.key) for item in result.ordered
    )
    assert result.errors == ()


def test_missing_parent_uses_fallback_and_descendants_append() -> None:
    result = build_collection_paths(
        [
            Collection("ORPHAN", "Orphan", "GONE"),
            Collection("CHILD", "Child", "ORPHAN"),
        ]
    )

    assert result.by_key["ORPHAN"] == "[missing:GONE]/Orphan"
    assert result.by_key["CHILD"] == "[missing:GONE]/Orphan/Child"
    assert [(error.code, error.collection_key) for error in result.errors] == [
        ("missing_collection_parent", "ORPHAN")
    ]


def test_cycle_members_receive_keyed_fallbacks() -> None:
    result = build_collection_paths(
        [
            Collection("A", "Alpha", "B"),
            Collection("B", "Beta", "C"),
            Collection("C", "Gamma", "A"),
            Collection("D", "Descendant", "A"),
        ]
    )

    assert result.by_key["A"] == "[cycle]/A/Alpha"
    assert result.by_key["B"] == "[cycle]/B/Beta"
    assert result.by_key["C"] == "[cycle]/C/Gamma"
    assert result.by_key["D"] == "[cycle]/A/Alpha/Descendant"
    assert {(error.code, error.collection_key) for error in result.errors} == {
        ("collection_cycle", "A"),
        ("collection_cycle", "B"),
        ("collection_cycle", "C"),
    }


def test_self_cycle_and_empty_name_are_deterministic() -> None:
    result = build_collection_paths([Collection("SELF", "", "SELF")])
    assert result.by_key == {"SELF": "[cycle]/SELF/[unnamed:SELF]"}


def test_order_of_input_does_not_change_paths_or_errors() -> None:
    nodes = [
        Collection("A", "A", "B"),
        Collection("B", "B", "A"),
        Collection("C", "C", "MISSING"),
    ]

    forward = build_collection_paths(nodes)
    backward = build_collection_paths(reversed(nodes))

    assert forward == backward


def test_duplicate_collection_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate collection key"):
        build_collection_paths([Collection("A", "one"), Collection("A", "two")])


def test_convenience_function_returns_plain_dict() -> None:
    assert collection_paths([Collection("A", "Alpha")]) == {"A": "Alpha"}
