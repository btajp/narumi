"""A closed, text-only projection of the official Codex model catalog."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from narumi.errors import InvalidArgumentError, ModelUnavailableError
from narumi.providers._common import timestamp
from narumi.providers.codex._rpc import unavailable
from narumi.providers.metadata.validation import (
    billing,
    check_public_payload,
    public_text,
    require_object,
)

MAX_PAGES = 5
MAX_MODELS = 200
PAGE_SIZE = 100
_EFFORT = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")


def fetch_models(call: Callable[..., dict[str, Any]]) -> list[dict[str, Any]]:
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    cursors: set[str] = set()
    cursor: str | None = None
    fetched_at = timestamp()
    for _ in range(MAX_PAGES):
        params: dict[str, Any] = {"limit": PAGE_SIZE, "includeHidden": False}
        if cursor is not None:
            params["cursor"] = cursor
        body = call("model/list", params)
        check_public_payload(body)
        data = body.get("data")
        if not isinstance(data, list) or len(data) > PAGE_SIZE:
            raise unavailable("codex_invalid_model_catalog")
        for raw in data:
            record = require_object(raw)
            if type(record.get("hidden")) is not bool:
                raise unavailable("codex_invalid_model_catalog")
            if record["hidden"]:
                continue
            model = _model(record, fetched_at)
            identifier = model["model_id"]
            if identifier in seen or len(models) >= MAX_MODELS:
                raise unavailable("codex_model_catalog_limit")
            seen.add(identifier)
            models.append(model)
        next_cursor = body.get("nextCursor")
        if next_cursor is None:
            return models
        if (
            not isinstance(next_cursor, str)
            or not next_cursor
            or len(next_cursor) > 1024
            or not next_cursor.isprintable()
            or next_cursor in cursors
            or not data
        ):
            raise unavailable("codex_invalid_model_pagination")
        cursors.add(next_cursor)
        cursor = next_cursor
    raise unavailable("codex_model_page_limit")


def _model(raw: dict[str, Any], fetched_at: str) -> dict[str, Any]:
    identifier = public_text(raw.get("model"), max_length=256, identifier=True)
    name = public_text(raw.get("displayName"))
    raw_efforts = raw.get("supportedReasoningEfforts")
    if not isinstance(raw_efforts, list) or not 1 <= len(raw_efforts) <= 16:
        raise unavailable("codex_invalid_reasoning_catalog")
    efforts: list[str] = []
    for raw_effort in raw_efforts:
        effort = require_object(raw_effort).get("reasoningEffort")
        if not isinstance(effort, str) or not _EFFORT.fullmatch(effort) or effort in efforts:
            raise unavailable("codex_invalid_reasoning_catalog")
        efforts.append(effort)
    default_effort = raw.get("defaultReasoningEffort")
    if default_effort not in efforts:
        raise unavailable("codex_invalid_reasoning_catalog")
    modalities = raw.get("inputModalities")
    text_verified = isinstance(modalities, list) and "text" in modalities
    return {
        "model_id": identifier,
        "display_name": name,
        "resolved_revision": None,
        "input_modalities": ["text"] if text_verified else [],
        "output_modalities": ["text"],
        "roles": ["llm"],
        "timestamp_support": "none",
        "context_window": None,
        "max_output_tokens": None,
        "parameter_schema": {
            "type": "object",
            "properties": {
                "reasoning_effort": {"type": "string", "enum": efforts, "default": default_effort}
            },
            "required": [],
            "additionalProperties": False,
        },
        "availability": "available" if text_verified else "unverified",
        "reason": None if text_verified else "codex_text_capability_unverified",
        "source": "runtime",
        "fetched_at": fetched_at,
        "billing": billing("subscription"),
    }


def select_model(
    models: list[dict[str, Any]], identifier: str, parameters: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(parameters, dict) or set(parameters) - {"reasoning_effort"}:
        raise InvalidArgumentError("Codex generation parameters are not supported")
    model = next((item for item in models if item["model_id"] == identifier), None)
    if model is None or model["availability"] != "available":
        raise ModelUnavailableError("The selected Codex model is not available")
    if "reasoning_effort" in parameters:
        supported = model["parameter_schema"]["properties"]["reasoning_effort"]["enum"]
        effort = parameters["reasoning_effort"]
        if not isinstance(effort, str) or effort not in supported:
            raise InvalidArgumentError("The selected Codex model does not support this effort")
    return model
