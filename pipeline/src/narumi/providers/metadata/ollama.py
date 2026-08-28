"""Inspect local Ollama model metadata without pulling or loading a model."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from narumi.errors import EngineUnavailableError
from narumi.providers.metadata.validation import (
    billing,
    invalid_metadata,
    optional_positive_integer,
    parameter_schema,
    public_text,
    require_object,
)

MAX_MODELS = 200
_DIGEST = re.compile(r"(?:sha256:)?[a-f0-9]{64}\Z")


def local_selector(model: str) -> str:
    """Pin source intent so an alias changed to a cloud model cannot receive text."""
    model = public_text(model, max_length=250, identifier=True)
    if is_cloud_model(model):
        raise invalid_metadata("remote_models_not_supported")
    if model.lower().endswith(":local"):
        model = model[:-6]
    if ":" not in model.rsplit("/", 1)[-1]:
        model += ":latest"
    return model + ":local"


def is_cloud_model(model: str) -> bool:
    tag = model.lower().rsplit("/", 1)[-1]
    return ":cloud" in tag or tag.endswith("-cloud")


def fetch_models(
    request: Callable[..., dict[str, Any]],
    *,
    fetched_at: str,
    selected_model: str | None = None,
) -> list[dict[str, Any]]:
    body = request("GET", "/api/tags")
    data = body.get("models")
    if not isinstance(data, list):
        raise invalid_metadata()
    if len(data) > MAX_MODELS:
        raise invalid_metadata("metadata_catalog_limit")
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in data:
        raw = require_object(raw)
        model_id = public_text(raw.get("model", raw.get("name")), max_length=256, identifier=True)
        if ("name" in raw and raw["name"] != model_id) or model_id in seen:
            raise invalid_metadata()
        seen.add(model_id)
        if selected_model is not None and model_id != selected_model:
            continue
        models.append(_model(raw, model_id, request=request, fetched_at=fetched_at))
    return models


def _model(
    raw: dict[str, Any],
    model_id: str,
    *,
    request: Callable[..., dict[str, Any]],
    fetched_at: str,
) -> dict[str, Any]:
    digest = raw.get("digest")
    if digest is not None and (not isinstance(digest, str) or not _DIGEST.fullmatch(digest)):
        raise invalid_metadata()
    candidate = {
        "model_id": model_id,
        "display_name": model_id[:160],
        "resolved_revision": digest,
        "input_modalities": [],
        "output_modalities": [],
        "roles": [],
        "timestamp_support": "none",
        "context_window": None,
        "max_output_tokens": None,
        "parameter_schema": parameter_schema(None, enabled=False),
        "availability": "unverified",
        "reason": "local_model_metadata_unverified",
        "source": "runtime",
        "fetched_at": fetched_at,
        "billing": billing("unknown"),
    }
    # Even /api/show can proxy cloud metadata, so inspect tags before calling it.
    if is_cloud_model(model_id) or _is_remote(raw):
        return _unavailable(candidate, "unsupported", "remote_models_not_supported")
    details = require_object(raw.get("details", {}))
    size = optional_positive_integer(raw.get("size"))
    if digest is None or size is None or details.get("format") != "gguf":
        return candidate
    try:
        show = request("POST", "/api/show", payload={"model": local_selector(model_id)})
    except EngineUnavailableError:
        # Older servers may not implement the explicit local selector. Do not retry
        # with an unqualified name, which could allow a remote source.
        return _unavailable(candidate, "unverified", "local_model_verification_failed")
    if _is_remote(show):
        return _unavailable(candidate, "unsupported", "remote_models_not_supported")
    if require_object(show.get("details", {})).get("format") != "gguf":
        return candidate
    capabilities = show.get("capabilities")
    if not isinstance(capabilities, list) or any(type(item) is not str for item in capabilities):
        return candidate
    if "cloud" in capabilities or "remote" in capabilities:
        return _unavailable(candidate, "unsupported", "remote_models_not_supported")
    if "completion" not in capabilities:
        return _unavailable(candidate, "unsupported", "text_completion_not_supported")
    info = require_object(show.get("model_info", {}))
    architecture = info.get("general.architecture")
    context_window = None
    if architecture is not None:
        architecture = public_text(architecture, max_length=64, identifier=True)
        context_window = optional_positive_integer(info.get(architecture + ".context_length"))
    candidate.update(
        input_modalities=["text", "image"] if "vision" in capabilities else ["text"],
        output_modalities=["text"],
        roles=["llm"],
        context_window=context_window,
        parameter_schema=parameter_schema(None),
        availability="available",
        reason=None,
        billing=billing("local"),
    )
    return candidate


def _is_remote(raw: dict[str, Any]) -> bool:
    return any(raw.get(field) not in (None, "") for field in ("remote_host", "remote_model"))


def _unavailable(candidate: dict[str, Any], availability: str, reason: str) -> dict[str, Any]:
    candidate.update(availability=availability, reason=reason)
    return candidate
