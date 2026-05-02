"""Thin HTTP client for Ollama.

Two design choices baked in:

  * `keep_alive: -1` — once a model is loaded into RAM/VRAM it stays
    resident across calls. First call has cold-start cost; every
    subsequent call is fast.
  * `format: "json"` — Ollama's grammar-constrained JSON mode.
    Output is guaranteed to be valid JSON; near-zero parse failures.

We use the `/api/generate` endpoint (single-turn) rather than `/api/chat`
because our prompts are stateless: system + user, get one JSON back, no
multi-turn conversation needed.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import requests

logger = logging.getLogger(__name__)


DEFAULT_BASE_URL = "http://localhost:11434"


@dataclass
class OllamaResponse:
    """Parsed response. `parsed` is the JSON object the model returned;
    `raw_text` is the underlying string in case parsing failed."""
    parsed: dict | None
    raw_text: str
    model: str
    elapsed_s: float


class OllamaClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout_s: int = 180,
        keep_alive: int | str = -1,
    ):
        self._base = (
            base_url
            or os.environ.get("OLLAMA_BASE_URL")
            or DEFAULT_BASE_URL
        ).rstrip("/")
        self._timeout = timeout_s
        self._keep_alive = keep_alive
        self._session = requests.Session()

    def generate_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        temperature: float = 0.2,
        num_predict: int = 256,
    ) -> OllamaResponse:
        """Run a single-turn prompt expecting a JSON object back.

        Raises requests.RequestException on terminal HTTP errors.
        Sets `parsed=None` if the model's JSON-mode output still
        somehow fails to decode (rare; Ollama's grammar usually prevents
        this).
        """
        payload: dict[str, Any] = {
            "model": model,
            "prompt": user,
            "system": system,
            "stream": False,
            "format": "json",
            "keep_alive": self._keep_alive,
            "options": {
                "temperature": temperature,
                "num_predict": num_predict,
            },
        }
        url = f"{self._base}/api/generate"
        resp = self._session.post(url, json=payload, timeout=self._timeout)
        resp.raise_for_status()
        body = resp.json()

        text = body.get("response", "")
        elapsed = (
            body.get("total_duration", 0) / 1e9
            if body.get("total_duration")
            else 0.0
        )

        try:
            parsed = json.loads(text) if text else None
        except json.JSONDecodeError as e:
            logger.warning(
                "ollama json-mode produced invalid json model=%s err=%s body=%s",
                model, e, text[:200],
            )
            parsed = None

        return OllamaResponse(
            parsed=parsed, raw_text=text, model=model, elapsed_s=elapsed,
        )

    def warmup(self, model: str) -> None:
        """Load `model` into RAM/VRAM with a no-op call. Useful at worker
        startup so the first real listing doesn't pay cold-start latency."""
        try:
            self._session.post(
                f"{self._base}/api/generate",
                json={
                    "model": model,
                    "prompt": "",
                    "stream": False,
                    "keep_alive": self._keep_alive,
                },
                timeout=120,
            ).raise_for_status()
            logger.info("warmed up model=%s", model)
        except requests.RequestException as e:
            logger.warning("warmup failed for %s: %s", model, e)


# --- module-level singleton ------------------------------------------------

_DEFAULT: OllamaClient | None = None


def get_default_client() -> OllamaClient:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = OllamaClient()
    return _DEFAULT
