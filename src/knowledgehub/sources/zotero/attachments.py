"""Read-only Zotero WebDAV archive discovery and safe PDF extraction.

The WebDAV tree is an input-only boundary.  This module opens archives and
sidecars without following symlinks and writes exclusively below the supplied
``extracted_root``.  It intentionally knows nothing about SQLite or manifests,
which makes attachment resolution usable by both normal sync and an explicit
attachment rescan.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import time
import uuid
import zipfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Mapping


class AttachmentStatus(str, Enum):
    """Stable wire values for attachment availability."""

    READY = "ready"
    MISSING_ARCHIVE = "missing_archive"
    AMBIGUOUS_ARCHIVE = "ambiguous_archive"
    UNSAFE_ARCHIVE = "unsafe_archive"
    UNSTABLE_ARCHIVE = "unstable_archive"
    INVALID_ARCHIVE = "invalid_archive"
    MISSING_PDF = "missing_pdf"
    # Backwards-compatible symbol; the wire value is the manifest contract.
    NO_PDF = "missing_pdf"
    AMBIGUOUS_ATTACHMENT = "ambiguous_attachment"
    EXTRACTION_ERROR = "extraction_error"


@dataclass(frozen=True, slots=True)
class AttachmentRequest:
    """The WebDAV lookup information obtained from a Zotero attachment item."""

    attachment_key: str
    filename: str | None = None


@dataclass(frozen=True, slots=True)
class AttachmentCacheState:
    """Previously committed cache values used to avoid redundant extraction."""

    archive_sha256: str | None = None
    pdf_sha256: str | None = None
    pdf_path: str | Path | None = None
    source_size: int | None = None
    source_mtime_ns: int | None = None
    api_filename: str | None = None


@dataclass(frozen=True, slots=True)
class ArchiveLocation:
    attachment_key: str
    archive_path: Path | None
    prop_path: Path | None
    problem: AttachmentStatus | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ArchiveSignature:
    archive_size: int
    archive_mtime_ns: int
    archive_inode: int
    prop_size: int
    prop_mtime_ns: int
    prop_inode: int


@dataclass(frozen=True, slots=True)
class ArchiveStability:
    stable: bool
    signature: ArchiveSignature | None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ZipInspection:
    selected_member: str | None
    pdf_members: tuple[str, ...]
    status: AttachmentStatus
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class AttachmentResolution:
    attachment_key: str
    status: AttachmentStatus
    status_detail: str | None = None
    archive_path: str | None = None
    selected_member: str | None = None
    pdf_path: str | None = None
    archive_sha256: str | None = None
    pdf_sha256: str | None = None
    pdf_size: int | None = None
    source_size: int | None = None
    source_mtime_ns: int | None = None
    prop_mtime_ns: int | None = None
    reused: bool = False

    @property
    def ready(self) -> bool:
        return self.status is AttachmentStatus.READY


@dataclass(frozen=True, slots=True)
class StagedAttachment:
    """A cache directory awaiting the sync-level recoverable publication."""

    staged: Path
    target: Path


@dataclass(frozen=True, slots=True)
class MappingSample:
    attachment_key: str
    passed: bool
    status: AttachmentStatus
    detail: str | None
    archive_path: str | None


@dataclass(frozen=True, slots=True)
class MappingValidation:
    verified: bool
    sampled: int
    passed: int
    failed: int
    webdav_realpath: str
    samples: tuple[MappingSample, ...]


Sleeper = Callable[[float], None]
_SAFE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


class _ArchiveProblem(Exception):
    def __init__(self, status: AttachmentStatus, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _json_detail(**values: object) -> str:
    return json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _regular_nofollow(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
    except OSError:
        return False
    return stat.S_ISREG(mode) and not stat.S_ISLNK(mode)


def _directory_nofollow(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
    except OSError:
        return False
    return stat.S_ISDIR(mode) and not stat.S_ISLNK(mode)


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _contains_symlink(path: Path, root: Path) -> bool:
    """Return true when a component below ``root`` is a symlink."""

    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current = current / part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                return True
        except OSError:
            return True
    return False


def _validate_key(key: str) -> None:
    if not key or not _SAFE_KEY.fullmatch(key) or key in {".", ".."}:
        raise ValueError(f"unsafe Zotero attachment key: {key!r}")


def locate_archive(webdav_root: str | Path, attachment_key: str) -> ArchiveLocation:
    """Locate a WebDAV archive without following symlinks.

    ``<root>/<key>.zip`` always wins.  The nested compatibility layout is
    considered only when that flat path does not exist at all.
    """

    _validate_key(attachment_key)
    root = Path(webdav_root).expanduser().resolve(strict=True)
    if not _directory_nofollow(root):
        return ArchiveLocation(
            attachment_key,
            None,
            None,
            AttachmentStatus.UNSAFE_ARCHIVE,
            "webdav root is not a real directory",
        )

    flat = root / f"{attachment_key}.zip"
    if _lexists(flat):
        if not _regular_nofollow(flat) or _contains_symlink(flat, root):
            return ArchiveLocation(
                attachment_key,
                flat,
                flat.with_suffix(".prop"),
                AttachmentStatus.UNSAFE_ARCHIVE,
                "flat archive is a symlink or not a regular file",
            )
        return _location_with_prop(root, attachment_key, flat)

    nested_dir = root / attachment_key
    if not _lexists(nested_dir):
        return ArchiveLocation(
            attachment_key,
            None,
            None,
            AttachmentStatus.MISSING_ARCHIVE,
            "archive not found",
        )
    if not _directory_nofollow(nested_dir) or _contains_symlink(nested_dir, root):
        return ArchiveLocation(
            attachment_key,
            None,
            None,
            AttachmentStatus.UNSAFE_ARCHIVE,
            "nested archive directory is a symlink or not a real directory",
        )

    try:
        zip_entries = sorted(
            (entry for entry in nested_dir.iterdir() if entry.suffix.lower() == ".zip"),
            key=lambda value: value.name,
        )
    except OSError as exc:
        return ArchiveLocation(
            attachment_key,
            None,
            None,
            AttachmentStatus.UNSAFE_ARCHIVE,
            f"cannot enumerate nested archive directory: {exc}",
        )
    if not zip_entries:
        return ArchiveLocation(
            attachment_key,
            None,
            None,
            AttachmentStatus.MISSING_ARCHIVE,
            "nested archive directory contains no ZIP",
        )
    if len(zip_entries) > 1:
        return ArchiveLocation(
            attachment_key,
            None,
            None,
            AttachmentStatus.AMBIGUOUS_ARCHIVE,
            _json_detail(candidates=[entry.name for entry in zip_entries]),
        )
    archive = zip_entries[0]
    if not _regular_nofollow(archive) or _contains_symlink(archive, root):
        return ArchiveLocation(
            attachment_key,
            archive,
            archive.with_suffix(".prop"),
            AttachmentStatus.UNSAFE_ARCHIVE,
            "nested archive is a symlink or not a regular file",
        )
    return _location_with_prop(root, attachment_key, archive)


def _location_with_prop(root: Path, key: str, archive: Path) -> ArchiveLocation:
    prop = archive.with_suffix(".prop")
    if not _lexists(prop):
        return ArchiveLocation(
            key,
            archive,
            prop,
            AttachmentStatus.UNSTABLE_ARCHIVE,
            "archive sidecar .prop is missing",
        )
    if not _regular_nofollow(prop) or _contains_symlink(prop, root):
        return ArchiveLocation(
            key,
            archive,
            prop,
            AttachmentStatus.UNSAFE_ARCHIVE,
            "archive sidecar .prop is a symlink or not a regular file",
        )
    return ArchiveLocation(key, archive, prop)


def _signature(location: ArchiveLocation) -> ArchiveSignature:
    if location.archive_path is None or location.prop_path is None:
        raise OSError("archive or sidecar path is absent")
    archive_stat = location.archive_path.stat(follow_symlinks=False)
    prop_stat = location.prop_path.stat(follow_symlinks=False)
    if not stat.S_ISREG(archive_stat.st_mode) or not stat.S_ISREG(prop_stat.st_mode):
        raise OSError("archive or sidecar ceased to be a regular file")
    return ArchiveSignature(
        archive_size=archive_stat.st_size,
        archive_mtime_ns=archive_stat.st_mtime_ns,
        archive_inode=archive_stat.st_ino,
        prop_size=prop_stat.st_size,
        prop_mtime_ns=prop_stat.st_mtime_ns,
        prop_inode=prop_stat.st_ino,
    )


def check_archives_stable(
    locations: Iterable[ArchiveLocation],
    *,
    observations: int = 2,
    interval: float = 0.25,
    sleeper: Sleeper = time.sleep,
) -> dict[str, ArchiveStability]:
    """Observe all candidate archives as a batch and compare stat signatures."""

    if observations < 2:
        raise ValueError("observations must be at least 2")
    if interval < 0:
        raise ValueError("interval must not be negative")
    candidates = {
        location.attachment_key: location for location in locations if location.problem is None
    }
    history: dict[str, list[ArchiveSignature | None]] = {key: [] for key in candidates}
    for observation in range(observations):
        for key, location in candidates.items():
            try:
                history[key].append(_signature(location))
            except OSError:
                history[key].append(None)
        if observation + 1 < observations:
            sleeper(interval)

    result: dict[str, ArchiveStability] = {}
    for key, signatures in history.items():
        first = signatures[0]
        stable = first is not None and all(value == first for value in signatures[1:])
        result[key] = ArchiveStability(
            stable=stable,
            signature=first if stable else None,
            detail=None if stable else "archive or sidecar changed during stability check",
        )
    return result


def _open_readonly(path: Path):  # type: ignore[no-untyped-def]
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    return os.fdopen(descriptor, "rb")


def _sha256_readonly(path: Path) -> str:
    digest = hashlib.sha256()
    with _open_readonly(path) as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_member(info: zipfile.ZipInfo) -> None:
    name = info.filename
    if not name or "\x00" in name:
        raise _ArchiveProblem(AttachmentStatus.UNSAFE_ARCHIVE, "ZIP contains an empty or NUL path")
    if "\\" in name:
        raise _ArchiveProblem(
            AttachmentStatus.UNSAFE_ARCHIVE,
            _json_detail(member=name, reason="backslash path is forbidden"),
        )
    if name.startswith("/") or name.startswith("//") or _WINDOWS_DRIVE.match(name):
        raise _ArchiveProblem(
            AttachmentStatus.UNSAFE_ARCHIVE,
            _json_detail(member=name, reason="absolute ZIP path is forbidden"),
        )
    pure = PurePosixPath(name)
    if pure.is_absolute() or any(part in {"..", ""} for part in pure.parts):
        raise _ArchiveProblem(
            AttachmentStatus.UNSAFE_ARCHIVE,
            _json_detail(member=name, reason="ZIP path traversal is forbidden"),
        )

    unix_mode = info.external_attr >> 16
    file_type = stat.S_IFMT(unix_mode)
    if stat.S_ISLNK(unix_mode):
        raise _ArchiveProblem(
            AttachmentStatus.UNSAFE_ARCHIVE,
            _json_detail(member=name, reason="ZIP symlink is forbidden"),
        )
    # Some ZIP creators record only permission bits (file_type == 0).
    if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise _ArchiveProblem(
            AttachmentStatus.UNSAFE_ARCHIVE,
            _json_detail(member=name, reason="ZIP special file is forbidden"),
        )


def _inspect_open(
    archive_stream,
    requested_filename: str | None,  # type: ignore[no-untyped-def]
) -> tuple[ZipInspection, zipfile.ZipInfo | None]:
    try:
        if not zipfile.is_zipfile(archive_stream):
            raise _ArchiveProblem(AttachmentStatus.INVALID_ARCHIVE, "not a ZIP archive")
        archive_stream.seek(0)
        with zipfile.ZipFile(archive_stream, "r") as archive:
            try:
                bad_member = archive.testzip()
            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                raise _ArchiveProblem(
                    AttachmentStatus.INVALID_ARCHIVE,
                    f"ZIP integrity check failed: {exc}",
                ) from exc
            if bad_member is not None:
                raise _ArchiveProblem(
                    AttachmentStatus.INVALID_ARCHIVE,
                    _json_detail(member=bad_member, reason="CRC check failed"),
                )
            infos = archive.infolist()
            for info in infos:
                _validate_member(info)
            pdf_infos = sorted(
                (
                    info
                    for info in infos
                    if not info.is_dir() and PurePosixPath(info.filename).suffix.lower() == ".pdf"
                ),
                key=lambda value: value.filename,
            )
            names = tuple(info.filename for info in pdf_infos)
            if not pdf_infos:
                return (
                    ZipInspection(None, (), AttachmentStatus.NO_PDF, "ZIP contains no PDF"),
                    None,
                )

            matches: list[zipfile.ZipInfo] = []
            if requested_filename:
                matches = [
                    info
                    for info in pdf_infos
                    if PurePosixPath(info.filename).name == requested_filename
                ]
            if len(matches) == 1:
                selected = matches[0]
            elif len(pdf_infos) == 1:
                selected = pdf_infos[0]
            else:
                return (
                    ZipInspection(
                        None,
                        names,
                        AttachmentStatus.AMBIGUOUS_ATTACHMENT,
                        _json_detail(candidates=list(names)),
                    ),
                    None,
                )
            return (
                ZipInspection(selected.filename, names, AttachmentStatus.READY),
                selected,
            )
    except _ArchiveProblem:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise _ArchiveProblem(
            AttachmentStatus.INVALID_ARCHIVE, f"cannot inspect ZIP: {exc}"
        ) from exc


def inspect_archive(
    archive_path: str | Path, requested_filename: str | None = None
) -> ZipInspection:
    """Validate a ZIP and deterministically choose its PDF, without extraction."""

    path = Path(archive_path)
    try:
        if not _regular_nofollow(path):
            raise _ArchiveProblem(
                AttachmentStatus.UNSAFE_ARCHIVE,
                "archive is a symlink or not a regular file",
            )
        with _open_readonly(path) as stream:
            inspection, _ = _inspect_open(stream, requested_filename)
            return inspection
    except _ArchiveProblem as exc:
        return ZipInspection(None, (), exc.status, exc.detail)
    except OSError as exc:
        return ZipInspection(None, (), AttachmentStatus.INVALID_ARCHIVE, f"cannot open ZIP: {exc}")


def validate_attachment_mapping(
    webdav_root: str | Path,
    requests: Iterable[AttachmentRequest],
    *,
    sample_size: int = 20,
) -> MappingValidation:
    """Validate the API-key-to-WebDAV mapping using deterministic samples.

    Requests are sorted by attachment key.  Missing or otherwise unavailable
    archives are not samples; up to ``sample_size`` locatable, sidecar-complete
    archives are inspected.  A sample passes only when the normal selection
    rule can unambiguously choose a PDF.  Verification requires at least one
    sample and every sampled archive to pass.
    """

    if sample_size < 1:
        raise ValueError("sample_size must be at least 1")
    root = Path(webdav_root).expanduser().resolve(strict=True)
    request_map: dict[str, AttachmentRequest] = {}
    for request in requests:
        _validate_key(request.attachment_key)
        if request.attachment_key in request_map:
            raise ValueError(f"duplicate attachment key: {request.attachment_key}")
        request_map[request.attachment_key] = request

    samples: list[MappingSample] = []
    for key in sorted(request_map):
        location = locate_archive(root, key)
        if location.problem is not None or location.archive_path is None:
            continue
        inspection = inspect_archive(location.archive_path, request_map[key].filename)
        passed = inspection.status is AttachmentStatus.READY
        samples.append(
            MappingSample(
                attachment_key=key,
                passed=passed,
                status=inspection.status,
                detail=inspection.detail,
                archive_path=str(location.archive_path),
            )
        )
        if len(samples) >= sample_size:
            break
    passed_count = sum(sample.passed for sample in samples)
    return MappingValidation(
        verified=bool(samples) and passed_count == len(samples),
        sampled=len(samples),
        passed=passed_count,
        failed=len(samples) - passed_count,
        webdav_realpath=str(root),
        samples=tuple(samples),
    )


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _safe_rmtree(path: Path, root: Path) -> None:
    if not _within(path, root) or path == root:
        raise RuntimeError(f"refusing to remove path outside extracted root: {path}")
    if _lexists(path):
        if path.is_symlink():
            path.unlink()
        else:
            shutil.rmtree(path)


class AttachmentResolver:
    """Resolve Zotero ZIP attachments into a managed PDF cache."""

    def __init__(
        self,
        webdav_root: str | Path,
        extracted_root: str | Path,
        *,
        stability_observations: int = 2,
        stability_interval: float = 0.25,
        sleeper: Sleeper = time.sleep,
        staging_root: str | Path | None = None,
    ) -> None:
        self.webdav_root = Path(webdav_root).expanduser().resolve(strict=True)
        if not _directory_nofollow(self.webdav_root):
            raise ValueError("webdav_root must be a real directory")
        extracted = Path(extracted_root).expanduser()
        extracted.mkdir(parents=True, exist_ok=True)
        self.extracted_root = extracted.resolve(strict=True)
        if not _directory_nofollow(self.extracted_root):
            raise ValueError("extracted_root must be a real directory")
        if (
            self.extracted_root == self.webdav_root
            or _within(self.extracted_root, self.webdav_root)
            or _within(self.webdav_root, self.extracted_root)
        ):
            raise ValueError("extracted_root and the read-only WebDAV root must not overlap")
        if staging_root is None:
            self.staging_root: Path | None = None
            self._work_root = self.extracted_root
        else:
            staging = Path(staging_root).expanduser()
            staging.mkdir(parents=True, exist_ok=True)
            self.staging_root = staging.resolve(strict=True)
            if not _directory_nofollow(self.staging_root):
                raise ValueError("staging_root must be a real directory")
            if (
                self.staging_root == self.webdav_root
                or _within(self.staging_root, self.webdav_root)
                or _within(self.webdav_root, self.staging_root)
                or self.staging_root == self.extracted_root
                or _within(self.staging_root, self.extracted_root)
                or _within(self.extracted_root, self.staging_root)
            ):
                raise ValueError("staging_root must not overlap source or extracted roots")
            self._work_root = self.staging_root
        self._staged_attachments: list[StagedAttachment] = []
        if stability_observations < 2:
            raise ValueError("stability_observations must be at least 2")
        if stability_interval < 0:
            raise ValueError("stability_interval must not be negative")
        self.stability_observations = stability_observations
        self.stability_interval = stability_interval
        self.sleeper = sleeper

    @property
    def staged_attachments(self) -> tuple[StagedAttachment, ...]:
        return tuple(self._staged_attachments)

    def resolve(
        self,
        request: AttachmentRequest,
        *,
        previous: AttachmentCacheState | None = None,
    ) -> AttachmentResolution:
        """Resolve one request; batch callers should prefer :meth:`resolve_many`."""

        return self.resolve_many(
            [request],
            previous={request.attachment_key: previous} if previous else None,
        )[request.attachment_key]

    def resolve_many(
        self,
        requests: Iterable[AttachmentRequest],
        *,
        previous: Mapping[str, AttachmentCacheState | None] | None = None,
    ) -> dict[str, AttachmentResolution]:
        """Locate and stability-check a batch before doing any extraction."""

        request_map: dict[str, AttachmentRequest] = {}
        for request in requests:
            _validate_key(request.attachment_key)
            if request.attachment_key in request_map:
                raise ValueError(f"duplicate attachment key: {request.attachment_key}")
            request_map[request.attachment_key] = request
        locations = {key: locate_archive(self.webdav_root, key) for key in request_map}
        stability = check_archives_stable(
            locations.values(),
            observations=self.stability_observations,
            interval=self.stability_interval,
            sleeper=self.sleeper,
        )
        results: dict[str, AttachmentResolution] = {}
        for key in sorted(request_map):
            location = locations[key]
            if location.problem is not None:
                results[key] = AttachmentResolution(
                    attachment_key=key,
                    status=location.problem,
                    status_detail=location.detail,
                    archive_path=(str(location.archive_path) if location.archive_path else None),
                )
                continue
            observed = stability[key]
            if not observed.stable or observed.signature is None:
                results[key] = AttachmentResolution(
                    attachment_key=key,
                    status=AttachmentStatus.UNSTABLE_ARCHIVE,
                    status_detail=observed.detail,
                    archive_path=str(location.archive_path),
                )
                continue
            results[key] = self._resolve_stable(
                request_map[key],
                location,
                observed.signature,
                previous.get(key) if previous else None,
            )
        return results

    def _resolve_stable(
        self,
        request: AttachmentRequest,
        location: ArchiveLocation,
        signature: ArchiveSignature,
        previous: AttachmentCacheState | None,
    ) -> AttachmentResolution:
        assert location.archive_path is not None
        archive = location.archive_path
        common = {
            "attachment_key": request.attachment_key,
            "archive_path": str(archive),
            "source_size": signature.archive_size,
            "source_mtime_ns": signature.archive_mtime_ns,
            "prop_mtime_ns": signature.prop_mtime_ns,
        }
        try:
            archive_hash = _sha256_readonly(archive)
        except OSError as exc:
            return AttachmentResolution(
                **common,
                status=AttachmentStatus.INVALID_ARCHIVE,
                status_detail=f"cannot hash archive: {exc}",
            )
        if not self._signature_unchanged(location, signature):
            return AttachmentResolution(
                **common,
                status=AttachmentStatus.UNSTABLE_ARCHIVE,
                status_detail="archive changed while hashing",
            )

        cached = self._reuse_cache(request, previous, archive_hash, common)
        if cached is not None:
            return cached

        temp = self._work_root / f".tmp-{request.attachment_key}-{uuid.uuid4().hex}"
        temp.mkdir(mode=0o700)
        try:
            with _open_readonly(archive) as stream:
                inspection, selected = _inspect_open(stream, request.filename)
                if inspection.status is not AttachmentStatus.READY or selected is None:
                    return AttachmentResolution(
                        **common,
                        status=inspection.status,
                        status_detail=inspection.detail,
                        selected_member=inspection.selected_member,
                        archive_sha256=archive_hash,
                    )
                destination = temp / PurePosixPath(selected.filename).name
                stream.seek(0)
                with zipfile.ZipFile(stream, "r") as zip_stream:
                    matching = [
                        info for info in zip_stream.infolist() if info.filename == selected.filename
                    ]
                    if len(matching) != 1:
                        raise _ArchiveProblem(
                            AttachmentStatus.AMBIGUOUS_ATTACHMENT,
                            _json_detail(candidates=[info.filename for info in matching]),
                        )
                    with (
                        zip_stream.open(matching[0], "r") as source,
                        destination.open("xb") as output,
                    ):
                        shutil.copyfileobj(source, output, length=1024 * 1024)
                        output.flush()
                        os.fsync(output.fileno())
            pdf_hash = _sha256_readonly(destination)
            pdf_size = destination.stat().st_size
            _fsync_directory(temp)
            if not self._signature_unchanged(location, signature):
                return AttachmentResolution(
                    **common,
                    status=AttachmentStatus.UNSTABLE_ARCHIVE,
                    status_detail="archive changed during extraction",
                    archive_sha256=archive_hash,
                )
            if self.staging_root is None:
                target = self._publish_temp(request.attachment_key, temp)
            else:
                staged = self._stage_temp(request.attachment_key, temp)
                target = self.extracted_root / request.attachment_key
                self._staged_attachments.append(StagedAttachment(staged=staged, target=target))
            temp = Path()  # mark ownership as transferred
            pdf_path = target / destination.name
            return AttachmentResolution(
                **common,
                status=AttachmentStatus.READY,
                selected_member=inspection.selected_member,
                pdf_path=str(pdf_path),
                archive_sha256=archive_hash,
                pdf_sha256=pdf_hash,
                pdf_size=pdf_size,
                reused=False,
            )
        except _ArchiveProblem as exc:
            return AttachmentResolution(
                **common,
                status=exc.status,
                status_detail=exc.detail,
                archive_sha256=archive_hash,
            )
        except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
            return AttachmentResolution(
                **common,
                status=AttachmentStatus.EXTRACTION_ERROR,
                status_detail=f"PDF extraction failed: {exc}",
                archive_sha256=archive_hash,
            )
        finally:
            if temp != Path() and _lexists(temp):
                _safe_rmtree(temp, self._work_root)

    def _signature_unchanged(self, location: ArchiveLocation, expected: ArchiveSignature) -> bool:
        try:
            return _signature(location) == expected
        except OSError:
            return False

    def _reuse_cache(
        self,
        request: AttachmentRequest,
        previous: AttachmentCacheState | None,
        archive_hash: str,
        common: Mapping[str, object],
    ) -> AttachmentResolution | None:
        if (
            previous is None
            or previous.archive_sha256 != archive_hash
            or not previous.pdf_sha256
            or previous.pdf_path is None
            or previous.api_filename != request.filename
        ):
            return None
        candidate = Path(previous.pdf_path).expanduser()
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            return None
        expected_dir = self.extracted_root / request.attachment_key
        if (
            not _within(resolved, expected_dir)
            or _contains_symlink(resolved, self.extracted_root)
            or not _regular_nofollow(resolved)
        ):
            return None
        try:
            current_hash = _sha256_readonly(resolved)
        except OSError:
            return None
        if current_hash != previous.pdf_sha256:
            return None
        return AttachmentResolution(
            **common,
            status=AttachmentStatus.READY,
            pdf_path=str(resolved),
            archive_sha256=archive_hash,
            pdf_sha256=current_hash,
            pdf_size=resolved.stat().st_size,
            reused=True,
        )

    def _publish_temp(self, key: str, temp: Path) -> Path:
        target = self.extracted_root / key
        backup = self.extracted_root / f".backup-{key}-{uuid.uuid4().hex}"
        if _lexists(target) and (target.is_symlink() or not _directory_nofollow(target)):
            raise OSError("existing extracted cache target is unsafe")
        moved_old = False
        try:
            if target.exists():
                os.replace(target, backup)
                moved_old = True
            os.replace(temp, target)
            _fsync_directory(self.extracted_root)
        except BaseException:
            if not target.exists() and moved_old and backup.exists():
                os.replace(backup, target)
                _fsync_directory(self.extracted_root)
            raise
        if moved_old and backup.exists():
            _safe_rmtree(backup, self.extracted_root)
            _fsync_directory(self.extracted_root)
        return target

    def _stage_temp(self, key: str, temp: Path) -> Path:
        assert self.staging_root is not None
        staged = self.staging_root / key
        if _lexists(staged):
            raise OSError(f"duplicate staged attachment target: {key}")
        os.replace(temp, staged)
        _fsync_directory(self.staging_root)
        return staged


__all__ = [
    "ArchiveLocation",
    "ArchiveSignature",
    "ArchiveStability",
    "AttachmentCacheState",
    "AttachmentRequest",
    "AttachmentResolution",
    "AttachmentResolver",
    "AttachmentStatus",
    "MappingSample",
    "MappingValidation",
    "StagedAttachment",
    "ZipInspection",
    "check_archives_stable",
    "inspect_archive",
    "locate_archive",
    "validate_attachment_mapping",
]
