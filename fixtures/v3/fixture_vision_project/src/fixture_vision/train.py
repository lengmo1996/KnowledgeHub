"""Offline training CLI that always emits structured JSON."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import yaml

from fixture_vision.data import generate
from fixture_vision.evaluate import evaluate
from fixture_vision.model import FusionModel


def run(config: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    data = generate(config)
    model = FusionModel(
        int(config["input_dim"]),
        int(config["hidden_dim"]),
        str(config["fusion"]),
        int(config["seed"]),
    )
    final_loss = 0.0
    for epoch in range(int(config["epochs"])):
        final_loss = model.train_step(*data["train"], float(config["learning_rate"]))
        if bool(config.get("inject_nan")) and epoch == 1:
            raise FloatingPointError("controlled fixture failure: injected non-finite loss")
    validation = evaluate(model, data["validation"])
    test = evaluate(model, data["test"])
    return {
        "accuracy": test["accuracy"],
        "loss": test["loss"],
        "validation_accuracy": validation["accuracy"],
        "validation_loss": validation["loss"],
        "train_final_loss": final_loss,
        "parameter_count": model.parameter_count,
        "runtime_seconds": time.perf_counter() - started,
        "seed": int(config["seed"]),
        "samples": int(config["samples"]),
        "fusion": str(config["fusion"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    result = run(config)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
