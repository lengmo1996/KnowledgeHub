from __future__ import annotations

from pathlib import Path

import pytest

from knowledgehub.core.atomic import PathOutsideRootError, safe_remove


def test_safe_remove_unlinks_in_root_symlink_without_removing_referent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    root.mkdir()
    referent = root / "payload.txt"
    referent.write_bytes(b"keep me")
    link = root / "payload-link"
    link.symlink_to(referent)

    safe_remove(link, root=root)

    assert referent.read_bytes() == b"keep me"
    assert not link.exists()
    assert not link.is_symlink()


def test_safe_remove_unlinks_in_root_symlink_pointing_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    referent = tmp_path / "external.txt"
    referent.write_bytes(b"external")
    link = root / "external-link"
    link.symlink_to(referent)

    safe_remove(link, root=root)

    assert referent.read_bytes() == b"external"
    assert not link.is_symlink()


def test_safe_remove_rejects_outside_symlink_even_when_it_points_inside_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    root.mkdir()
    referent = root / "inside.txt"
    referent.write_bytes(b"inside")
    outside_link = tmp_path / "outside-link"
    outside_link.symlink_to(referent)

    with pytest.raises(PathOutsideRootError):
        safe_remove(outside_link, root=root)

    assert outside_link.is_symlink()
    assert referent.read_bytes() == b"inside"
