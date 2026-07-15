"""Argument parsing and service dispatch for the Zotero source."""

from __future__ import annotations

import argparse
import logging
import signal
import threading
import time
from pathlib import Path
from typing import Any

from knowledgehub.core.hashing import canonical_json_dumps
from knowledgehub.core.locking import LockBusyError
from knowledgehub.core.logging import configure_logging

from .client import ZoteroClient
from .config import ZoteroConfig
from .models import SyncMode, ZoteroError
from .rebuild import rebuild_source
from .state import ZoteroStateStore
from .sync import resolve_attachments_once, sync_once
from .validation import validate_source
from .webdav_cache import refresh_webdav_cache

LOGGER = logging.getLogger(__name__)


def add_zotero_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("zotero", help="Zotero source and paginated WebDAV cache")
    commands = parser.add_subparsers(dest="zotero_command", required=True)

    sync_parser = commands.add_parser("sync", help="Synchronize Zotero metadata and attachments")
    mode = sync_parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Run one incremental sync (default)")
    mode.add_argument(
        "--full", action="store_true", help="Fetch complete remote object lists from version 0"
    )

    watch = commands.add_parser("watch", help="Poll by repeatedly calling the same sync_once()")
    watch.add_argument("--interval", type=int, help="Polling interval in seconds")

    resolve = commands.add_parser(
        "resolve-attachments", help="Rescan WebDAV archives without changing library_version"
    )
    resolve.add_argument("--limit", type=int, help="Resolve at most this many eligible PDFs")
    resolve.add_argument(
        "--attachment-key",
        action="append",
        dest="attachment_keys",
        help="Resolve only this attachment key (repeatable)",
    )
    commands.add_parser("validate", help="Validate SQLite, relationships, cache and manifests")
    refresh = commands.add_parser(
        "refresh-cache", help="Refresh the local mirror through Nutstore WebDAV pagination"
    )
    refresh.add_argument(
        "--no-prune",
        action="store_true",
        help="Keep supported local ZIP/PROP files that are absent from the remote listing",
    )
    refresh.add_argument(
        "--adopt-existing",
        action="store_true",
        help="Trust unindexed local ZIP/PROP files whose size matches the remote listing",
    )
    commands.add_parser("doctor", help="Check configuration, paths, API key and library permission")
    status = commands.add_parser("status", help="Show local source state and recent runs")
    status.add_argument("--runs", type=int, default=10, help="Number of recent runs to show")
    rebuild = commands.add_parser("rebuild", help="Safely rebuild state from version 0")
    rebuild.add_argument(
        "--yes", action="store_true", help="Promote a validated rebuild; default is dry-run"
    )


def run_zotero_command(args: argparse.Namespace) -> int:
    try:
        config = _load_config(args.config)
        configure_logging(
            level=config.log_level,
            secrets=(
                config.api_key.get_secret_value(),
                config.webdav_username.get_secret_value(),
                config.webdav_password.get_secret_value(),
            ),
        )
        command = args.zotero_command
        needs_runtime = command in {
            "sync",
            "watch",
            "resolve-attachments",
            "validate",
            "doctor",
        } or (command == "rebuild" and args.yes)
        if needs_runtime:
            config.prepare_runtime()
            configure_logging(
                level=config.log_level,
                data_dir=config.data_dir,
                secrets=(
                    config.api_key.get_secret_value(),
                    config.webdav_username.get_secret_value(),
                    config.webdav_password.get_secret_value(),
                ),
            )
        if command == "sync":
            mode = SyncMode.FULL if args.full else SyncMode.INCREMENTAL
            _emit(sync_once(config, mode=mode).to_dict())
            return 0
        if command == "resolve-attachments":
            _emit(
                resolve_attachments_once(
                    config,
                    limit=args.limit,
                    attachment_keys=args.attachment_keys,
                ).to_dict()
            )
            return 0
        if command == "refresh-cache":
            _emit(
                refresh_webdav_cache(
                    config,
                    prune=config.webdav_prune and not args.no_prune,
                    adopt_existing=args.adopt_existing or config.webdav_adopt_existing,
                ).to_dict()
            )
            return 0
        if command == "watch":
            interval = args.interval if args.interval is not None else config.poll_interval_seconds
            return _watch(config, interval)
        if command == "validate":
            report = validate_source(config)
            _emit(report.to_dict())
            return 0 if report.valid else 1
        if command == "doctor":
            _emit(_doctor(config))
            return 0
        if command == "status":
            _emit(_status(config, max(1, args.runs)))
            return 0
        if command == "rebuild":
            _emit(rebuild_source(config, confirmed=args.yes))
            return 0
        raise ZoteroError("cli_error", f"Unsupported command: {command}")
    except LockBusyError as exc:
        _emit_error("lock_busy", str(exc))
        return 3
    except ZoteroError as exc:
        _emit_error(exc.code, str(exc), retryable=exc.retryable, context=exc.context)
        return (
            2
            if exc.code in {"config_error", "unsupported_library_type", "streaming_not_implemented"}
            else 1
        )
    except KeyboardInterrupt:
        _emit_error("interrupted", "Interrupted")
        return 130
    except Exception as exc:  # pragma: no cover - last-resort command boundary
        LOGGER.exception("unhandled Zotero command failure")
        _emit_error("unexpected_error", str(exc))
        return 1


