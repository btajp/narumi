"""Bounded JSON HTTP with no proxy, redirect, SDK or credential inheritance."""

from __future__ import annotations

import http.client
import json
import math
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, Literal

from narumi.errors import AuthenticationRequiredError, EngineUnavailableError
from narumi.providers.metadata.deadline import (
    DeadlineHTTPHandler,
    DeadlineHTTPSHandler,
    RequestDeadline,
)
from narumi.providers.metadata.tls import tls_context
from narumi.providers.metadata.validation import check_public_payload, invalid_metadata

MAX_RESPONSE_BYTES = 1_048_576
MAX_GENERATION_RESPONSE_BYTES = 8_388_608
DEFAULT_TIMEOUT = 10.0


class RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise invalid_metadata("redirect_rejected")


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
        timeout: float = DEFAULT_TIMEOUT,
        response_kind: Literal["metadata", "generation"] = "metadata",
    ) -> dict[str, Any]:
        if (
            not math.isfinite(timeout)
            or timeout <= 0
            or response_kind not in {"metadata", "generation"}
        ):
            raise invalid_metadata("invalid_http_options")
        limit = MAX_RESPONSE_BYTES if response_kind == "metadata" else MAX_GENERATION_RESPONSE_BYTES
        request_headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        request_headers.update(headers or {})
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        deadline = RequestDeadline(timeout, monotonic=self._monotonic)
        request.narumi_deadline = deadline
        secrets = tuple(
            value
            for key, value in request_headers.items()
            if key.lower() in {"x-api-key", "authorization"}
        )
        try:
            deadline.start()
            with self._opener.open(request, timeout=timeout) as response:
                deadline.remaining()
                if response.geturl() != url:
                    raise invalid_metadata("redirect_rejected")
                if response.status != 200:
                    self._status_error(response.status)
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip()
                if content_type != "application/json":
                    raise invalid_metadata("invalid_content_type")
                if response.headers.get("Content-Encoding", "identity") != "identity":
                    raise invalid_metadata("unsupported_content_encoding")
                length = response.headers.get("Content-Length")
                if length is not None and not 0 <= int(length) <= limit:
                    raise invalid_metadata("metadata_size_limit")
                raw = self._read_body(response, deadline, limit)
        except urllib.error.HTTPError as exc:
            exc.close()
            self._status_error(exc.code)
        except (urllib.error.URLError, http.client.HTTPException, OSError, TimeoutError):
            raise invalid_metadata("metadata_connection_failed") from None
        except ValueError:
            raise invalid_metadata("invalid_metadata") from None
        finally:
            deadline.close()
        try:
            body = json.loads(
                raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant
            )
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise invalid_metadata("invalid_metadata") from None
        if not isinstance(body, dict):
            raise invalid_metadata()
        if response_kind == "generation":
            # Ollama's deprecated token context can contain one item per input token.
            # It is not an artifact or a generation input in narumi; do not retain it
            # or apply the metadata object-count bound to this unused vector.
            body.pop("context", None)
        check_public_payload(body, secrets=secrets, reject_credentials=False)
        return body

    @staticmethod
    def _read_body(response, deadline: RequestDeadline, limit: int) -> bytes:
        chunks = []
        total = 0
        read = getattr(response, "read1", response.read)
        while total <= limit:
            deadline.remaining()
            chunk = read(limit + 1 - total)
            if not chunk:
                break
            total += len(chunk)
            chunks.append(chunk)
        deadline.remaining()
        if total > limit:
            raise invalid_metadata("metadata_size_limit")
        return b"".join(chunks)

    @staticmethod
    def _status_error(status: int) -> None:
        if status in {401, 403}:
            raise AuthenticationRequiredError(
                "Provider authentication failed", details={"reason": "credential_rejected"}
            ) from None
        raise EngineUnavailableError(
            "Provider metadata request failed",
            details={"reason": "metadata_http_error", "status": status},
        ) from None
