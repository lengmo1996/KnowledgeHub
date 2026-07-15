import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_compose_restart_policy_keeps_gpu_workloads_manual() -> None:
    qdrant = yaml.safe_load(_read("deploy/qdrant/compose.yaml"))
    gpu = yaml.safe_load(_read("deploy/gpu/compose.yaml"))

    assert qdrant["services"]["qdrant"]["restart"] == "unless-stopped"
    for service in (
        "embedding-gpu0",
        "embedding-gpu1",
        "reranker-light-gpu1",
        "reranker-quality-gpu1",
    ):
        assert gpu["services"][service]["restart"] == "no"
    assert gpu["services"]["search-api"]["restart"] == "unless-stopped"
    assert "deploy" not in gpu["services"]["search-api"]
    quality_devices = gpu["services"]["reranker-quality-gpu1"]["deploy"]["resources"][
        "reservations"
    ]["devices"]
    assert quality_devices[0]["device_ids"] == ["1"]


def test_core_is_boot_installable_but_gpu_workloads_are_static() -> None:
    core = _read("deploy/systemd/knowledgehub-rag-core.service")
    online = _read("deploy/systemd/knowledgehub-rag-online.service")
    dual = _read("deploy/systemd/knowledgehub-rag-embed-dual.service")

    assert "\n[Install]\n" in core
    assert "WantedBy=multi-user.target" in core
    assert "--wait qdrant" in core
    assert "\n[Install]\n" not in online
    assert "\n[Install]\n" not in dual
    assert "Conflicts=knowledgehub-rag-embed-dual.service" in online
    assert "Conflicts=knowledgehub-rag-online.service" in dual


def test_mcp_orders_after_real_core_unit() -> None:
    for name in ("knowledgehub-mcp-lan.service", "knowledgehub-mcp-tailscale.service"):
        unit = _read(f"deploy/systemd/{name}")
        assert "Requires=knowledgehub-rag-core.service" in unit
        assert "After=" in unit and "knowledgehub-rag-core.service" in unit
        assert "qdrant.service" not in unit


def test_search_api_is_boot_installable_after_core() -> None:
    unit = _read("deploy/systemd/knowledgehub-rag-search-api.service")

    assert "Requires=docker.service knowledgehub-rag-core.service" in unit
    assert "\n[Install]\n" in unit
    assert "WantedBy=multi-user.target" in unit
    assert "--wait qdrant search-api" in unit


def test_scheduled_rag_retries_and_runs_dynamic_scheduler() -> None:
    unit = _read("deploy/systemd/knowledgehub-zotero-rag-incremental.service")

    assert "ExecStart=/usr/local/libexec/knowledgehub-rag-incremental-with-retries" in unit
    assert "TimeoutStartSec=30h" in unit
    assert "Restart=" not in unit


