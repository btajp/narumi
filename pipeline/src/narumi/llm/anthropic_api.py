"""``anthropic-api`` provider: metered Anthropic Messages API."""

from __future__ import annotations

import base64
import mimetypes
import os
from pathlib import Path
from typing import Any

from narumi.errors import EngineUnavailableError, InvalidArgumentError
from narumi.llm.base import CapabilityProfile

PROVIDER_NAME = "anthropic-api"
ENV_MODEL = "NARUMI_ANTHROPIC_MODEL"
ENV_API_KEY = "ANTHROPIC_API_KEY"
DEFAULT_MODEL = "claude-opus-5"
"""Default model id (no date suffix). Override with ``NARUMI_ANTHROPIC_MODEL``."""

PROFILE = CapabilityProfile(
    vision=True,
    context_window=200_000,
    cost_class="api",
    data_destination="anthropic",
    tool_use=True,
    max_output_tokens=8192,
)

_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


class AnthropicAPIProvider:
    name = PROVIDER_NAME
    profile = PROFILE

    def __init__(self, *, model: str | None = None, api_key: str | None = None) -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise EngineUnavailableError(
                "anthropic SDK is not installed (uv sync --extra anthropic)",
                details={"provider": PROVIDER_NAME, "error": str(exc)},
            ) from exc
        key = api_key or os.environ.get(ENV_API_KEY)
        if not key:
            raise EngineUnavailableError(
                f"{ENV_API_KEY} is not set; the anthropic-api provider needs an API key",
                details={"provider": PROVIDER_NAME, "env": ENV_API_KEY},
            )
        self.model = model or os.environ.get(ENV_MODEL) or DEFAULT_MODEL
        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=key)

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        images: list[Path] | None = None,
        max_tokens: int | None = None,
    ) -> str:
        content: list[dict[str, Any]] = [_image_block(p) for p in images or []]
        content.append({"type": "text", "text": prompt})
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or self.profile.max_output_tokens,
            "messages": [{"role": "user", "content": content}],
        }
        if system:
            kwargs["system"] = system
        try:
            response = self._client.messages.create(**kwargs)
        except self._anthropic.APIError as exc:
            raise EngineUnavailableError(
                f"anthropic API call failed: {exc}",
                details={"provider": PROVIDER_NAME, "error": type(exc).__name__},
            ) from exc
        if response.stop_reason == "refusal":
            raise EngineUnavailableError(
                "anthropic API refused the request",
                details={"provider": PROVIDER_NAME, "stop_reason": "refusal"},
            )
        text = "".join(block.text for block in response.content if block.type == "text").strip()
        if not text:
            raise EngineUnavailableError(
                "anthropic API returned no text",
                details={"provider": PROVIDER_NAME, "stop_reason": response.stop_reason},
            )
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
