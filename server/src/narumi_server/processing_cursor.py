"""Opaque, request-bound cursor for durable processing-run pagination."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from collections.abc import Sequence

from narumi.catalog.db import normalize_scope
from narumi.errors import InvalidArgumentError

RunOrderKey = tuple[str, str]

_MAGIC = b"NR1"
_HASH_BYTES = 32
_TIMESTAMP_BYTES = 20
_RUN_BYTES = 16
_CHECKSUM_BYTES = 16
_PAYLOAD_BYTES = 3 + _HASH_BYTES * 2 + _TIMESTAMP_BYTES + _RUN_BYTES
_CURSOR_BYTES = _PAYLOAD_BYTES + _CHECKSUM_BYTES
_MEETING_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
_RUN_ID = re.compile(r"^run-([0-9a-f]{32})$")
_CREATED_AT = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")


def encode_processing_runs_cursor(
    after: RunOrderKey | None,
    *,
    meeting_id: str,
    scope: str | Sequence[str] | None,
) -> str | None:
    """Encode the next exclusive order anchor without exposing it as a path or query."""
    if after is None:
        return None
    created_at, run_id = after
    run_match = _RUN_ID.fullmatch(run_id) if isinstance(run_id, str) else None
    if (
        not isinstance(meeting_id, str)
        or _MEETING_ID.fullmatch(meeting_id) is None
        or not isinstance(created_at, str)
        or _CREATED_AT.fullmatch(created_at) is None
        or run_match is None
    ):
        raise _invalid_cursor()
    payload = b"".join(
        (
            _MAGIC,
            _binding_hash(meeting_id.encode("ascii")),
            _scope_hash(scope),
            created_at.encode("ascii"),
            bytes.fromhex(run_match.group(1)),
        )
    )
    checksum = hashlib.sha256(b"narumi-processing-runs-cursor-v1\0" + payload).digest()[
        :_CHECKSUM_BYTES
    ]
    return base64.urlsafe_b64encode(payload + checksum).rstrip(b"=").decode("ascii")


def decode_processing_runs_cursor(
    cursor: str | None,
    *,
    meeting_id: str,
    scope: str | Sequence[str] | None,
) -> RunOrderKey | None:
    """Decode one cursor only after the handler has re-authorized its meeting and scope."""
    if cursor is None:
        return None
    if (
        not isinstance(cursor, str)
        or not 1 <= len(cursor) <= 256
        or re.fullmatch(r"[A-Za-z0-9_-]+", cursor) is None
        or not isinstance(meeting_id, str)
        or _MEETING_ID.fullmatch(meeting_id) is None
    ):
        raise _invalid_cursor()
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (ValueError, UnicodeError):
        raise _invalid_cursor() from None
    if len(value) != _CURSOR_BYTES:
        raise _invalid_cursor()
    payload, checksum = value[:_PAYLOAD_BYTES], value[_PAYLOAD_BYTES:]
    expected = hashlib.sha256(b"narumi-processing-runs-cursor-v1\0" + payload).digest()[
        :_CHECKSUM_BYTES
    ]
    meeting_start = len(_MAGIC)
    scope_start = meeting_start + _HASH_BYTES
    created_start = scope_start + _HASH_BYTES
    run_start = created_start + _TIMESTAMP_BYTES
    if (
        payload[:meeting_start] != _MAGIC
        or not hmac.compare_digest(
            payload[meeting_start:scope_start], _binding_hash(meeting_id.encode("ascii"))
        )
        or not hmac.compare_digest(payload[scope_start:created_start], _scope_hash(scope))
        or not hmac.compare_digest(checksum, expected)
    ):
        raise _invalid_cursor()
    try:
        created_at = payload[created_start:run_start].decode("ascii")
    except UnicodeDecodeError:
        raise _invalid_cursor() from None
    if _CREATED_AT.fullmatch(created_at) is None:
        raise _invalid_cursor()
    run_id = "run-" + payload[run_start:_PAYLOAD_BYTES].hex()
    if _RUN_ID.fullmatch(run_id) is None:
        raise _invalid_cursor()
    return created_at, run_id


def _scope_hash(scope: str | Sequence[str] | None) -> bytes:
    normalized = sorted(normalize_scope(scope))
    encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _binding_hash(encoded)


def _binding_hash(value: bytes) -> bytes:
    return hashlib.sha256(value).digest()


def _invalid_cursor() -> InvalidArgumentError:
    return InvalidArgumentError("The processing runs cursor is invalid or belongs to another query")
