"""Device-bound Qwen reranker service with OOM batch reduction."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from knowledgehub.pipeline.config import (
    LIGHT_RERANKER_REVISION,
    QUALITY_RERANKER_REVISION,
    RagConfig,
)


class RerankBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1)
    passages: list[str] = Field(min_length=1, max_length=200)
    profile: str


class QwenCausalLMReranker:
    def __init__(self, config: RagConfig, *, device: str) -> None:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        if config.reranker_profile == "light":
            model = "Qwen/Qwen3-Reranker-0.6B"
            revision = LIGHT_RERANKER_REVISION
        elif config.reranker_profile == "quality":
            model = "Qwen/Qwen3-Reranker-4B"
            revision = QUALITY_RERANKER_REVISION
        else:
            raise ValueError("reranker service requires light or quality profile")
        self.profile = config.reranker_profile
        self.model_name = model
        self.revision = revision
        self.device = device
        self.max_length = config.reranker_max_length
        self.batch_size = config.reranker_batch_size
        self.instruction = config.embedding_query_instruction
        cache = str(config.model_cache_dir / "huggingface" / "hub")
        self._tokenizer: Any = AutoTokenizer.from_pretrained(
            model,
            revision=revision,
            cache_dir=cache,
            padding_side="left",
            local_files_only=False,
        )
        self._tokenizer.pad_token = self._tokenizer.eos_token
        dtype = torch.float16 if device.startswith("cuda") else torch.float32
        self._model: Any = AutoModelForCausalLM.from_pretrained(
            model,
            revision=revision,
            cache_dir=cache,
            local_files_only=False,
            dtype=dtype,
        ).to(device)
        self._model.eval()
        self._false_token_id = int(self._tokenizer("no", add_special_tokens=False).input_ids[0])
        self._true_token_id = int(self._tokenizer("yes", add_special_tokens=False).input_ids[0])
        prefix = (
            "<|im_start|>system\nJudge whether the Document meets the requirements based on "
            'the Query and the Instruct provided. The answer must be "yes" or "no".'
            "<|im_end|>\n<|im_start|>user\n"
        )
        suffix = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
        self._prefix_tokens = self._tokenizer.encode(prefix, add_special_tokens=False)
        self._suffix_tokens = self._tokenizer.encode(suffix, add_special_tokens=False)

    def rerank(self, query: str, passages: list[str]) -> tuple[list[float], int]:
        batch_size = min(self.batch_size, len(passages))
        while True:
            try:
                return self._predict(query, passages, batch_size), batch_size
            except RuntimeError as exc:
                if "out of memory" not in str(exc).lower() or batch_size == 1:
                    raise
                batch_size = max(1, batch_size // 2)
                try:
                    import torch

                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except ImportError:
                    pass

    def _predict(self, query: str, passages: list[str], batch_size: int) -> list[float]:
        import torch

        scores: list[float] = []
        available = self.max_length - len(self._prefix_tokens) - len(self._suffix_tokens)
        if available <= 0:
            raise ValueError("reranker max length is smaller than its prompt template")
        for offset in range(0, len(passages), batch_size):
            texts = [
                f"<Instruct>: {self.instruction}\n<Query>: {query}\n<Document>: {passage}"
                for passage in passages[offset : offset + batch_size]
            ]
            encoded = self._tokenizer(
                texts,
                padding=False,
                truncation="longest_first",
                return_attention_mask=False,
                max_length=available,
            )
            encoded["input_ids"] = [
                self._prefix_tokens + value + self._suffix_tokens for value in encoded["input_ids"]
            ]
            inputs = self._tokenizer.pad(
                encoded,
                padding=True,
                return_tensors="pt",
                max_length=self.max_length,
            )
            inputs = {key: value.to(self._model.device) for key, value in inputs.items()}
            with torch.inference_mode():
                logits = self._model(**inputs).logits[:, -1, :]
                binary = torch.stack(
                    [
                        logits[:, self._false_token_id],
                        logits[:, self._true_token_id],
                    ],
                    dim=1,
                )
                scores.extend(torch.softmax(binary, dim=1)[:, 1].float().cpu().tolist())
        return [float(value) for value in scores]


def create_app(
    config: RagConfig,
    *,
    device: str,
    model: Any | None = None,
) -> FastAPI:
    if not config.reranker_api_key:
        raise ValueError("KH_RERANKER_API_KEY is required")
    reranker = model or QwenCausalLMReranker(config, device=device)
    app = FastAPI(title="KnowledgeHub Reranker API", version="1")

    def authorize(authorization: str | None = Header(default=None)) -> None:
        if authorization != f"Bearer {config.reranker_api_key.get_secret_value()}":
            raise HTTPException(status_code=401, detail="invalid credentials")

    @app.get("/health")
    def health(_: None = Depends(authorize)) -> dict[str, Any]:
        return {
            "status": "ok",
            "profile": reranker.profile,
            "model": reranker.model_name,
            "revision": reranker.revision,
            "device": reranker.device,
            "max_length": reranker.max_length,
        }

    @app.post("/rerank")
    def rerank(body: RerankBody, _: None = Depends(authorize)) -> dict[str, Any]:
        if body.profile != reranker.profile:
            raise HTTPException(status_code=409, detail="requested profile is not loaded")
        scores, batch_size = reranker.rerank(body.query, body.passages)
        return {
            "scores": scores,
            "profile": reranker.profile,
            "model": reranker.model_name,
            "revision": reranker.revision,
            "batch_size": batch_size,
            "device": reranker.device,
        }

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("configs/rag/default.yaml"))
    parser.add_argument("--device", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    if (
        args.host not in {"127.0.0.1", "::1", "localhost"}
        and os.environ.get("KH_ALLOW_NON_LOOPBACK") != "true"
    ):
        raise SystemExit("refusing non-loopback bind without KH_ALLOW_NON_LOOPBACK=true")
    uvicorn.run(
        create_app(RagConfig.load(args.config), device=args.device),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
