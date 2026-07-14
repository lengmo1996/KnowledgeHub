from __future__ import annotations

import hashlib
import stat
import zipfile
from pathlib import Path

import pytest

from knowledgehub.sources.zotero.attachments import (
    AttachmentCacheState,
    AttachmentRequest,
    AttachmentResolver,
    AttachmentStatus,
    check_archives_stable,
    inspect_archive,
    locate_archive,
    validate_attachment_mapping,
)

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def _write_archive(
    root: Path,
    key: str,
    members: dict[str, bytes],
    *,
    prop: bool = True,
    nested: bool = False,
) -> Path:
    directory = root / key if nested else root
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / f"{key}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as stream:
        for name, content in members.items():
            stream.writestr(name, content)
    if prop:
        archive.with_suffix(".prop").write_text("mtime=1", encoding="utf-8")
    return archive


def _resolver(webdav: Path, extracted: Path, **kwargs: object) -> AttachmentResolver:
    return AttachmentResolver(
        webdav,
        extracted,
        stability_interval=0,
        sleeper=lambda _: None,
        **kwargs,
    )


def test_flat_archive_wins_over_nested_fallback(tmp_path: Path) -> None:
    root = tmp_path / "webdav"
    root.mkdir()
    flat = _write_archive(root, "ABCD1234", {"flat.pdf": PDF})
    _write_archive(root, "ABCD1234", {"nested.pdf": PDF}, nested=True)

    location = locate_archive(root, "ABCD1234")

    assert location.problem is None
    assert location.archive_path == flat


def test_nested_fallback_and_ambiguity_are_deterministic(tmp_path: Path) -> None:
    root = tmp_path / "webdav"
    root.mkdir()
    nested = _write_archive(root, "KEY1", {"paper.pdf": PDF}, nested=True)
    assert locate_archive(root, "KEY1").archive_path == nested

    other = nested.parent / "other.zip"
    other.write_bytes(nested.read_bytes())
    other.with_suffix(".prop").write_text("ok", encoding="utf-8")
    location = locate_archive(root, "KEY1")
    assert location.problem is AttachmentStatus.AMBIGUOUS_ARCHIVE
    assert location.detail == '{"candidates":["KEY1.zip","other.zip"]}'


def test_missing_prop_and_symlink_are_never_read(tmp_path: Path) -> None:
    root = tmp_path / "webdav"
    root.mkdir()
    archive = _write_archive(root, "NOPROP", {"paper.pdf": PDF}, prop=False)
    assert locate_archive(root, "NOPROP").problem is AttachmentStatus.UNSTABLE_ARCHIVE

    archive.unlink()
    target = tmp_path / "outside.zip"
    target.write_bytes(b"secret")
    archive.symlink_to(target)
    location = locate_archive(root, "NOPROP")
    assert location.problem is AttachmentStatus.UNSAFE_ARCHIVE


def test_batch_stability_check_observes_all_candidates(tmp_path: Path) -> None:
    root = tmp_path / "webdav"
    root.mkdir()
    one = _write_archive(root, "ONE", {"one.pdf": PDF})
    _write_archive(root, "TWO", {"two.pdf": PDF})
    locations = [locate_archive(root, "ONE"), locate_archive(root, "TWO")]

    def mutate(_: float) -> None:
        one.write_bytes(one.read_bytes() + b"changed")

    result = check_archives_stable(locations, interval=0, sleeper=mutate)

    assert not result["ONE"].stable
    assert result["TWO"].stable


@pytest.mark.parametrize(
    "member",
    ["../escape.pdf", "/absolute.pdf", "C:/windows.pdf", "folder\\escape.pdf"],
)
def test_inspection_rejects_dangerous_member_paths(tmp_path: Path, member: str) -> None:
    root = tmp_path / "webdav"
    root.mkdir()
    archive = _write_archive(root, "BADPATH", {member: PDF})

    result = inspect_archive(archive)

    assert result.status is AttachmentStatus.UNSAFE_ARCHIVE


def test_inspection_rejects_symlink_and_special_members(tmp_path: Path) -> None:
    archive = tmp_path / "links.zip"
    for mode in (stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o600):
        with zipfile.ZipFile(archive, "w") as stream:
            info = zipfile.ZipInfo("paper.pdf")
            info.create_system = 3
            info.external_attr = mode << 16
            stream.writestr(info, PDF)
        assert inspect_archive(archive).status is AttachmentStatus.UNSAFE_ARCHIVE


