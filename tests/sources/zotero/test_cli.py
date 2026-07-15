from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowledgehub.cli.main import build_parser, main
from knowledgehub.core.locking import LockBusyError
from knowledgehub.sources.zotero.models import SyncMode, SyncSummary, ZoteroError
from knowledgehub.sources.zotero.validation import ValidationReport


def _patch_config(monkeypatch, config) -> None:
    import knowledgehub.sources.zotero.cli as cli

    monkeypatch.setattr(cli, "_load_config", lambda _explicit: config)
    monkeypatch.setattr(cli, "configure_logging", lambda **_kwargs: None)


def test_parser_exposes_all_zotero_commands_and_sync_default() -> None:
    parser = build_parser()

    sync = parser.parse_args(["zotero", "sync"])
    commands = [
        parser.parse_args(["zotero", command]).zotero_command
        for command in (
            "resolve-attachments",
            "refresh-cache",
            "validate",
            "doctor",
            "status",
            "rebuild",
        )
    ]

    assert sync.source == "zotero"
    assert sync.zotero_command == "sync"
    assert sync.once is False
    assert sync.full is False
    assert commands == [
        "resolve-attachments",
        "refresh-cache",
        "validate",
        "doctor",
        "status",
        "rebuild",
    ]
    with pytest.raises(SystemExit):
        parser.parse_args(["zotero", "sync", "--once", "--full"])


