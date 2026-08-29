"""Intersect the OpenAI Models API with reviewed text and audio capabilities."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from narumi.providers.metadata.audio_capabilities import audio_model_capabilities
from narumi.providers.metadata.openai_capabilities import model_capabilities
from narumi.providers.metadata.validation import (
    billing,
    invalid_metadata,
    parameter_schema,
    public_text,
    require_object,
)

MAX_MODELS = 200
MODELS_SOURCE_URL = "https://developers.openai.com/api/reference/resources/models"
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


def fetch_models(
    request: Callable[..., dict[str, Any]], *, fetched_at: str, now: datetime
) -> list[dict[str, Any]]:
    body = request("GET", "/v1/models")
    data = body.get("data")
    if body.get("object") != "list" or not isinstance(data, list):
        raise invalid_metadata()
    if len(data) > MAX_MODELS:
        raise invalid_metadata("metadata_catalog_limit")
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in data:
        candidate = _model(require_object(raw), fetched_at=fetched_at, now=now)
        if candidate["model_id"] in seen:
            raise invalid_metadata("metadata_catalog_limit")
        seen.add(candidate["model_id"])
        models.append(candidate)
    return models


def _model(raw: dict[str, Any], *, fetched_at: str, now: datetime) -> dict[str, Any]:
    if raw.get("object") != "model":
        raise invalid_metadata()
    model_id = public_text(raw.get("id"), max_length=256, identifier=True)
    created = raw.get("created")
    if type(created) is not int or not 0 <= created <= 2**53 - 1:
        raise invalid_metadata()
    public_text(raw.get("owned_by"))
    expires_on = _shutdown_date(raw.get("shutdown_date"))
    capabilities = model_capabilities(model_id)
    candidate = {
        "model_id": model_id,
        "display_name": model_id[:160],
        "resolved_revision": None,
        "input_modalities": [],
        "output_modalities": [],
        "roles": [],
        "timestamp_support": "none",
        "context_window": None,
        "max_output_tokens": None,
        "parameter_schema": parameter_schema(None, enabled=False),
        "availability": "unverified",
        "availability_expires_on": expires_on.isoformat() if expires_on else None,
        "reason": "model_capabilities_unavailable",
        "source": "provider_api",
        "fetched_at": fetched_at,
        "billing": billing("api"),
    }
    if capabilities is not None:
        candidate.update(
            display_name=capabilities.display_name,
            resolved_revision=capabilities.resolved_revision,
            input_modalities=["text"],
            output_modalities=["text"],
            roles=["llm"],
            context_window=capabilities.context_window,
            max_output_tokens=capabilities.max_output_tokens,
            parameter_schema=capabilities.parameter_schema(),
            availability="available",
            reason=None,
        )
    elif (audio := audio_model_capabilities(model_id)) is not None:
        candidate.update(
            display_name=audio.display_name,
            resolved_revision=audio.resolved_revision,
            input_modalities=["audio"],
            output_modalities=["text"],
            roles=["transcription"],
            timestamp_support=audio.timestamp_support,
            parameter_schema=audio.parameter_schema(),
            availability=audio.availability,
            reason=audio.reason,
        )
    # The provider supplies a date, not a shutdown instant or timezone. Preserve
    # that date; the UTC comparison is Narumi's conservative application rule.
    if expires_on is not None and expires_on <= now.astimezone(UTC).date():
        candidate.update(availability="retired", reason="model_retired")
    return candidate


def _shutdown_date(value: Any) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise invalid_metadata()
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise invalid_metadata() from None
