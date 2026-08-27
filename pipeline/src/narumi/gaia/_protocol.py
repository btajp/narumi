"""Local-only, non-redirecting MCP HTTP transport and Gaia error adaptation."""

from __future__ import annotations

import http.client
import ipaddress
import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from narumi.errors import (
    ContractMismatchError,
    EngineUnavailableError,
    ErrorCode,
    InvalidArgumentError,
    NarumiError,
)

PROTOCOL_VERSION = "2025-06-18"
_ERROR_BODY_TAIL = 500
_ERROR_CODES = {
    "not_found": ErrorCode.NOT_FOUND,
    "scope_denied": ErrorCode.SCOPE_DENIED,
    "unauthorized": ErrorCode.SCOPE_DENIED,
    "invalid_params": ErrorCode.INVALID_ARGUMENT,
    "contract_mismatch": ErrorCode.CONTRACT_MISMATCH,
    "conflict": ErrorCode.INVALID_ARGUMENT,
    "busy": ErrorCode.BUSY,
    "not_implemented": ErrorCode.ENGINE_UNAVAILABLE,
    "internal": ErrorCode.INTERNAL,
}


class HttpStatusError(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class Transport:
    def __init__(self, url: str, *, api_key: str | None, timeout: float) -> None:
        validate_api_key(api_key)
        self._api_key = api_key
        self.url = local_url(url, api_key=api_key)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise InvalidArgumentError("gaia-library timeout must be a finite positive number")
        self.timeout = timeout
        self.session_id: str | None = None
        self._next_id = 1
        # Disable environmental proxies and every redirect, including loopback redirects:
        # neither a proxy nor a second local service should ever receive this credential.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        rpc_id = self._next_id
        self._next_id += 1
        data, content_type = self.post(
            {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}
        )
        message = extract_response(data, content_type, rpc_id)
        error = message.get("error")
        if "error" in message:
            if (
                "result" in message
                or not isinstance(error, dict)
                or type(error.get("code")) is not int
                or not isinstance(error.get("message"), str)
            ):
                raise ContractMismatchError("gaia-library returned an invalid JSON-RPC error")
            code = error.get("code")
            raise RpcError(
                code,
                error["message"],
                error.get("data"),
            )
        result = message.get("result")
        if not isinstance(result, dict):
            raise ContractMismatchError(f"gaia-library returned no result for {method}")
        return result

    def post(self, body: dict[str, Any]) -> tuple[bytes, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key}"
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        try:
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise InvalidArgumentError("gaia-library arguments must be valid JSON") from None
        request = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                session_id = response.headers.get("Mcp-Session-Id")
                if session_id:
                    if any(not 33 <= ord(char) <= 126 for char in session_id):
                        raise ContractMismatchError(
                            "gaia-library returned an invalid MCP session ID"
                        )
                    self.session_id = session_id
                content_type = (response.headers.get("Content-Type") or "").lower()
                return response.read(), content_type
        except urllib.error.HTTPError as err:
            try:
                with err:
                    # Redact before truncation so a key spanning the boundary cannot leak.
                    error_body = self.scrub(err.read().decode("utf-8", errors="replace"))
            except (OSError, http.client.HTTPException):
                error_body = ""
            raise HttpStatusError(err.code, error_body[-_ERROR_BODY_TAIL:]) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as err:
            reason = self.scrub(str(getattr(err, "reason", None) or err))
            raise EngineUnavailableError(
                f"gaia-library unreachable at {self.url}: {reason}",
                details={"url": self.url},
            ) from None

    def http_error(self, err: HttpStatusError) -> NarumiError:
        message = f"gaia-library HTTP {err.status} at {self.url}: {err.body}"
        details: dict[str, Any] = {"url": self.url, "status": err.status}
        if err.status >= 500 or err.status == 404:
            return EngineUnavailableError(message, details=details)
        code = ErrorCode.INTERNAL
        if err.status in (401, 403):
            code = ErrorCode.SCOPE_DENIED
            details["gaia_code"] = "unauthorized" if err.status == 401 else "scope_denied"
        elif err.status == 429:
            code = ErrorCode.BUSY
        return NarumiError(message, code=code, details=details)

    def scrub(self, value: Any) -> Any:
        if isinstance(value, str):
            if self._api_key:
                for secret in (self._api_key, urllib.parse.quote(self._api_key, safe="")):
                    value = value.replace(secret, "[REDACTED]")
            return re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [REDACTED]", value)
        if isinstance(value, dict):
            return {self.scrub(key): self.scrub(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.scrub(item) for item in value]
        return value

    def scrub_error(self, err: NarumiError) -> NarumiError:
        err.message = self.scrub(err.message)
        err.args = (err.message,)
        err.details = self.scrub(err.details)
        return err


def validate_api_key(api_key: str | None) -> None:
    """A supplied key must be a non-empty RFC 6750 bearer token; None means no header."""
    if api_key is not None and (
        not isinstance(api_key, str) or re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", api_key) is None
    ):
        raise InvalidArgumentError("gaia-library API key must be a non-empty bearer token")


def local_url(url: str, *, api_key: str | None = None) -> str:
    """Accept only HTTP loopback URLs; canonicalize localhost without consulting DNS."""
    message = "gaia-library URL must be a loopback HTTP endpoint without credentials or query"
    if not isinstance(url, str) or not url.strip():
        raise InvalidArgumentError(message)
    if any(ord(char) < 32 or ord(char) >= 127 for char in url):
        raise InvalidArgumentError(message)
    url = url.strip()
    if any(ord(char) <= 32 or ord(char) >= 127 for char in url) or any(
        char in url for char in "\\?#"
    ):
        raise InvalidArgumentError(message)
    if api_key and api_key in urllib.parse.unquote(url):
        raise InvalidArgumentError("gaia-library API key must not appear in its URL")
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        port = parts.port
        if (
            parts.scheme != "http"
            or not host
            or parts.username is not None
            or "%" in parts.netloc
            or (port is not None and port < 1)
        ):
            raise ValueError
        address = ipaddress.ip_address("127.0.0.1" if host == "localhost" else host)
        if not address.is_loopback or (
            address.version == 6 and address != ipaddress.IPv6Address("::1")
        ):
            raise ValueError
        netloc = f"[{address.compressed}]" if address.version == 6 else address.compressed
        if port is not None:
            netloc += f":{port}"
        return urllib.parse.urlunsplit(("http", netloc, parts.path or "/", "", ""))
    except ValueError:
        raise InvalidArgumentError(message) from None


def _structured_error(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    nested = value.get("error")
    if isinstance(nested, dict):
        value = nested
    return value if isinstance(value.get("code"), str) else None


def product_error(tool: str, error: dict[str, Any], **extra: Any) -> NarumiError:
    gaia_code = error["code"]
    details = {"tool": tool, "gaia_code": gaia_code, **extra}
    if error.get("details") is not None:
        details["gaia"] = error["details"]
    code = _ERROR_CODES.get(gaia_code, ErrorCode.INTERNAL)
    message = str(error.get("message") or f"gaia-library tool {tool} failed")
    if code == ErrorCode.ENGINE_UNAVAILABLE:
        return EngineUnavailableError(message, details=details)
    return NarumiError(message, code=code, details=details)


def rpc_error(tool: str, err: RpcError) -> NarumiError:
    error = _structured_error(err.data)
    details = {"tool": tool, "rpc_code": err.code}
    # Gaia uses -32602 + data.code=not_found for unknown tools. A business not_found
    # must retain that code, not be misreported as a missing capability.
    if err.code == -32601 or re.match(r"\s*unknown\s+tool\b", err.message, re.IGNORECASE):
        if error:
            details.update({"gaia_code": error["code"], "gaia": error.get("details")})
        return EngineUnavailableError(err.message, details=details)
    if error:
        return product_error(tool, error, rpc_code=err.code)
    code = {-32602: ErrorCode.INVALID_ARGUMENT, -32001: ErrorCode.SCOPE_DENIED}.get(
        err.code, ErrorCode.INTERNAL
    )
    return NarumiError(
        f"gaia-library RPC error {err.code}: {err.message}", code=code, details=details
    )


def unwrap_tool_result(tool: str, result: dict[str, Any]) -> dict[str, Any]:
    structured = result.get("structuredContent")
    if "structuredContent" in result and not isinstance(structured, dict):
        raise ContractMismatchError(f"gaia-library returned invalid structuredContent for {tool}")
    if "isError" in result and not isinstance(result["isError"], bool):
        raise ContractMismatchError(f"gaia-library returned invalid isError for {tool}")
    text = joined_text(result)
    payload = structured
    if payload is None and text:
        try:
            parsed = decode_json(text)
        except (ValueError, RecursionError):
            parsed = None
        if isinstance(parsed, dict):
            payload = parsed
    if result.get("isError") or (payload is not None and isinstance(payload.get("error"), dict)):
        error = _structured_error(payload)
        if error:
            raise product_error(tool, error)
        raise NarumiError(text or f"gaia-library tool {tool} failed", details={"tool": tool})
    return payload if payload is not None else {"text": text}


def joined_text(result: dict[str, Any]) -> str:
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(
        item["text"]
        for item in content
        if isinstance(item, dict)
        and item.get("type") == "text"
        and isinstance(item.get("text"), str)
    )


def extract_response(data: bytes, content_type: str, expected_id: int) -> dict[str, Any]:
    try:
        if "text/event-stream" in content_type:
            candidates = [decode_json(payload) for payload in sse_events(data)]
        else:
            node = decode_json(data)
            candidates = node if isinstance(node, list) else [node]
    except (ValueError, RecursionError):
        raise ContractMismatchError("gaia-library returned invalid JSON or SSE") from None
    for candidate in candidates:
        if (
            isinstance(candidate, dict)
            and type(candidate.get("id")) is int
            and candidate["id"] == expected_id
        ):
            if candidate.get("jsonrpc") != "2.0":
                raise ContractMismatchError("gaia-library returned an unsupported JSON-RPC version")
            return candidate
    raise ContractMismatchError(f"gaia-library response is missing id {expected_id}")


def decode_json(data: bytes | str) -> Any:
    def reject_constant(_: str) -> None:
        raise ValueError("non-finite numbers are not valid JSON")

    return json.loads(data, parse_constant=reject_constant)


def sse_events(data: bytes) -> list[str]:
    events: list[str] = []
    current: list[str] = []
    for line in [*data.decode("utf-8", errors="replace").splitlines(), ""]:
        if not line:
            if current and "\n".join(current).strip():
                events.append("\n".join(current))
            current.clear()
        elif line.startswith("data:"):
            value = line[len("data:") :]
            current.append(value[1:] if value.startswith(" ") else value)
    return events
