"""Bounded JSON HTTP with no proxy, redirect, SDK or credential inheritance."""

from __future__ import annotations

import http.client
import json
import math
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, Literal

from narumi.errors import AuthenticationRequiredError, CancelledError, EngineUnavailableError
from narumi.providers.metadata.deadline import (
    DeadlineHTTPHandler,
    DeadlineHTTPSHandler,
    RequestDeadline,
)
from narumi.providers.metadata.tls import tls_context
from narumi.providers.metadata.validation import (
    MAX_PUBLIC_PAYLOAD_NODES,
    check_public_payload,
    invalid_metadata,
)

MAX_RESPONSE_BYTES = 1_048_576
MAX_GENERATION_RESPONSE_BYTES = 8_388_608
MAX_MULTIPART_REQUEST_BYTES = 24_000_000 + 65_536
DEFAULT_TIMEOUT = 10.0
_OUTCOME_RESPONSE_KINDS = {"generation", "transcription"}
_TRANSCRIPTION_KNOWN_REFUSALS = {400, 401, 403, 413, 429}
_MULTIPART_CONTENT_TYPE = re.compile(r"multipart/form-data; boundary=([A-Za-z0-9_-]{1,70})\Z")
_HEADER_NAME = re.compile(r"[A-Za-z][A-Za-z0-9-]*\Z")
_METADATA_REASONS = {
    "invalid_metadata",
    "redirect_rejected",
    "invalid_content_type",
    "unsupported_content_encoding",
    "metadata_size_limit",
    "metadata_structure_limit",
    "unsafe_metadata",
}


class _HTTPStatus(Exception):
    def __init__(self, status: int):
        self.status = status


def _unknown_outcome() -> EngineUnavailableError:
    return EngineUnavailableError(
        "Provider generation outcome is unknown",
        details={"reason": "provider_generation_outcome_unknown", "outcome_unknown": True},
    )


def _cancelled(deadline: RequestDeadline, *, response_kind: str) -> CancelledError:
    if response_kind in _OUTCOME_RESPONSE_KINDS and deadline.request_started:
        return CancelledError(
            "Provider generation was cancelled after transmission; outcome is unknown",
            details={"reason": "provider_generation_outcome_unknown", "outcome_unknown": True},
        )
    return CancelledError(
        "Provider generation was cancelled", details={"reason": "provider_generation_cancelled"}
    )


def _response_secrets(headers: dict[str, str]) -> tuple[str, ...]:
    secrets = []
    for key, value in headers.items():
        if key.lower() in {"x-api-key", "authorization"}:
            secrets.append(value)
        if key.lower() == "authorization":
            parts = value.split(None, 1)
            if len(parts) == 2 and parts[0].lower() == "bearer":
                secrets.append(parts[1].strip())
    return tuple(secrets)


def _multipart_headers(
    method: str, headers: dict[str, str] | None, body: bytes, response_kind: str
) -> dict[str, str]:
    """Validate the fixed raw upload framing before any transport can start."""
    if (
        method != "POST"
        or response_kind != "transcription"
        or type(body) is not bytes
        or not 0 < len(body) <= MAX_MULTIPART_REQUEST_BYTES
        or (headers is not None and not isinstance(headers, dict))
    ):
        raise invalid_metadata("invalid_http_options")
    normalized: dict[str, str] = {}
    for name, value in (headers or {}).items():
        if (
            not isinstance(name, str)
            or _HEADER_NAME.fullmatch(name) is None
            or not isinstance(value, str)
            or not value.isascii()
            or any(ord(char) < 32 or ord(char) > 126 for char in value)
            or name.lower() in normalized
        ):
            raise invalid_metadata("invalid_http_options")
        normalized[name.lower()] = value
    content_type = _MULTIPART_CONTENT_TYPE.fullmatch(normalized.get("content-type", ""))
    if (
        content_type is None
        or "transfer-encoding" in normalized
        or normalized.get("content-length", str(len(body))) != str(len(body))
    ):
        raise invalid_metadata("invalid_http_options")
    boundary = content_type.group(1).encode("ascii")
    if not body.startswith(b"--" + boundary + b"\r\n") or not body.endswith(
        b"\r\n--" + boundary + b"--\r\n"
    ):
        raise invalid_metadata("invalid_http_options")
    normalized.setdefault("accept", "application/json")
    normalized.setdefault("accept-encoding", "identity")
    normalized["content-length"] = str(len(body))
    return normalized


class RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib closes this response only after redirect_request returns. A
        # rejection must close its file reference as well as the tracked socket.
        try:
            if fp is not None:
                fp.close()
        except Exception:
            pass
        raise invalid_metadata("redirect_rejected") from None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


