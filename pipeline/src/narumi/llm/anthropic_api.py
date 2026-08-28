"""``anthropic-api`` provider: metered Anthropic Messages API."""

from __future__ import annotations

import base64
import mimetypes
import os
from pathlib import Path
from typing import Any

from narumi.errors import EngineUnavailableError, InvalidArgumentError
from narumi.llm.base import CapabilityProfile
from narumi.providers.metadata.http import JSONHTTPClient

PROVIDER_NAME = "anthropic-api"
ENV_MODEL = "NARUMI_ANTHROPIC_MODEL"
ENV_API_KEY = "ANTHROPIC_API_KEY"
DEFAULT_MODEL = "claude-opus-5"
"""Default model id (no date suffix). Override with ``NARUMI_ANTHROPIC_MODEL``."""
MESSAGES_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_TIMEOUT_SEC = 600.0

PROFILE = CapabilityProfile(
    vision=True,
    context_window=200_000,
    cost_class="api",
    data_destination="anthropic",
    tool_use=False,
    max_output_tokens=8192,
)

_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


class AnthropicAPIProvider:
    name = PROVIDER_NAME
    profile = PROFILE

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        http: JSONHTTPClient | None = None,
    ) -> None:
        key = os.environ.get(ENV_API_KEY) if api_key is None else api_key
        if not key:
            raise EngineUnavailableError(
                "The anthropic-api provider needs a non-empty API key",
                details={"provider": PROVIDER_NAME, "env": ENV_API_KEY},
            )
        if (
            not isinstance(key, str)
            or len(key) > 4096
            or any(not 33 <= ord(char) <= 126 for char in key)
        ):
            raise InvalidArgumentError(
                "API key has an invalid format", details={"provider": PROVIDER_NAME}
            )
        self.model = (os.environ.get(ENV_MODEL) or DEFAULT_MODEL) if model is None else model
        if (
            not isinstance(self.model, str)
            or not self.model
            or len(self.model) > 256
            or not self.model.isascii()
            or not self.model.isprintable()
            or self.model != self.model.strip()
        ):
            raise InvalidArgumentError(
                "Anthropic model must be a non-empty model identifier",
                details={"provider": PROVIDER_NAME},
            )
        self._api_key = key
        self._http = http if http is not None else JSONHTTPClient()

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        images: list[Path] | None = None,
        max_tokens: int | None = None,
    ) -> str:
        if max_tokens is not None and (type(max_tokens) is not int or max_tokens <= 0):
            raise InvalidArgumentError("max_tokens must be a positive integer")
        content: list[dict[str, Any]] = [_image_block(p) for p in images or []]
        content.append({"type": "text", "text": prompt})
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.profile.max_output_tokens if max_tokens is None else max_tokens,
            "messages": [{"role": "user", "content": content}],
        }
        if system:
            payload["system"] = system
        try:
            # The SDK inherits custom headers, endpoints and debug logging from the
            # environment. This transport supplies only these explicit credentials.
            response = self._http.request(
                "POST",
                MESSAGES_URL,
                headers={"x-api-key": self._api_key, "anthropic-version": "2023-06-01"},
                payload=payload,
                timeout=DEFAULT_TIMEOUT_SEC,
                response_kind="generation",
            )
        except Exception:
            # This is an external-call boundary: neither exception text nor its
            # chained traceback is safe for job logs or the catalog.
            raise EngineUnavailableError(
                "Anthropic generation request failed", details={"provider": PROVIDER_NAME}
            ) from None
        return _response_text(response)


def _response_text(response: dict[str, Any]) -> str:
    def invalid_response() -> EngineUnavailableError:
        return EngineUnavailableError(
            "Anthropic returned no usable text", details={"provider": PROVIDER_NAME}
        )

    if (
        not isinstance(response, dict)
        or response.get("type") != "message"
        or response.get("role") != "assistant"
        or response.get("stop_reason") not in ("end_turn", "max_tokens", "stop_sequence")
        or not isinstance(response.get("content"), list)
    ):
        raise invalid_response() from None
    parts: list[str] = []
    for block in response["content"]:
        if not isinstance(block, dict):
            raise invalid_response() from None
        if block.get("type") == "text":
            if not isinstance(block.get("text"), str):
                raise invalid_response() from None
            parts.append(block["text"])
        elif block.get("type") not in ("thinking", "redacted_thinking"):
            raise invalid_response() from None
    text = "".join(parts).strip()
    if not text:
        raise invalid_response() from None
    return text


def _image_block(path: Path) -> dict[str, Any]:
    media_type, _ = mimetypes.guess_type(str(path))
    if media_type not in _IMAGE_TYPES:
        raise InvalidArgumentError(
            f"unsupported image type for {path.name}: {media_type}",
            details={"path": str(path), "supported": sorted(_IMAGE_TYPES)},
        )
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}
