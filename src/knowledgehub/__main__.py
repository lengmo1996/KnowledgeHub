"""Allow ``python -m knowledgehub`` to use the normal CLI entry point."""

from __future__ import annotations

from knowledgehub.cli.main import main

if __name__ == "__main__":  # pragma: no cover - exercised through a subprocess
    raise SystemExit(main())
