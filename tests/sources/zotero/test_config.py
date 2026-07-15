from __future__ import annotations

from pathlib import Path

import pytest

from knowledgehub.sources.zotero.config import SecretValue, ZoteroConfig
from knowledgehub.sources.zotero.models import ZoteroError


def _write(path: Path, value: str) -> Path:
    path.write_text(value, encoding="utf-8")
    return path


def test_load_merges_default_explicit_and_environment_in_order(tmp_path: Path) -> None:
    default = _write(
        tmp_path / "default.yaml",
        """
sources:
  zotero:
    api_key: default-secret
    library_type: user
    library_id: 1
    api_concurrency: 1
    poll_interval_seconds: 60
""",
    )
    explicit = _write(
        tmp_path / "zotero.yaml",
        """
library_id: 2
api_concurrency: 3
poll_interval_seconds: 120
attachment_scan_on_304: false
""",
    )

    config = ZoteroConfig.load(
        explicit,
        default_path=default,
        environ={
            "ZOTERO_API_KEY": "environment-secret",
            "ZOTERO_LIBRARY_ID": "3",
            "ZOTERO_API_CONCURRENCY": "4",
            "ZOTERO_ATTACHMENT_SCAN_ON_304": "yes",
        },
    )

    assert config.library_id == 3
    assert config.api_concurrency == 4
    assert config.poll_interval_seconds == 120
    assert config.attachment_scan_on_304 is True
    assert config.api_key.get_secret_value() == "environment-secret"


def test_secret_never_appears_in_string_or_repr() -> None:
    secret = SecretValue("do-not-leak")

    assert secret.get_secret_value() == "do-not-leak"
    assert "do-not-leak" not in str(secret)
    assert "do-not-leak" not in repr(secret)
    assert bool(secret)
    assert not bool(SecretValue(""))


@pytest.mark.parametrize(
    ("environment", "field", "expected"),
    [
        ({"ZOTERO_MAX_RETRIES": "7"}, "max_retries", 7),
        ({"ZOTERO_HTTP_TIMEOUT_SECONDS": "2.5"}, "http_timeout_seconds", 2.5),
        (
            {"ZOTERO_WEBDAV_REQUEST_INTERVAL_SECONDS": "0.75"},
            "webdav_request_interval_seconds",
            0.75,
        ),
        (
            {"ZOTERO_WEBDAV_RETRY_COOLDOWN_SECONDS": "900"},
            "webdav_retry_cooldown_seconds",
            900.0,
        ),
        (
            {"ZOTERO_WEBDAV_MAX_RETRY_DELAY_SECONDS": "1800"},
            "webdav_max_retry_delay_seconds",
            1800.0,
        ),
        ({"ZOTERO_WEBDAV_ADOPT_EXISTING": "true"}, "webdav_adopt_existing", True),
        ({"ZOTERO_WEBDAV_PRUNE": "false"}, "webdav_prune", False),
        ({"ZOTERO_ENABLE_STREAMING": "off"}, "enable_streaming", False),
        (
            {"ZOTERO_METADATA_CHANGES_REQUIRE_CHUNKING": "1"},
            "metadata_changes_require_chunking",
            True,
        ),
        ({"ZOTERO_WEBDAV_DIR": "~/zotero"}, "webdav_dir", Path("~/zotero").expanduser()),
        ({"ZOTERO_WEBDAV_PAGE_LIMIT": "42"}, "webdav_page_limit", 42),
    ],
)
def test_environment_values_are_typed(
    tmp_path: Path,
    environment: dict[str, str],
    field: str,
    expected: object,
) -> None:
    values = {"ZOTERO_API_KEY": "secret", **environment}
    config = ZoteroConfig.load(environ=values)
    assert getattr(config, field) == expected


@pytest.mark.parametrize(
    ("yaml_value", "code", "message"),
    [
        ("library_type: group", "config_error", "required for group"),
        ("library_type: organization", "unsupported_library_type", "Unsupported library type"),
        ("library_id: 0", "config_error", "positive integer"),
        ("api_base_url: http://api.zotero.org", "config_error", "HTTPS origin"),
        ("api_base_url: https://user:pass@api.zotero.org", "config_error", "HTTPS origin"),
        ("api_base_url: https://api.zotero.org:99999", "config_error", "valid HTTPS origin"),
        ("http_timeout_seconds: .nan", "config_error", "timeout must be positive"),
        ("zip_stability_interval_seconds: .inf", "config_error", "non-negative"),
        ("api_concurrency: 5", "config_error", "between 1 and 4"),
        ("max_retries: -1", "config_error", "cannot be negative"),
        ("zip_stability_check_count: 1", "config_error", "at least two"),
        ("poll_interval_seconds: 0", "config_error", "must be positive"),
        ("enable_streaming: true", "streaming_not_implemented", "not implemented"),
        ("webdav_url: http://dav.example/zotero/", "config_error", "HTTPS collection"),
        ("webdav_url: https://dav.example/zotero", "config_error", "ending in /"),
        ("webdav_page_limit: 0", "config_error", "PAGE_LIMIT must be positive"),
        (
            "webdav_request_interval_seconds: -.inf",
            "config_error",
            "REQUEST_INTERVAL_SECONDS must be non-negative",
        ),
        (
            "webdav_request_interval_seconds: -0.1",
            "config_error",
            "REQUEST_INTERVAL_SECONDS must be non-negative",
        ),
        (
            "webdav_retry_cooldown_seconds: -0.1",
            "config_error",
            "RETRY_COOLDOWN_SECONDS must be non-negative",
        ),
        (
            "webdav_max_retry_delay_seconds: -0.1",
            "config_error",
            "MAX_RETRY_DELAY_SECONDS must be non-negative",
        ),
        (
            "webdav_retry_cooldown_seconds: 61\nwebdav_max_retry_delay_seconds: 60",
            "config_error",
            "RETRY_COOLDOWN_SECONDS must not exceed",
        ),
    ],
)
def test_static_validation_rejects_unsafe_or_unsupported_values(
    tmp_path: Path,
    yaml_value: str,
    code: str,
    message: str,
) -> None:
    path = _write(tmp_path / "zotero.yaml", f"api_key: secret\n{yaml_value}\n")

    with pytest.raises(ZoteroError, match=message) as error:
        ZoteroConfig.load(path, environ={})

    assert error.value.code == code


