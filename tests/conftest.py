from __future__ import annotations

import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from knowledgehub.sources.zotero.config import SecretValue, ZoteroConfig  # noqa: E402


@dataclass
class FakeClock:
    now: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now

    def sleep(self, delay: float) -> None:
        assert delay >= 0
        self.sleeps.append(delay)
        self.now += delay


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def zotero_config_factory(tmp_path: Path) -> Callable[..., ZoteroConfig]:
    webdav = tmp_path / "webdav"
    webdav.mkdir()
    base = ZoteroConfig(
        api_key=SecretValue("test-api-key"),
        library_type="user",
        library_id=42,
        webdav_dir=webdav,
        data_dir=tmp_path / "data",
        max_retries=2,
        api_concurrency=1,
        zip_stability_interval_seconds=0,
    )

    def factory(**overrides: Any) -> ZoteroConfig:
        return replace(base, **overrides)

    return factory
