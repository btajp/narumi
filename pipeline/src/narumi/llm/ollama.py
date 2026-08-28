"""Ollama generation restricted to verified local models and numeric loopback origins."""

from __future__ import annotations

import base64
import math
import os
from pathlib import Path
from typing import Any

from narumi.errors import EngineUnavailableError, InvalidArgumentError, NarumiError
from narumi.llm.base import CapabilityProfile
from narumi.providers.metadata import MetadataClient, validate_endpoint
from narumi.providers.metadata.http import JSONHTTPClient
from narumi.providers.metadata.ollama import local_selector

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
        http: JSONHTTPClient | None = None,
    ) -> None:
        self.model = model or os.environ.get(ENV_MODEL) or DEFAULT_MODEL
        self.host = _validate_host(host or os.environ.get(ENV_HOST) or DEFAULT_HOST)
        if not math.isfinite(timeout) or timeout <= 0:
            raise InvalidArgumentError("Ollama timeout must be positive")
        self.timeout = timeout
        self._http = http or JSONHTTPClient()
        self._metadata = MetadataClient(http=self._http)

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        images: list[Path] | None = None,
        max_tokens: int | None = None,
    ) -> str:
        # A loopback server can proxy cloud models. Recheck before every completion,
        # then pin the source using :local to also close model-alias replacement races.
        try:
            model = self._metadata.require_local_ollama_model(self.host, self.model)
        except NarumiError:
            raise EngineUnavailableError(
                "Ollama model could not be verified for local generation",
                details={"provider": PROVIDER_NAME, "reason": "local_model_unverified"},
            ) from None
        if images and "image" not in model["input_modalities"]:
            raise InvalidArgumentError(
                "The selected Ollama model does not support image input",
                details={"provider": PROVIDER_NAME},
            )
        if max_tokens is not None and (type(max_tokens) is not int or max_tokens <= 0):
            raise InvalidArgumentError("max_tokens must be a positive integer")
        payload: dict[str, Any] = {
            "model": local_selector(model["model_id"]),
            "prompt": prompt,
            "stream": False,
        }
        if system:
            payload["system"] = system
        if images:
            payload["images"] = [
                base64.standard_b64encode(path.read_bytes()).decode("ascii") for path in images
            ]
        if max_tokens is not None:
            payload["options"] = {"num_predict": max_tokens}
        body = self._post("/api/generate", payload)
        if body.get("remote_host") or body.get("remote_model"):
            raise EngineUnavailableError(
                "Ollama did not confirm a local result", details={"provider": PROVIDER_NAME}
            )
        text = body.get("response")
        if not isinstance(text, str) or not text.strip():
            raise EngineUnavailableError(
                "Ollama returned no text", details={"provider": PROVIDER_NAME}
            )
        return text.strip()

    def _post(self, route: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._http.request(
                "POST",
                _validate_host(self.host) + route,
                payload=payload,
                timeout=self.timeout,
                response_kind="generation",
            )
        except NarumiError:
            raise EngineUnavailableError(
                "Ollama generation request failed", details={"provider": PROVIDER_NAME}
            ) from None


def _validate_host(host: str) -> str:
    return validate_endpoint(PROVIDER_NAME, host)