def _load_config(explicit: Path | None) -> ZoteroConfig:
    cwd = Path.cwd()
    default_path = cwd / "configs" / "default.yaml"
    source_path = explicit
    if source_path is None:
        candidate = cwd / "configs" / "sources" / "zotero.yaml"
        source_path = candidate if candidate.is_file() else None
    return ZoteroConfig.load(
        source_path,
        default_path=default_path if default_path.is_file() else None,
    )


def _doctor(config: ZoteroConfig) -> dict[str, Any]:
    config.prepare_runtime()
    with ZoteroClient(config) as client:
        access = client.verify_key()
    return {
        "status": "ok",
        "library_type": access.library_type,
        "library_id": access.library_id,
        "key_user_id": access.user_id,
        "webdav_dir": str(config.webdav_dir.resolve(strict=True)),
        "data_dir": str(config.data_dir.resolve(strict=True)),
        "api_base_url": config.api_base_url,
        "read_only": True,
    }


def _status(config: ZoteroConfig, run_limit: int) -> dict[str, Any]:
    store = ZoteroStateStore(config.data_dir)
    if not store.path.exists() and not store.path.is_symlink():
        return {"initialized": False, "state_database": str(store.path), "recent_runs": []}
    with store.connect_readonly() as connection:
        state = store.library_state(connection)
        attachments = store.load_attachments(connection)
        documents = store.load_documents(include_deleted=True, connection=connection)
        recent_runs = store.recent_runs(run_limit, connection=connection)
    return {
        "initialized": state is not None,
        "library_state": state,
        "attachments": {
            "total": len(attachments),
            "by_status": _count_by(attachments, "resolver_status"),
        },
        "documents": {
            "current": sum(not value.get("deleted") for value in documents.values()),
            "tombstones": sum(bool(value.get("deleted")) for value in documents.values()),
        },
        "recent_runs": recent_runs,
    }


def _watch(config: ZoteroConfig, interval: int) -> int:
    if interval <= 0:
        raise ZoteroError("config_error", "Watch interval must be positive")
    stopped = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stopped.set()

    previous_handlers: dict[signal.Signals, Any] = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[sig] = signal.signal(sig, stop)
    failures = 0
    try:
        while not stopped.is_set():
            began = time.monotonic()
            try:
                result = sync_once(config)
                _emit(result.to_dict())
                failures = 0
            except (ZoteroError, LockBusyError) as exc:
                failures += 1
                LOGGER.error(
                    "watch sync failed",
                    extra={"retry_count": failures, "error_type": type(exc).__name__},
                )
            remaining = max(0.0, interval - (time.monotonic() - began))
            stopped.wait(remaining)
        return 0
    finally:
        for signal_number, handler in previous_handlers.items():
            signal.signal(signal_number, handler)


def _count_by(values: dict[str, dict[str, Any]], field: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values.values():
        key = str(value.get(field) or "unknown")
        result[key] = result.get(key, 0) + 1
    return dict(sorted(result.items()))


def _emit(value: Any) -> None:
    print(canonical_json_dumps(value))


def _emit_error(code: str, message: str, **extra: Any) -> None:
    _emit({"status": "error", "error_code": code, "message": message, **extra})