class JSONHTTPClient:
    """Only callers supply headers; upstream errors never escape as public text."""

    def __init__(self, *, opener=None, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            RejectRedirects(),
            DeadlineHTTPHandler(),
            DeadlineHTTPSHandler(context=tls_context()),
        )
        self._monotonic = monotonic

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        raw_body: bytes | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        response_kind: Literal["metadata", "generation", "transcription"] = "metadata",
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if (
            not math.isfinite(timeout)
            or timeout <= 0
            or response_kind not in {"metadata", "generation", "transcription"}
            or (raw_body is not None and payload is not None)
            or (response_kind == "transcription" and raw_body is None)
        ):
            raise invalid_metadata("invalid_http_options")
        limit = MAX_RESPONSE_BYTES if response_kind == "metadata" else MAX_GENERATION_RESPONSE_BYTES
        if raw_body is not None:
            request_headers = _multipart_headers(method, headers, raw_body, response_kind)
            data = raw_body
        else:
            request_headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
            request_headers.update(headers or {})
            if payload is not None:
                request_headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        deadline = RequestDeadline(
            timeout,
            monotonic=self._monotonic,
            should_cancel=should_cancel,
            interruptible_write=response_kind == "transcription",
        )
        request.narumi_deadline = deadline
        secrets = _response_secrets(request_headers)
        try:
            deadline.start()
            handler = DeadlineHTTPSHandler if request.type == "https" else DeadlineHTTPHandler
            if not any(isinstance(item, handler) for item in getattr(self._opener, "handlers", ())):
                # Injected openers without the deadline handler cannot report
                # their write boundary. Conservatively regard open() as sent.
                deadline.mark_request_started()
            with self._opener.open(request, timeout=timeout) as response:
                deadline.remaining()
                if response.geturl() != url:
                    raise invalid_metadata("redirect_rejected")
                if response.status != 200:
                    raise _HTTPStatus(response.status)
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip()
                if content_type != "application/json":
                    raise invalid_metadata("invalid_content_type")
                if response.headers.get("Content-Encoding", "identity") != "identity":
                    raise invalid_metadata("unsupported_content_encoding")
                length = response.headers.get("Content-Length")
                expected_length = int(length) if length is not None else None
                if expected_length is not None and not 0 <= expected_length <= limit:
                    raise invalid_metadata("metadata_size_limit")
                raw = self._read_body(response, deadline, limit, expected_length=expected_length)
            body = json.loads(
                raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant
            )
            if not isinstance(body, dict):
                raise invalid_metadata()
            if response_kind == "generation":
                # Ollama's deprecated token context is unused and may contain
                # more items than the metadata object-count bound permits.
                body.pop("context", None)
            check_public_payload(
                body,
                secrets=secrets,
                reject_credentials=False,
                max_nodes=MAX_PUBLIC_PAYLOAD_NODES if response_kind == "transcription" else 20_000,
            )
            deadline.remaining()
            return body
        except urllib.error.HTTPError as exc:
            # Never parse upstream error bodies, including authentication errors.
            try:
                exc.close()
            except Exception:
                pass
            self._status_error(exc.code, response_kind=response_kind)
        except _HTTPStatus as exc:
            self._status_error(exc.status, response_kind=response_kind)
        except Exception as exc:
            if deadline.cancelled:
                raise _cancelled(deadline, response_kind=response_kind) from None
            if response_kind in _OUTCOME_RESPONSE_KINDS and deadline.request_started:
                raise _unknown_outcome() from None
            reason = "invalid_metadata"
            if isinstance(exc, (urllib.error.URLError, http.client.HTTPException, OSError)):
                reason = "metadata_connection_failed"
            elif isinstance(exc, EngineUnavailableError):
                observed = exc.details.get("reason") if isinstance(exc.details, dict) else None
                if isinstance(observed, str) and observed in _METADATA_REASONS:
                    reason = observed
            raise invalid_metadata(reason) from None
        finally:
            deadline.close()

    @staticmethod
    def _read_body(
        response, deadline: RequestDeadline, limit: int, *, expected_length: int | None = None
    ) -> bytes:
        chunks = []
        total = 0
        read_limit = limit if expected_length is None else expected_length
        read = getattr(response, "read1", response.read)
        while total <= read_limit:
            deadline.remaining()
            chunk = read(read_limit + 1 - total)
            if not chunk:
                break
            total += len(chunk)
            chunks.append(chunk)
        deadline.remaining()
        if total > limit:
            raise invalid_metadata("metadata_size_limit")
        # HTTPResponse.read1() permits premature EOF even when Content-Length
        # promises more bytes. Syntactically complete JSON is not proof that the
        # framed response finished; never accept that partial provider outcome.
        if expected_length is not None and total != expected_length:
            raise invalid_metadata()
        return b"".join(chunks)

    @staticmethod
    def _status_error(status: int, *, response_kind: str = "metadata") -> None:
        if type(status) is not int or not 100 <= status <= 599:
            if response_kind in _OUTCOME_RESPONSE_KINDS:
                raise _unknown_outcome() from None
            raise invalid_metadata() from None
        if status in {401, 403}:
            raise AuthenticationRequiredError(
                "Provider authentication failed", details={"reason": "credential_rejected"}
            ) from None
        if response_kind == "generation" and not 400 <= status < 500:
            raise _unknown_outcome() from None
        if response_kind == "transcription" and status not in _TRANSCRIPTION_KNOWN_REFUSALS:
            raise _unknown_outcome() from None
        raise EngineUnavailableError(
            "Provider generation request failed"
            if response_kind in _OUTCOME_RESPONSE_KINDS
            else "Provider metadata request failed",
            details={"reason": "metadata_http_error", "status": status},
        ) from None