def test_pdf_selection_prefers_unique_api_filename(tmp_path: Path) -> None:
    root = tmp_path / "webdav"
    root.mkdir()
    archive = _write_archive(
        root,
        "SELECT",
        {"supplement.pdf": PDF + b"supplement", "folder/article.pdf": PDF},
    )

    exact = inspect_archive(archive, "article.pdf")
    ambiguous = inspect_archive(archive, "missing.pdf")

    assert exact.status is AttachmentStatus.READY
    assert exact.selected_member == "folder/article.pdf"
    assert ambiguous.status is AttachmentStatus.AMBIGUOUS_ATTACHMENT
    assert ambiguous.pdf_members == ("folder/article.pdf", "supplement.pdf")


def test_single_pdf_fallback_and_missing_pdf_wire_value(tmp_path: Path) -> None:
    root = tmp_path / "webdav"
    root.mkdir()
    one = _write_archive(root, "ONEPDF", {"unexpected.pdf": PDF})
    none = _write_archive(root, "NOPDF", {"notes.txt": b"text"})

    assert inspect_archive(one, "expected.pdf").selected_member == "unexpected.pdf"
    result = inspect_archive(none)
    assert result.status.value == "missing_pdf"


def test_resolve_extracts_only_pdf_and_does_not_mutate_webdav(tmp_path: Path) -> None:
    webdav = tmp_path / "webdav"
    extracted = tmp_path / "extracted"
    webdav.mkdir()
    archive = _write_archive(
        webdav,
        "ATTACH",
        {"nested/paper.pdf": PDF, "nested/notes.txt": b"not extracted"},
    )
    prop = archive.with_suffix(".prop")
    before = (archive.stat(), prop.stat(), hashlib.sha256(archive.read_bytes()).hexdigest())

    result = _resolver(webdav, extracted).resolve(AttachmentRequest("ATTACH", "paper.pdf"))

    after = (archive.stat(), prop.stat(), hashlib.sha256(archive.read_bytes()).hexdigest())
    assert result.ready
    assert result.pdf_path is not None
    assert Path(result.pdf_path).read_bytes() == PDF
    assert list((extracted / "ATTACH").iterdir()) == [Path(result.pdf_path)]
    assert before[0].st_ino == after[0].st_ino
    assert before[0].st_mtime_ns == after[0].st_mtime_ns
    assert before[1].st_mtime_ns == after[1].st_mtime_ns
    assert before[2] == after[2]


def test_unchanged_archive_reuses_verified_pdf_cache(tmp_path: Path) -> None:
    webdav = tmp_path / "webdav"
    extracted = tmp_path / "extracted"
    webdav.mkdir()
    _write_archive(webdav, "CACHE", {"paper.pdf": PDF})
    resolver = _resolver(webdav, extracted)
    first = resolver.resolve(AttachmentRequest("CACHE", "paper.pdf"))
    assert first.ready and first.pdf_path
    pdf_stat = Path(first.pdf_path).stat()

    second = resolver.resolve(
        AttachmentRequest("CACHE", "paper.pdf"),
        previous=AttachmentCacheState(
            archive_sha256=first.archive_sha256,
            pdf_sha256=first.pdf_sha256,
            pdf_path=first.pdf_path,
            api_filename="paper.pdf",
        ),
    )

    assert second.ready and second.reused
    assert Path(second.pdf_path or "").stat().st_ino == pdf_stat.st_ino


