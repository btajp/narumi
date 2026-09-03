"""Bounded private protocol between the resident server and a short-lived SDK worker."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 3 * 1024 * 1024
MAX_RESPONSE_BYTES = 3 * 1024 * 1024
MAX_PROMPT_BYTES = 2 * 1024 * 1024
MAX_SYSTEM_BYTES = 128 * 1024
MAX_API_KEY_BYTES = 16 * 1024
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_TOKEN_COUNT = 2**53 - 1
MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
CONNECTION_PATTERN = re.compile(r"conn-[a-f0-9]{12,64}")
RUNTIME_EVIDENCE_FIELDS = frozenset(
    {
        "resource_id",
        "sdk_version",
        "cli_version",
        "cli_sha256",
        "sdk_source_sha256",
        "isolation_profile_sha256",
    }
)
EXECUTION_EVIDENCE_FIELDS = RUNTIME_EVIDENCE_FIELDS | {"resource_sha256"}

PROBE_SENTINEL = "NARUMI_CLAUDE_SDK_MODEL_PROBE_V1_OK"
PROBE_SYSTEM = (
    "You are answering a provider availability probe. Do not use tools. "
    "Return only the exact ASCII sentinel requested by the user."
)
PROBE_PROMPT = f"Return exactly this text and nothing else: {PROBE_SENTINEL}"


@dataclass(frozen=True)
class WorkerRequest:
    connection_id: str
    api_key: str
    model_id: str
    prompt: str
    system: str | None
    expected_runtime: dict[str, str] | None = None


@dataclass(frozen=True)
class WorkerResponse:
    text: str
    returned_model: str
    usage: dict[str, int]
    runtime_evidence: dict[str, str] = field(default_factory=dict)


def encode_request(request: WorkerRequest) -> bytes:
    validate_request(request)
    return _encode(
        {
            "protocol_version": PROTOCOL_VERSION,
            "connection_id": request.connection_id,
            "api_key": request.api_key,
            "model_id": request.model_id,
            "prompt": request.prompt,
            "system": request.system,
            "expected_runtime": request.expected_runtime,
        },
        MAX_REQUEST_BYTES,
    )


def decode_request(raw: bytes) -> WorkerRequest:
    value = _decode(raw, MAX_REQUEST_BYTES)
    if (
        set(value)
        != {
            "protocol_version",
            "connection_id",
            "api_key",
            "model_id",
            "prompt",
            "system",
            "expected_runtime",
        }
        or value.get("protocol_version") != PROTOCOL_VERSION
    ):
        raise ValueError("invalid worker request")
    request = WorkerRequest(
        connection_id=value.get("connection_id"),
        api_key=value.get("api_key"),
        model_id=value.get("model_id"),
        prompt=value.get("prompt"),
        system=value.get("system"),
        expected_runtime=value.get("expected_runtime"),
    )
    validate_request(request)
    return request


def encode_response(response: WorkerResponse) -> bytes:
    validate_response(response)
    return _encode(
        {
            "protocol_version": PROTOCOL_VERSION,
            "status": "ok",
            "text": response.text,
            "returned_model": response.returned_model,
            "usage": response.usage,
            "runtime_evidence": response.runtime_evidence,
        },
        MAX_RESPONSE_BYTES,
    )


def decode_response(raw: bytes) -> WorkerResponse:
    value = _decode(raw, MAX_RESPONSE_BYTES)
    if (
        set(value)
        != {
            "protocol_version",
            "status",
            "text",
            "returned_model",
            "usage",
            "runtime_evidence",
        }
        or value.get("protocol_version") != PROTOCOL_VERSION
        or value.get("status") != "ok"
    ):
        raise ValueError("invalid worker response")
    response = WorkerResponse(
        text=value.get("text"),
        returned_model=value.get("returned_model"),
        usage=value.get("usage"),
        runtime_evidence=value.get("runtime_evidence"),
    )
    validate_response(response)
    return response


def encode_failure() -> bytes:
    return _encode({"protocol_version": PROTOCOL_VERSION, "status": "error"}, MAX_RESPONSE_BYTES)


def validate_request(request: WorkerRequest) -> None:
    if not isinstance(request.connection_id, str) or not CONNECTION_PATTERN.fullmatch(
        request.connection_id
    ):
        raise ValueError("invalid connection")
    if (
        not _api_key(request.api_key)
        or not isinstance(request.model_id, str)
        or not MODEL_PATTERN.fullmatch(request.model_id)
        or not _private_string(request.prompt, MAX_PROMPT_BYTES)
        or (
            request.system is not None
            and not _private_string(request.system, MAX_SYSTEM_BYTES, allow_empty=True)
        )
        or (request.expected_runtime is not None and not valid_runtime(request.expected_runtime))
    ):
        raise ValueError("invalid worker request")


def _api_key(value: Any) -> bool:
    return (
        _private_string(value, MAX_API_KEY_BYTES)
        and value.isascii()
        and value.isprintable()
        and not any(character.isspace() for character in value)
    )


def validate_response(response: WorkerResponse) -> None:
    if (
        not _private_string(response.text, MAX_TEXT_BYTES)
        or not isinstance(response.returned_model, str)
        or not MODEL_PATTERN.fullmatch(response.returned_model)
        or not valid_usage(response.usage)
        or not valid_runtime(response.runtime_evidence)
    ):
        raise ValueError("invalid worker response")


def valid_usage(value: Any) -> bool:
    allowed = {
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
    }
    return (
        isinstance(value, dict)
        and bool(value)
        and not set(value).difference(allowed)
        and {"input_tokens", "output_tokens"}.issubset(value)
        and all(type(item) is int and 0 <= item <= MAX_TOKEN_COUNT for item in value.values())
    )


def valid_runtime(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) in {RUNTIME_EVIDENCE_FIELDS, EXECUTION_EVIDENCE_FIELDS}
        and all(_public_evidence_string(item) for item in value.values())
        and value["sdk_version"] == "0.2.144"
        and value["cli_version"] == "2.1.239"
        and all(
            re.fullmatch(r"[a-f0-9]{64}", value[field])
            for field in (
                "cli_sha256",
                "sdk_source_sha256",
                "isolation_profile_sha256",
                *(("resource_sha256",) if "resource_sha256" in value else ()),
            )
        )
    )


def _public_evidence_string(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value.isascii()
        and value.isprintable()
        and "/" not in value
        and "\\" not in value
        and len(value) <= 128
    )


def _private_string(value: Any, limit: int, *, allow_empty: bool = False) -> bool:
    return (
        isinstance(value, str)
        and (allow_empty or bool(value.strip()))
        and "\x00" not in value
        and len(value.encode("utf-8")) <= limit
    )


def _encode(value: dict[str, Any], limit: int) -> bytes:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )
    if len(raw) > limit:
        raise ValueError("private protocol message is too large")
    return raw + b"\n"


def _decode(raw: bytes, limit: int) -> dict[str, Any]:
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= limit + 1:
        raise ValueError("invalid private protocol message")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite number")),
        )
    except (UnicodeError, ValueError, RecursionError):
        raise ValueError("invalid private protocol message") from None
    if not isinstance(value, dict) or not _finite(value):
        raise ValueError("invalid private protocol message")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _finite(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_finite(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _finite(item) for key, item in value.items())
    return True