def test_retry_wrapper_runs_at_most_three_attempts(tmp_path: Path) -> None:
    counter = tmp_path / "counter"
    attempt = tmp_path / "attempt"
    attempt.write_text(
        "#!/bin/sh\n"
        f"counter='{counter}'\n"
        "value=0\n"
        'test ! -f "$counter" || value=$(cat "$counter")\n'
        "value=$((value + 1))\n"
        'printf \'%s\' "$value" >"$counter"\n'
        'test "$value" -ge 3\n',
        encoding="utf-8",
    )
    attempt.chmod(0o755)
    env = {
        **os.environ,
        "KH_RAG_ATTEMPT_BIN": str(attempt),
        "KH_RAG_RETRY_DELAY_SECONDS": "0",
    }

    completed = subprocess.run(
        ["bash", str(ROOT / "deploy/systemd/knowledgehub-rag-incremental-with-retries")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert completed.returncode == 0
    assert counter.read_text(encoding="utf-8") == "3"
    assert "attempt 3 succeeded" in completed.stdout


def test_retry_wrapper_stops_after_three_failures(tmp_path: Path) -> None:
    counter = tmp_path / "counter"
    attempt = tmp_path / "attempt"
    attempt.write_text(
        "#!/bin/sh\n"
        f"counter='{counter}'\n"
        "value=0\n"
        'test ! -f "$counter" || value=$(cat "$counter")\n'
        "value=$((value + 1))\n"
        'printf \'%s\' "$value" >"$counter"\n'
        "exit 75\n",
        encoding="utf-8",
    )
    attempt.chmod(0o755)
    env = {
        **os.environ,
        "KH_RAG_ATTEMPT_BIN": str(attempt),
        "KH_RAG_RETRY_DELAY_SECONDS": "0",
    }

    completed = subprocess.run(
        ["bash", str(ROOT / "deploy/systemd/knowledgehub-rag-incremental-with-retries")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert completed.returncode == 75
    assert counter.read_text(encoding="utf-8") == "3"
    assert "all 3 attempts failed" in completed.stderr


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        ("0, 24576, 10, 24566\n1, 24576, 20, 24556\n", "mode=dual gpu_ids=0,1"),
        ("0, 24576, 9000, 15576\n1, 24576, 20, 24556\n", "mode=single gpu_ids=1"),
        ("0, 24576, 10, 24566\n1, 24576, 9000, 15576\n", "mode=single gpu_ids=0"),
    ],
)
def test_gpu_scheduler_selects_dual_or_specific_single_card(
    tmp_path: Path, rows: str, expected: str
) -> None:
    nvidia_smi = tmp_path / "nvidia-smi"
    nvidia_smi.write_text(f"#!/bin/sh\nprintf '%s' '{rows}'\n", encoding="utf-8")
    nvidia_smi.chmod(0o755)
    env = {
        **os.environ,
        "KH_NVIDIA_SMI_BIN": str(nvidia_smi),
        "KH_DOCKER_BIN": "/bin/true",
    }

    completed = subprocess.run(
        ["bash", str(ROOT / "deploy/systemd/knowledgehub-rag-incremental-run"), "--select-only"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert completed.returncode == 0, completed.stderr
    assert expected in completed.stdout


def test_gpu_scheduler_defers_when_both_cards_are_busy(tmp_path: Path) -> None:
    nvidia_smi = tmp_path / "nvidia-smi"
    nvidia_smi.write_text(
        "#!/bin/sh\nprintf '%s' '0, 24576, 9000, 15576\n1, 24576, 8000, 16576\n'\n",
        encoding="utf-8",
    )
    nvidia_smi.chmod(0o755)
    docker = tmp_path / "docker"
    docker.write_text("#!/bin/sh\nprintf '%s\\n' reranker-quality-gpu1\n", encoding="utf-8")
    docker.chmod(0o755)
    env = {
        **os.environ,
        "KH_NVIDIA_SMI_BIN": str(nvidia_smi),
        "KH_DOCKER_BIN": str(docker),
        "KH_CURL_BIN": "/bin/true",
    }

    completed = subprocess.run(
        ["bash", str(ROOT / "deploy/systemd/knowledgehub-rag-incremental-run"), "--select-only"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert completed.returncode == 75
    assert "no GPU satisfies the VRAM policy" in completed.stdout
    assert "reusing" not in completed.stdout


def test_gpu_scheduler_reuses_running_target_embedding_container(tmp_path: Path) -> None:
    nvidia_smi = tmp_path / "nvidia-smi"
    nvidia_smi.write_text(
        "#!/bin/sh\nprintf '%s' '0, 24576, 9000, 15576\n1, 24576, 8000, 16576\n'\n",
        encoding="utf-8",
    )
    nvidia_smi.chmod(0o755)
    docker = tmp_path / "docker"
    docker.write_text("#!/bin/sh\nprintf '%s\\n' embedding-gpu0\n", encoding="utf-8")
    docker.chmod(0o755)
    env = {
        **os.environ,
        "KH_NVIDIA_SMI_BIN": str(nvidia_smi),
        "KH_DOCKER_BIN": str(docker),
        "KH_CURL_BIN": "/bin/true",
    }

    completed = subprocess.run(
        ["bash", str(ROOT / "deploy/systemd/knowledgehub-rag-incremental-run"), "--select-only"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert completed.returncode == 0
    assert "reusing healthy embedding-gpu0 on gpu=0" in completed.stdout
    assert "mode=single gpu_ids=0" in completed.stdout


def test_gpu_scheduler_does_not_compose_up_reused_container(tmp_path: Path) -> None:
    nvidia_smi = tmp_path / "nvidia-smi"
    nvidia_smi.write_text(
        "#!/bin/sh\nprintf '%s' '0, 24576, 9000, 15576\n1, 24576, 8000, 16576\n'\n",
        encoding="utf-8",
    )
    nvidia_smi.chmod(0o755)
    docker_log = tmp_path / "docker.log"
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >>'{docker_log}'\n"
        'case "$*" in\n'
        "  *' ps --status running --services') printf '%s\\n' embedding-gpu0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    env = {
        **os.environ,
        "KH_NVIDIA_SMI_BIN": str(nvidia_smi),
        "KH_DOCKER_BIN": str(docker),
        "KH_CURL_BIN": "/bin/true",
        "KH_CONDA_BIN": "/bin/true",
    }

    completed = subprocess.run(
        ["bash", str(ROOT / "deploy/systemd/knowledgehub-rag-incremental-run")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "skipping compose up" in completed.stdout
    docker_calls = docker_log.read_text(encoding="utf-8")
    assert " ps --status running --services" in docker_calls
    assert " up " not in docker_calls
    assert " stop " not in docker_calls


def test_gpu_scheduler_starts_and_cleans_only_missing_container(tmp_path: Path) -> None:
    nvidia_smi = tmp_path / "nvidia-smi"
    nvidia_smi.write_text(
        "#!/bin/sh\nprintf '%s' '0, 24576, 9000, 15576\n1, 24576, 20, 24556\n'\n",
        encoding="utf-8",
    )
    nvidia_smi.chmod(0o755)
    docker_log = tmp_path / "docker.log"
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >>'{docker_log}'\n"
        'case "$*" in\n'
        "  *' ps --status running --services') printf '%s\\n' embedding-gpu0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    env = {
        **os.environ,
        "KH_NVIDIA_SMI_BIN": str(nvidia_smi),
        "KH_DOCKER_BIN": str(docker),
        "KH_CURL_BIN": "/bin/true",
        "KH_CONDA_BIN": "/bin/true",
    }

    completed = subprocess.run(
        ["bash", str(ROOT / "deploy/systemd/knowledgehub-rag-incremental-run")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "starting missing embedding services: embedding-gpu1" in completed.stdout
    assert "stopping scheduler-owned embedding services: embedding-gpu1" in completed.stdout
    docker_calls = docker_log.read_text(encoding="utf-8")
    up_call = next(line for line in docker_calls.splitlines() if " up " in line)
    stop_call = next(line for line in docker_calls.splitlines() if " stop " in line)
    assert up_call.endswith("up -d --wait embedding-gpu1")
    assert stop_call.endswith("stop --timeout 120 embedding-gpu1")
    assert "embedding-gpu0" not in up_call
    assert "embedding-gpu0" not in stop_call
