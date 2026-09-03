"""Conservative OpenAI-compatible model discovery and explicit probe promotion."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from narumi.providers.metadata.validation import (
    billing,
    invalid_metadata,
    parameter_schema,
    public_text,
    require_object,
)

MAX_MODELS = 200
VERIFICATION_REQUIRED = "adapter_capability_verification_required"


def verification_source_fingerprint(model: dict[str, Any]) -> str:
    """Fingerprint only metadata that the discovery response actually established.

    A successful generation probe deliberately adds roles, modalities, limits, and a
    parameter schema.  Those promoted fields cannot be part of the discovery identity,
    otherwise the proof would be discarded on every subsequent ``/models`` refresh.
    ``fetched_at`` is also observational and changes on every refresh.
    """
    billing_value = model.get("billing")
    billing_kind = billing_value.get("kind") if isinstance(billing_value, dict) else None
    payload = {
        "identity_version": "openai-compatible-model-source-v1",
        "model_id": model.get("model_id"),
        "display_name": model.get("display_name"),
        "resolved_revision": model.get("resolved_revision"),
        "timestamp_support": model.get("timestamp_support"),
        "availability_expires_on": model.get("availability_expires_on"),
        "source": model.get("source"),
        "billing_kind": billing_kind,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def fetch_models(
    request: Callable[..., dict[str, Any]], *, fetched_at: str
) -> list[dict[str, Any]]:
    body = request("GET", "/models")
    data = body.get("data")
    if body.get("object") != "list" or not isinstance(data, list):
        raise invalid_metadata()
    if len(data) > MAX_MODELS:
        raise invalid_metadata("metadata_catalog_limit")
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in data:
        raw = require_object(raw)
        if raw.get("object") not in {None, "model"}:
            raise invalid_metadata()
        model_id = public_text(raw.get("id"), max_length=256, identifier=True)
        if model_id in seen:
            raise invalid_metadata("metadata_catalog_limit")
        seen.add(model_id)
        if raw.get("created") is not None:
            created = raw["created"]
            if type(created) is not int or not 0 <= created <= 2**53 - 1:
                raise invalid_metadata()
        if raw.get("owned_by") is not None:
            public_text(raw["owned_by"])
        models.append(model_descriptor(model_id, fetched_at=fetched_at, verified=False))
    return models


def model_descriptor(model_id: str, *, fetched_at: str, verified: bool) -> dict[str, Any]:
    model_id = public_text(model_id, max_length=256, identifier=True)
    return {
        "model_id": model_id,
        "display_name": model_id[:160],
        "resolved_revision": None,
        "input_modalities": ["text"] if verified else [],
        "output_modalities": ["text"] if verified else [],
        "roles": ["llm"] if verified else [],
        "timestamp_support": "none",
        "context_window": None,
        "max_output_tokens": None,
        "parameter_schema": parameter_schema(None, enabled=verified),
        "availability": "available" if verified else "unverified",
        "availability_expires_on": None,
        "reason": None if verified else VERIFICATION_REQUIRED,
        "source": "provider_api",
        "fetched_at": fetched_at,
        "billing": billing("api"),
    }
