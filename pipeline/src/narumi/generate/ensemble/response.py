"""Strict, bounded decoding for provider-returned ensemble JSON."""

from __future__ import annotations

import json
import math
from typing import Any, Never

from pydantic import ValidationError

from .types import EnsembleResponse

MAX_RESPONSE_BYTES = 262_144
MAX_JSON_DEPTH = 12


class ResponseStructureError(ValueError):
    """A received response is not the one closed JSON object required by the protocol."""


def _pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in values:
        if key in result:
            raise ResponseStructureError("duplicate JSON key")
        result[key] = value
    return result


def _constant(_value: str) -> Never:
    raise ResponseStructureError("non-finite JSON number")


def _float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ResponseStructureError("non-finite JSON number")
    return result


def _check_tree(value: Any, depth: int = 1) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ResponseStructureError("JSON depth exceeds the response limit")
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ResponseStructureError("JSON contains invalid Unicode") from exc
    elif isinstance(value, list):
        for child in value:
            _check_tree(child, depth + 1)
    elif isinstance(value, dict):
        for key, child in value.items():
            _check_tree(key, depth + 1)
            _check_tree(child, depth + 1)


def _reject_internal_aliases(value: Any) -> None:
    """Pydantic field names must not become undocumented wire aliases."""
    if isinstance(value, dict):
        if "from_claims" in value or "from_claim" in value:
            raise ResponseStructureError("unknown response field")
        for child in value.values():
            _reject_internal_aliases(child)
    elif isinstance(value, list):
        for child in value:
            _reject_internal_aliases(child)


def decode_response(raw_text: str) -> EnsembleResponse:
    """Decode one JSON object; no fences, prose, duplicate keys, bytes, or coercions."""
    if not isinstance(raw_text, str):
        raise ResponseStructureError("response must be Unicode text")
    try:
        encoded = raw_text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ResponseStructureError("response contains invalid Unicode") from exc
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise ResponseStructureError("response exceeds the byte limit")
    try:
        value = json.loads(
            raw_text,
            object_pairs_hook=_pairs,
            parse_constant=_constant,
            parse_float=_float,
        )
    except ResponseStructureError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError) as exc:
        raise ResponseStructureError("response is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ResponseStructureError("response root must be an object")
    _check_tree(value)
    _reject_internal_aliases(value)
    try:
        return EnsembleResponse.model_validate(value)
    except ValidationError as exc:
        raise ResponseStructureError("response does not match the closed schema") from exc