def test_corrupt_cache_is_reextracted_but_bad_new_archive_keeps_old_cache(
    tmp_path: Path,
) -> None:
    webdav = tmp_path / "webdav"
    extracted = tmp_path / "extracted"
    webdav.mkdir()
    archive = _write_archive(webdav, "RECOVER", {"paper.pdf": PDF})
    resolver = _resolver(webdav, extracted)
    first = resolver.resolve(AttachmentRequest("RECOVER", "paper.pdf"))
    assert first.pdf_path
    Path(first.pdf_path).write_bytes(b"corrupt cache")

    repaired = resolver.resolve(
        AttachmentRequest("RECOVER", "paper.pdf"),
        previous=AttachmentCacheState(
            archive_sha256=first.archive_sha256,
            pdf_sha256=first.pdf_sha256,
            pdf_path=first.pdf_path,
            api_filename="paper.pdf",
        ),
    )
    assert repaired.ready and not repaired.reused
    assert Path(repaired.pdf_path or "").read_bytes() == PDF

    archive.write_bytes(b"not a zip")
    failed = resolver.resolve(AttachmentRequest("RECOVER", "paper.pdf"))
    assert failed.status is AttachmentStatus.INVALID_ARCHIVE
    assert failed.pdf_path is None
    assert Path(repaired.pdf_path or "").read_bytes() == PDF


def test_changed_api_filename_never_reuses_a_different_selected_pdf(tmp_path: Path) -> None:
    webdav = tmp_path / "webdav"
    extracted = tmp_path / "extracted"
    webdav.mkdir()
    _write_archive(webdav, "SELECT", {"a.pdf": b"%PDF-a", "b.pdf": b"%PDF-b"})
    resolver = _resolver(webdav, extracted)
    first = resolver.resolve(AttachmentRequest("SELECT", "a.pdf"))
    assert first.ready and first.pdf_path

    second = resolver.resolve(
        AttachmentRequest("SELECT", "b.pdf"),
        previous=AttachmentCacheState(
            archive_sha256=first.archive_sha256,
            pdf_sha256=first.pdf_sha256,
            pdf_path=first.pdf_path,
            api_filename="a.pdf",
        ),
    )

    assert second.ready and not second.reused
    assert Path(second.pdf_path or "").name == "b.pdf"
    assert Path(second.pdf_path or "").read_bytes() == b"%PDF-b"


def test_mapping_validation_is_sorted_bounded_and_requires_all_samples(
    tmp_path: Path,
) -> None:
    webdav = tmp_path / "webdav"
    webdav.mkdir()
    _write_archive(webdav, "A", {"a.pdf": PDF})
    _write_archive(webdav, "B", {"one.pdf": PDF, "two.pdf": PDF})
    _write_archive(webdav, "C", {"c.pdf": PDF})
    requests = [
        AttachmentRequest("C", "c.pdf"),
        AttachmentRequest("B", "absent.pdf"),
        AttachmentRequest("A", "a.pdf"),
    ]

    first_only = validate_attachment_mapping(webdav, requests, sample_size=1)
    first_two = validate_attachment_mapping(webdav, requests, sample_size=2)

    assert first_only.verified
    assert [sample.attachment_key for sample in first_only.samples] == ["A"]
    assert not first_two.verified
    assert (first_two.sampled, first_two.passed, first_two.failed) == (2, 1, 1)


def test_zero_mapping_samples_is_unverified(tmp_path: Path) -> None:
    webdav = tmp_path / "webdav"
    webdav.mkdir()

    result = validate_attachment_mapping(webdav, [AttachmentRequest("MISSING", "paper.pdf")])

    assert not result.verified
    assert result.sampled == 0


def test_staged_resolution_reports_final_pdf_without_publishing_early(
    tmp_path: Path,
) -> None:
    webdav = tmp_path / "webdav"
    extracted = tmp_path / "extracted"
    staging = tmp_path / "staging"
    webdav.mkdir()
    new_pdf = PDF + b"new revision\n"
    _write_archive(webdav, "STAGED", {"folder/paper.pdf": new_pdf})

    target = extracted / "STAGED"
    target.mkdir(parents=True)
    final_pdf = target / "paper.pdf"
    final_pdf.write_bytes(b"old revision")

    resolver = _resolver(webdav, extracted, staging_root=staging)
    result = resolver.resolve(AttachmentRequest("STAGED", "paper.pdf"))

    assert result.ready
    assert result.pdf_size == len(new_pdf)
    assert result.pdf_path == str(final_pdf)
    assert final_pdf.read_bytes() == b"old revision"
    assert len(resolver.staged_attachments) == 1
    publication = resolver.staged_attachments[0]
    assert publication.target == target
    assert publication.staged.parent == staging
    assert (publication.staged / "paper.pdf").read_bytes() == new_pdf