def test_sync_cli_emits_json_and_selects_full_mode(
    monkeypatch,
    capsys,
    zotero_config_factory,
) -> None:
    import knowledgehub.sources.zotero.cli as cli

    config = zotero_config_factory()
    _patch_config(monkeypatch, config)
    seen: dict[str, object] = {}

    def fake_sync(received, *, mode):
        seen.update(config=received, mode=mode)
        return SyncSummary(
            sync_id="sync-1",
            mode=mode.value,
            status="success",
            committed_version=7,
        )

    monkeypatch.setattr(cli, "sync_once", fake_sync)

    exit_code = main(["zotero", "sync", "--full"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert seen == {"config": config, "mode": SyncMode.FULL}
    assert output["status"] == "success"
    assert output["committed_version"] == 7


def test_resolve_attachments_cli_dispatches_local_operation(
    monkeypatch,
    capsys,
    zotero_config_factory,
) -> None:
    import knowledgehub.sources.zotero.cli as cli

    config = zotero_config_factory()
    _patch_config(monkeypatch, config)
    seen: dict[str, object] = {}

    def fake_resolve(received, *, limit=None, attachment_keys=None):
        seen.update(limit=limit, attachment_keys=attachment_keys)
        return SyncSummary(
            sync_id="local",
            mode="attachments",
            status="success" if received is config else "failed",
        )

    monkeypatch.setattr(cli, "resolve_attachments_once", fake_resolve)

    assert (
        main(
            [
                "zotero",
                "resolve-attachments",
                "--limit",
                "20",
                "--attachment-key",
                "ABCD1234",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["mode"] == "attachments"
    assert seen == {"limit": 20, "attachment_keys": ["ABCD1234"]}


def test_refresh_cache_cli_dispatches_and_supports_local_adoption(
    monkeypatch,
    capsys,
    zotero_config_factory,
) -> None:
    import knowledgehub.sources.zotero.cli as cli

    config = zotero_config_factory()
    _patch_config(monkeypatch, config)
    seen: dict[str, object] = {}

    class Summary:
        def to_dict(self):
            return {"status": "success", "downloaded": 2}

    def fake_refresh(received, *, prune, adopt_existing):
        seen.update(config=received, prune=prune, adopt_existing=adopt_existing)
        return Summary()

    monkeypatch.setattr(cli, "refresh_webdav_cache", fake_refresh)

    assert main(["zotero", "refresh-cache", "--no-prune", "--adopt-existing"]) == 0
    assert json.loads(capsys.readouterr().out)["downloaded"] == 2
    assert seen == {"config": config, "prune": False, "adopt_existing": True}


def test_refresh_cache_cli_honors_configured_seed_policy(
    monkeypatch,
    capsys,
    zotero_config_factory,
) -> None:
    import knowledgehub.sources.zotero.cli as cli

    config = zotero_config_factory(webdav_adopt_existing=True, webdav_prune=False)
    _patch_config(monkeypatch, config)
    seen: dict[str, object] = {}

    class Summary:
        def to_dict(self):
            return {"status": "success"}

    def fake_refresh(received, *, prune, adopt_existing):
        seen.update(config=received, prune=prune, adopt_existing=adopt_existing)
        return Summary()

    monkeypatch.setattr(cli, "refresh_webdav_cache", fake_refresh)

    assert main(["zotero", "refresh-cache"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "success"
    assert seen == {"config": config, "prune": False, "adopt_existing": True}


def test_validate_cli_uses_report_validity_as_exit_status(
    monkeypatch,
    capsys,
    zotero_config_factory,
) -> None:
    import knowledgehub.sources.zotero.cli as cli

    config = zotero_config_factory()
    _patch_config(monkeypatch, config)
    report = ValidationReport()
    report.add("error", "broken", "broken", "repair")
    monkeypatch.setattr(cli, "validate_source", lambda _config: report)

    assert main(["zotero", "validate"]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["valid"] is False
    assert output["issues"][0]["code"] == "broken"


@pytest.mark.parametrize(
    ("error", "expected_exit"),
    [
        (ZoteroError("config_error", "bad config"), 2),
        (ZoteroError("network_error", "offline", retryable=True), 1),
    ],
)
def test_classified_errors_are_sanitized_json(
    monkeypatch,
    capsys,
    error: ZoteroError,
    expected_exit: int,
) -> None:
    import knowledgehub.sources.zotero.cli as cli

    monkeypatch.setattr(cli, "_load_config", lambda _explicit: (_ for _ in ()).throw(error))

    assert main(["zotero", "status"]) == expected_exit
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "error"
    assert output["error_code"] == error.code
    assert output["message"] == str(error)


def test_lock_busy_has_dedicated_exit_code(
    monkeypatch,
    capsys,
    zotero_config_factory,
) -> None:
    import knowledgehub.sources.zotero.cli as cli

    config = zotero_config_factory()
    _patch_config(monkeypatch, config)
    monkeypatch.setattr(
        cli,
        "sync_once",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(LockBusyError(Path("lock"))),
    )

    assert main(["zotero", "sync"]) == 3
    assert json.loads(capsys.readouterr().out)["error_code"] == "lock_busy"


def test_status_before_initialization_is_read_only(zotero_config_factory) -> None:
    from knowledgehub.sources.zotero.cli import _status

    config = zotero_config_factory()

    result = _status(config, 10)

    assert result == {
        "initialized": False,
        "state_database": str(config.data_dir / "state" / "zotero.sqlite3"),
        "recent_runs": [],
    }
    assert not config.data_dir.exists()


def test_status_rejects_symlinked_state_database(
    tmp_path: Path,
    zotero_config_factory,
) -> None:
    from knowledgehub.sources.zotero.cli import _status
    from knowledgehub.sources.zotero.state import ZoteroStateStore

    external = ZoteroStateStore(tmp_path / "external")
    external.initialize()
    config = zotero_config_factory(data_dir=tmp_path / "linked")
    (config.data_dir / "state").mkdir(parents=True)
    (config.data_dir / "state" / "zotero.sqlite3").symlink_to(external.path)

    with pytest.raises(ZoteroError, match="non-symlink") as error:
        _status(config, 10)

    assert error.value.code == "state_error"


def test_rebuild_defaults_to_dry_run(
    monkeypatch,
    capsys,
    zotero_config_factory,
) -> None:
    import knowledgehub.sources.zotero.cli as cli

    config = zotero_config_factory()
    _patch_config(monkeypatch, config)
    monkeypatch.setattr(
        cli,
        "rebuild_source",
        lambda received, *, confirmed: {
            "same_config": received is config,
            "confirmed": confirmed,
        },
    )

    assert main(["zotero", "rebuild"]) == 0
    assert json.loads(capsys.readouterr().out) == {"confirmed": False, "same_config": True}


def test_watch_rejects_non_positive_interval(zotero_config_factory) -> None:
    from knowledgehub.sources.zotero.cli import _watch

    with pytest.raises(ZoteroError, match="must be positive") as error:
        _watch(zotero_config_factory(), 0)
    assert error.value.code == "config_error"


def test_watch_cli_does_not_replace_explicit_zero_with_default(
    monkeypatch,
    capsys,
    zotero_config_factory,
) -> None:
    config = zotero_config_factory(poll_interval_seconds=300)
    _patch_config(monkeypatch, config)

    assert main(["zotero", "watch", "--interval", "0"]) == 2

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "error"
    assert output["error_code"] == "config_error"
    assert output["message"] == "Watch interval must be positive"
