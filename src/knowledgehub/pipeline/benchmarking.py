"""Bounded GPU memory sampling used only by explicit benchmarks."""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any

from knowledgehub.pipeline.config import inspect_gpu_devices


@dataclass(slots=True)
class GPUMemoryMonitor:
    interval_seconds: float = 0.25
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)
    _peak_used_mb: dict[str, int] = field(default_factory=dict, init=False)
    _devices: dict[str, dict[str, Any]] = field(default_factory=dict, init=False)

    def start(self) -> None:
        self._sample()
        self._thread = threading.Thread(target=self._run, name="gpu-memory-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self.interval_seconds * 4))
        self._sample()
        return {
            "devices": [self._devices[key] for key in sorted(self._devices)],
            "peak_used_memory_mb": dict(sorted(self._peak_used_mb.items())),
        }

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self._sample()

    def _sample(self) -> None:
        try:
            devices = inspect_gpu_devices()
        except (OSError, ValueError, subprocess.SubprocessError):
            return
        for device in devices:
            used = max(0, device.total_memory_mb - device.free_memory_mb)
            self._peak_used_mb[device.uuid] = max(self._peak_used_mb.get(device.uuid, 0), used)
            self._devices[device.uuid] = {
                "logical_id": device.logical_id,
                "name": device.name,
                "pci_bus_id": device.pci_bus_id,
                "total_memory_mb": device.total_memory_mb,
                "uuid": device.uuid,
            }
