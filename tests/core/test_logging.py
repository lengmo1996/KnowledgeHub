from __future__ import annotations

import logging

from knowledgehub.core.logging import REDACTED, configure_logging, redact_text


def test_redact_text_covers_known_secrets_headers_assignments_and_query_values() -> None:
    secret = "top-secret-token"
    original = (
        f"known={secret} Zotero-API-Key: header-token "
        "authorization=Bearer bearer-token https://example.test/?key=query-token"
    )

    redacted = redact_text(original, (secret,))

    for value in (secret, "header-token", "bearer-token", "query-token"):
        assert value not in redacted
    assert redacted.count(REDACTED) == 4


def test_configured_handlers_redact_messages_arguments_exceptions_and_log_file(
    tmp_path, capsys
) -> None:
    secret = "never-log-this"
    logger = configure_logging(
        logger_name="knowledgehub.test.redaction",
        data_dir=tmp_path,
        secrets=(secret,),
    )

    logger.warning("credential=%s", {"api_key": secret})
    try:
        raise RuntimeError(f"transport failed for {secret}")
    except RuntimeError:
        logger.exception("request with Zotero-API-Key: %s failed", secret)

    for handler in logger.handlers:
        handler.flush()

    stderr = capsys.readouterr().err
    log_text = (tmp_path / "logs" / "knowledgehub.log").read_text(encoding="utf-8")
    for output in (stderr, log_text):
        assert secret not in output
        assert REDACTED in output

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logging.Logger.manager.loggerDict.pop(logger.name, None)
