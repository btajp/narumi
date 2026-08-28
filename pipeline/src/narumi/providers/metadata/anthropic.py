"""Anthropic Models API discovery without importing either generation SDK."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from narumi.providers.metadata.validation import (
    billing,
    invalid_metadata,
    optional_positive_integer,
    parameter_schema,
    public_text,
    require_object,
)

MAX_PAGES = 5
MAX_MODELS = 200
PAGE_SIZE = 100


def fetch_models(
    request: Callable[..., dict[str, Any]], *, provider_id: str, fetched_at: str
) -> list[dict[str, Any]]:
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    after_id: str | None = None
    for _ in range(MAX_PAGES):
        query = {"limit": str(PAGE_SIZE)}
        if after_id is not None:
            query["after_id"] = after_id
        body = request("GET", "/v1/models?" + urlencode(query))
        data, has_more = body.get("data"), body.get("has_more")
        if not isinstance(data, list) or len(data) > PAGE_SIZE or type(has_more) is not bool:
            raise invalid_metadata()
        for raw in data:
            candidate = _model(require_object(raw), provider_id=provider_id, fetched_at=fetched_at)
            if candidate["model_id"] in seen or len(models) >= MAX_MODELS:
                raise invalid_metadata("metadata_catalog_limit")
            seen.add(candidate["model_id"])
            models.append(candidate)
        if not has_more:
            return models
        if not data:
            raise invalid_metadata("invalid_pagination")
        after_id = public_text(body.get("last_id"), max_length=256, identifier=True)
        if after_id != models[-1]["model_id"]:
            raise invalid_metadata("invalid_pagination")
    raise invalid_metadata("metadata_page_limit")


def _model(raw: dict[str, Any], *, provider_id: str, fetched_at: str) -> dict[str, Any]:
    if raw.get("type") != "model":
        raise invalid_metadata()
    model_id = public_text(raw.get("id"), max_length=256, identifier=True)
    display_name = public_text(raw.get("display_name"))
    context_window = optional_positive_integer(raw.get("max_input_tokens"))
    max_output_tokens = optional_positive_integer(raw.get("max_tokens"))
    capabilities = raw.get("capabilities")
    image_input: bool | None = None
    if capabilities is not None:
        image = require_object(capabilities).get("image_input")
        if image is not None:
            image_input = require_object(image).get("supported")
            if type(image_input) is not bool:
                raise invalid_metadata()
    is_sdk = provider_id == "claude-agent-sdk"
    if is_sdk:
        availability, reason = "unverified", "sdk_authentication_and_history_isolation_unverified"
    elif image_input is None:
        availability, reason = "unverified", "model_capabilities_unavailable"
    else:
        availability, reason = "available", None
    return {
        "model_id": model_id,
        "display_name": display_name,
        "resolved_revision": None,
        "input_modalities": ["text", "image"] if image_input and not is_sdk else ["text"],
        "output_modalities": ["text"],
        "roles": ["llm"],
        "timestamp_support": "none",
        "context_window": context_window,
        "max_output_tokens": max_output_tokens,
        # Thinking/effort metadata is not an implemented generation parameter.
        "parameter_schema": parameter_schema(max_output_tokens, enabled=not is_sdk),
        "availability": availability,
        "reason": reason,
        "source": "provider_api",
        "fetched_at": fetched_at,
        "billing": billing("api"),
    }
