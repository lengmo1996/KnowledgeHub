"""Application logging with defence-in-depth secret redaction."""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableSequence, Optional, Sequence, Union

LogLevel = Union[int, str]
REDACTED = "[REDACTED]"

_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:zotero[-_]?api[-_]?key|api[-_]?key|authorization)\b\s*[:=]\s*)"
    r"(?:(?:bearer|basic)\s+)?([^\s,;&]+)"
)
_SENSITIVE_QUERY = re.compile(r"(?i)([?&](?:api[-_]?key|key)=)([^&#\s]+)")


def _redact_known_secrets(text: str, secrets: Iterable[str]) -> str:
    redacted = text
    for secret in sorted({value for value in secrets if value}, key=len, reverse=True):
        redacted = redacted.replace(secret, REDACTED)
    return redacted


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Redact known secret values and common credential syntaxes."""

    redacted = _redact_known_secrets(text, secrets)
    redacted = _SENSITIVE_ASSIGNMENT.sub(rf"\1{REDACTED}", redacted)
    return _SENSITIVE_QUERY.sub(rf"\1{REDACTED}", redacted)


def _redact_value(value: Any, secrets: Sequence[str]) -> Any:
    if isinstance(value, str):
        # Keep %-style logging templates intact.  Generic credential-pattern
        # redaction is deliberately deferred until after interpolation in the
        # formatter; only literal configured secrets are safe to replace here.
        return _redact_known_secrets(value, secrets)
    if isinstance(value, Mapping):
        return {key: _redact_value(item, secrets) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_redact_value(item, secrets) for item in value)
    if isinstance(value, list):
        return [_redact_value(item, secrets) for item in value]
    return value


class SecretRedactionFilter(logging.Filter):
    """Sanitise messages and arguments before a handler formats a record."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets: MutableSequence[str] = list(
            dict.fromkeys(value for value in secrets if value)
        )

    @property
    def secrets(self) -> tuple[str, ...]:
        return tuple(self._secrets)

    def add_secret(self, value: str) -> None:
        if value and value not in self._secrets:
            self._secrets.append(value)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact_value(record.msg, self.secrets)
        record.args = _redact_value(record.args, self.secrets)
        if record.exc_text:
            record.exc_text = redact_text(record.exc_text, self.secrets)
        return True


class RedactingFormatter(logging.Formatter):
    """Redact the fully formatted line, including newly rendered exceptions."""

    def __init__(self, *args: Any, secrets: Iterable[str] = (), **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._secrets = tuple(value for value in secrets if value)

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record), self._secrets)


def configure_logging(
    *,
    level: LogLevel = "INFO",
    data_dir: Optional[Path] = None,
    secrets: Iterable[str] = (),
    logger_name: str = "knowledgehub",
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> logging.Logger:
    """Configure stderr and optional rotating data-directory logging.

    Calling this function repeatedly replaces only handlers installed by this
    function, avoiding duplicate output while preserving unrelated handlers.
    """

    logger = logging.getLogger(logger_name)
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        if getattr(handler, "_knowledgehub_handler", False):
            logger.removeHandler(handler)
            handler.close()

    secret_values = tuple(value for value in secrets if value)
    record_filter = SecretRedactionFilter(secret_values)
    formatter = RedactingFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        secrets=secret_values,
    )

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    stderr_handler.addFilter(record_filter)
    stderr_handler._knowledgehub_handler = True  # type: ignore[attr-defined]
    logger.addHandler(stderr_handler)

    if data_dir is not None:
        log_dir = Path(data_dir) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "knowledgehub.log",
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=True,
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(record_filter)
        file_handler._knowledgehub_handler = True  # type: ignore[attr-defined]
        logger.addHandler(file_handler)

    return logger
