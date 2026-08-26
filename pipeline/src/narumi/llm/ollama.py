"""``ollama`` provider: local models through the Ollama HTTP API (nothing leaves the machine)."""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from narumi.errors import EngineUnavailableError, InvalidArgumentError
from narumi.llm.base import CapabilityProfile

PROVIDER_NAME = "ollama"
ENV_MODEL = "NARUMI_OLLAMA_MODEL"
ENV_HOST = "OLLAMA_HOST"
DEFAULT_MODEL = "qwen2.5:7b"
DEFAULT_HOST = "http://127.0.0.1:11434"
DEFAULT_TIMEOUT_SEC = 600.0

PROFILE = CapabilityProfile(
    vision=False,
    context_window=8192,
    cost_class="local",
    data_destination="local",
    tool_use=False,
    max_output_tokens=4096,
)


class OllamaProvider:
    name = PROVIDER_NAME
    profile = PROFILE

    def __init__(
        self,
        *,
        model: str | None = None,
        host: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SEC,
    ) -> None:
        self.model = model or os.environ.get(ENV_MODEL) or DEFAULT_MODEL
        self.host = _validate_host(host or os.environ.get(ENV_HOST) or DEFAULT_HOST)
        self.timeout = timeout

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        images: list[Path] | None = None,
        max_tokens: int | None = None,
    ) -> str:
        payload: dict[str, Any] = {"model": self.model, "prompt": prompt, "stream": False}
        if system:
            payload["system"] = system
        if images:
            payload["images"] = [
                base64.standard_b64encode(p.read_bytes()).decode("ascii") for p in images
            ]
        if max_tokens:
            payload["options"] = {"num_predict": max_tokens}
        body = self._post("/api/generate", payload)
        text = body.get("response")
        if not isinstance(text, str) or not text.strip():
            raise EngineUnavailableError(
                "ollama returned no text", details={"provider": PROVIDER_NAME, "model": self.model}
            )
        return text.strip()

    def _post(self, route: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            self.host + route,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise EngineUnavailableError(
                f"ollama at {self.host} answered HTTP {exc.code}",
                details={"provider": PROVIDER_NAME, "status": exc.code, "model": self.model},
            ) from exc
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            raise EngineUnavailableError(
                f"ollama is not reachable at {self.host}: {exc}",
                details={"provider": PROVIDER_NAME, "host": self.host},
            ) from exc
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EngineUnavailableError(
                "ollama returned invalid JSON", details={"provider": PROVIDER_NAME}
            ) from exc
        if not isinstance(body, dict):
            raise EngineUnavailableError(
                "ollama returned an unexpected payload", details={"provider": PROVIDER_NAME}
            )
        return body


def _validate_host(host: str) -> str:
    parsed = urllib.parse.urlsplit(host)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise InvalidArgumentError(
            f"{ENV_HOST} must be an http(s) URL, got {host!r}", details={"host": host}
        )
    return host.rstrip("/")
