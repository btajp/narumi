"""Sanitize provider observations into a small public metadata vocabulary."""

from __future__ import annotations

import math
import re
from typing import Any

from narumi.errors import EngineUnavailableError

MODEL_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,255}\Z")
_CREDENTIAL = re.compile(r"(?:sk-(?:ant-|proj-|svcacct-)[A-Za-z0-9_-]+|Bearer\s+\S+)")
APP_MAX_OUTPUT_TOKENS = 32_768
DEFAULT_OUTPUT_TOKENS = 4096


def invalid_metadata(reason: str = "invalid_metadata") -> EngineUnavailableError:
    return EngineUnavailableError(
        "Provider metadata could not be verified", details={"reason": reason}
    )


def public_text(value: Any, *, max_length: int = 160, identifier: bool = False) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > max_length
        or value != value.strip()
        or not value.isprintable()
        or _CREDENTIAL.search(value)
        or (identifier and MODEL_ID.fullmatch(value) is None)
    ):
        raise invalid_metadata()
    return value


def optional_positive_integer(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 < value <= 2**53 - 1:
        raise invalid_metadata()
    return value


def require_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise invalid_metadata()
    return value


def check_public_payload(
    value: Any, *, secrets: tuple[str, ...] = (), reject_credentials: bool = True
) -> None:
    """Bound JSON structure and reject secret reflections before exposing any fields."""
    pending = [(value, 0)]
    visited = 0
    while pending:
        item, depth = pending.pop()
        visited += 1
        if depth > 32 or visited > 20_000:
            raise invalid_metadata("metadata_structure_limit")
        if isinstance(item, dict):
            pending.extend((key, depth + 1) for key in item)
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, float) and not math.isfinite(item):
            raise invalid_metadata("invalid_metadata")
        elif isinstance(item, str) and (
            any(secret and secret in item for secret in secrets)
            or (reject_credentials and _CREDENTIAL.search(item))
        ):
            raise invalid_metadata("unsafe_metadata")


def parameter_schema(max_tokens: int | None, *, enabled: bool = True) -> dict[str, Any]:
    """Advertise a bounded request option without inventing a model capability."""
    properties: dict[str, Any] = {}
    if enabled:
        known_limit = optional_positive_integer(max_tokens)
        maximum = min(APP_MAX_OUTPUT_TOKENS, known_limit or APP_MAX_OUTPUT_TOKENS)
        properties["max_tokens"] = {
            "type": "integer",
            "minimum": 1,
            "maximum": maximum,
            "default": min(DEFAULT_OUTPUT_TOKENS, maximum),
        }
    return {
        "type": "object",
        "properties": properties,
        "required": [],
        "additionalProperties": False,
    }


def billing(kind: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "input_usd_per_million_tokens": None,
        "output_usd_per_million_tokens": None,
        "audio_usd_per_minute": None,
        "fetched_at": None,
    }