@pytest.mark.parametrize(
    ("yaml_value", "message"),
    [
        ("api_concurrency: many", "must be an integer"),
        ("http_timeout_seconds: soon", "must be numeric"),
        ("attachment_scan_on_304: perhaps", "must be a boolean"),
        ("webdav_adopt_existing: perhaps", "must be a boolean"),
        ("webdav_prune: perhaps", "must be a boolean"),
    ],
)
def test_conversion_errors_are_classified(
    tmp_path: Path,
    yaml_value: str,
    message: str,
) -> None:
    path = _write(tmp_path / "zotero.yaml", f"api_key: secret\n{yaml_value}\n")

    with pytest.raises(ZoteroError, match=message) as error:
        ZoteroConfig.load(path, environ={})

    assert error.value.code == "config_error"


def test_unknown_key_and_invalid_yaml_are_rejected(tmp_path: Path) -> None:
    unknown = _write(tmp_path / "unknown.yaml", "api_key: secret\nmade_up: true\n")
    malformed = _write(tmp_path / "malformed.yaml", "zotero: [\n")

    with pytest.raises(ZoteroError, match="Unknown Zotero configuration keys"):
        ZoteroConfig.load(unknown, environ={})
    with pytest.raises(ZoteroError, match="Cannot load YAML"):
        ZoteroConfig.load(malformed, environ={"ZOTERO_API_KEY": "secret"})

    non_utf8 = tmp_path / "non-utf8.yaml"
    non_utf8.write_bytes(b"api_key: \xff\n")
    with pytest.raises(ZoteroError, match="Cannot load YAML") as error:
        ZoteroConfig.load(non_utf8, environ={})
    assert error.value.code == "config_error"


def test_missing_file_and_missing_api_key_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ZoteroError, match="does not exist"):
        ZoteroConfig.load(tmp_path / "absent.yaml", environ={"ZOTERO_API_KEY": "secret"})
    with pytest.raises(ZoteroError, match="ZOTERO_API_KEY is required"):
        ZoteroConfig.load(environ={})


def test_prepare_runtime_creates_only_data_tree(
    tmp_path: Path,
    zotero_config_factory,
) -> None:
    config = zotero_config_factory()
    source_before = set(config.webdav_dir.iterdir())

    config.prepare_runtime()

    expected = {
        "state",
        "raw/items",
        "raw/collections",
        "raw/deleted",
        "extracted",
        "manifests/deltas",
        "runs",
        "logs",
        ".staging",
        ".rebuild",
    }
    assert all((config.data_dir / relative).is_dir() for relative in expected)
    assert set(config.webdav_dir.iterdir()) == source_before


def test_prepare_runtime_rejects_unreadable_source_and_data_inside_source(
    tmp_path: Path,
    zotero_config_factory,
) -> None:
    absent = zotero_config_factory(webdav_dir=tmp_path / "absent")
    with pytest.raises(ZoteroError, match="WebDAV directory is not readable"):
        absent.prepare_runtime()

    webdav = tmp_path / "source"
    webdav.mkdir()
    nested = zotero_config_factory(webdav_dir=webdav, data_dir=webdav / "generated")
    with pytest.raises(ZoteroError, match="must not be inside"):
        nested.prepare_runtime()
    assert not nested.data_dir.exists()


def test_prepare_runtime_classifies_webdav_io_error(
    monkeypatch,
    zotero_config_factory,
) -> None:
    config = zotero_config_factory()

    def fail_is_dir(_path: Path) -> bool:
        raise OSError(5, "mount unavailable")

    monkeypatch.setattr(Path, "is_dir", fail_is_dir)
    with pytest.raises(ZoteroError, match="cannot be accessed") as error:
        config.prepare_runtime()
    assert error.value.code == "config_error"


def test_user_library_id_can_be_resolved_without_mutating_original() -> None:
    config = ZoteroConfig(api_key=SecretValue("secret"), library_id=None)

    resolved = config.with_library_id(123)

    assert config.library_id is None
    assert resolved.library_id == 123


def test_webdav_credentials_are_typed_as_redacted_secrets() -> None:
    config = ZoteroConfig.load(
        environ={
            "ZOTERO_API_KEY": "api-secret",
            "ZOTERO_WEBDAV_USERNAME": "person@example.com",
            "ZOTERO_WEBDAV_PASSWORD": "application-password",
        }
    )

    assert config.webdav_username.get_secret_value() == "person@example.com"
    assert config.webdav_password.get_secret_value() == "application-password"
    assert "application-password" not in repr(config.webdav_password)
