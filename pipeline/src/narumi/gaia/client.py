"""Minimal MCP Streamable HTTP client for gaia-library (stdlib ``urllib`` only, no new deps).

Protocol: JSON-RPC 2.0 over POST — ``initialize`` → ``notifications/initialized`` once per
session, then ``tools/call``. The ``Mcp-Session-Id`` response header is kept and echoed on every
subsequent request; a 404 (session expired on the server) re-initializes once and retries.
Response bodies may be ``application/json`` (single message or batch) or ``text/event-stream``
(SSE, the ``data:`` payloads carry the JSON-RPC messages). This is a Python port of the parsing
approach in ``app/Sources/NarumiMenuBar/MCPClient.swift``.

gaia-library is optional (AGENTS.md): :meth:`GaiaClient.from_env` returns ``None`` when
``NARUMI_GAIA_URL`` is unset, and callers must treat ``None`` as "work from local data only".
A configured-but-unreachable server raises :class:`EngineUnavailableError` — never a silent
fallback.

The typed helpers (``search_context`` / ``get_glossary`` / ``resolve_speakers`` /
``propose_update``) follow the gaia-library contract sketch from the Notion design docs; the
gaia-library contract is still a draft, so they read the result tolerantly (missing keys become
empty results) and an unknown-tool error from the server is surfaced as
:class:`EngineUnavailableError` with the server's message.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from narumi.errors import EngineUnavailableError, ErrorCode, InvalidArgumentError, NarumiError

ENV_GAIA_URL = "NARUMI_GAIA_URL"
PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "narumi-pipeline"
CLIENT_VERSION = "1"
RPC_METHOD_NOT_FOUND = -32601
_ERROR_BODY_TAIL = 500
_UNKNOWN_TOOL_MARKERS = ("unknown tool", "not found", "unsupported")


class _HttpStatusError(Exception):
    """Internal: non-2xx HTTP response (converted to a NarumiError at the API boundary)."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body


class _RpcError(Exception):
    """Internal: JSON-RPC ``error`` response (converted at the API boundary)."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class GaiaClient:
    """One MCP session against a gaia-library Streamable HTTP endpoint."""

    def __init__(self, url: str, *, timeout: float = 30.0) -> None:
        if not isinstance(url, str) or not url.strip():
            raise InvalidArgumentError("gaia-library URL must be a non-empty string")
        self.url = url.strip()
        self.timeout = timeout
        self._session_id: str | None = None
        self._initialized = False
        self._next_id = 1

    @classmethod
    def from_env(cls) -> GaiaClient | None:
        """Client from ``$NARUMI_GAIA_URL``, or ``None`` when unset (gaia-library is optional)."""
        url = os.environ.get(ENV_GAIA_URL, "").strip()
        return cls(url) if url else None

    def reset(self) -> None:
        """Drop the session so the next call re-initializes."""
        self._session_id = None
        self._initialized = False

    # ------------------------------------------------------------------ tool calls
    def call(self, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """``tools/call`` returning the structured result payload as a dict.

        Prefers ``structuredContent``; falls back to parsing the joined text content as JSON,
        else returns ``{"text": ...}``. Raises :class:`EngineUnavailableError` when the server
        is unreachable or does not know the tool, and a :class:`NarumiError` carrying the
        server's structured error code when the tool itself reports an error.
        """
        params = {"name": tool, "arguments": dict(args or {})}
        try:
            result = self._call_once(params)
        except _HttpStatusError as err:
            if err.status != 404:
                raise self._http_error(err) from None
            # Session expired on the server side: re-initialize once and retry (MCPClient.swift).
            self.reset()
            try:
                result = self._call_once(params)
            except _HttpStatusError as retry_err:
                raise self._http_error(retry_err) from None
            except _RpcError as retry_err:
                raise _rpc_error(tool, retry_err) from None
        except _RpcError as err:
            raise _rpc_error(tool, err) from None
        return _unwrap_tool_result(tool, result)

    # Typed helpers (gaia-library contract sketch; results are read tolerantly).
    def search_context(
        self,
        query: str,
        *,
        engagement: str | None = None,
        scope: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """References relevant to ``query`` (each with ``system`` / ``uri`` / ``summary`` …)."""
        args: dict[str, Any] = {"query": query}
        if engagement is not None:
            args["engagement"] = engagement
        if scope is not None:
            args["scope"] = scope
        if limit is not None:
            args["limit"] = int(limit)
        result = self.call("search_context", args)
        refs = result.get("references", result.get("results"))
        return [ref for ref in refs if isinstance(ref, dict)] if isinstance(refs, list) else []

    def get_glossary(self, engagement: str | None = None) -> list[dict[str, Any]]:
        """Glossary entries (``term`` / ``aliases`` / ``note`` / optional ``kind``)."""
        args: dict[str, Any] = {}
        if engagement is not None:
            args["engagement"] = engagement
        result = self.call("get_glossary", args)
        terms = result.get("terms", result.get("glossary"))
        return [term for term in terms if isinstance(term, dict)] if isinstance(terms, list) else []

    def resolve_speakers(
        self, names: list[str], *, engagement: str | None = None
    ) -> dict[str, Any]:
        """Map name hints to known identities: ``{hint: {name, aliases, note?}}``."""
        args: dict[str, Any] = {"names": list(names)}
        if engagement is not None:
            args["engagement"] = engagement
        result = self.call("resolve_speakers", args)
        speakers = result.get("speakers")
        return speakers if isinstance(speakers, dict) else {}

    def propose_update(
        self,
        *,
        entity_type: str,
        patch: dict[str, Any],
        scope: str | None = None,
        provenance: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Queue an update proposal (the only write path into gaia-library, 絶対原則 5)."""
        args: dict[str, Any] = {"entity_type": entity_type, "patch": dict(patch)}
        if scope is not None:
            args["scope"] = scope
        if provenance is not None:
            args["provenance"] = provenance
        if request_id is not None:
            args["request_id"] = request_id
        return self.call("propose_update", args)

    # ------------------------------------------------------------------ session / transport
    def _call_once(self, params: dict[str, Any]) -> dict[str, Any]:
        self._ensure_initialized()
        return self._request("tools/call", params)

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            },
        )
        self._notify("notifications/initialized")
        self._initialized = True

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        rpc_id = self._next_id
        self._next_id += 1
        body = {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}
        data, content_type = self._post(body)
        message = _extract_response(data, content_type, rpc_id)
        error = message.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            raise _RpcError(
                int(code) if isinstance(code, int | float) else -1,
                str(error.get("message") or "unknown error"),
            )
        result = message.get("result")
        if not isinstance(result, dict):
            raise NarumiError(f"gaia-library returned no result for {method}")
        return result

    def _notify(self, method: str) -> None:
        self._post({"jsonrpc": "2.0", "method": method})

    def _post(self, body: dict[str, Any]) -> tuple[bytes, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        request = urllib.request.Request(
            self.url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                session_id = response.headers.get("Mcp-Session-Id")
                if session_id:
                    self._session_id = session_id
                content_type = (response.headers.get("Content-Type") or "").lower()
                return response.read(), content_type
        except urllib.error.HTTPError as err:
            tail = err.read().decode("utf-8", errors="replace")[-_ERROR_BODY_TAIL:]
            raise _HttpStatusError(err.code, tail) from None
        except (urllib.error.URLError, OSError) as err:
            reason = getattr(err, "reason", None) or err
            raise EngineUnavailableError(
                f"gaia-library unreachable at {self.url}: {reason}",
                details={"url": self.url},
            ) from None

    def _http_error(self, err: _HttpStatusError) -> NarumiError:
        message = f"gaia-library HTTP {err.status} at {self.url}: {err.body}"
        details = {"url": self.url, "status": err.status}
        if err.status >= 500 or err.status == 404:
            return EngineUnavailableError(message, details=details)
        return NarumiError(message, details=details)


# ---------------------------------------------------------------------------- response parsing
def _rpc_error(tool: str, err: _RpcError) -> NarumiError:
    """Unknown-tool errors → ``engine_unavailable`` with the server's message; else internal."""
    lowered = err.message.lower()
    details = {"tool": tool, "rpc_code": err.code}
    if err.code == RPC_METHOD_NOT_FOUND or any(m in lowered for m in _UNKNOWN_TOOL_MARKERS):
        return EngineUnavailableError(err.message, details=details)
    return NarumiError(f"gaia-library RPC error {err.code}: {err.message}", details=details)


def _unwrap_tool_result(tool: str, result: dict[str, Any]) -> dict[str, Any]:
    structured = result.get("structuredContent")
    payload = structured if isinstance(structured, dict) else None
    if result.get("isError"):
        error = payload.get("error") if payload else None
        if isinstance(error, dict):
            code_value = str(error.get("code") or "")
            try:
                code = ErrorCode(code_value)
            except ValueError:
                code = ErrorCode.INTERNAL
            raise NarumiError(
                str(error.get("message") or f"gaia-library tool {tool} failed"),
                code=code,
                details={
                    "tool": tool,
                    **({"gaia": error["details"]} if error.get("details") else {}),
                },
            )
        text = _joined_text(result)
        raise NarumiError(text or f"gaia-library tool {tool} failed", details={"tool": tool})
    if payload is not None:
        return payload
    text = _joined_text(result)
    if text:
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    return {"text": text}


def _joined_text(result: dict[str, Any]) -> str:
    texts: list[str] = []
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(str(item.get("text") or ""))
    return "\n".join(texts)


def _extract_response(data: bytes, content_type: str, expected_id: int) -> dict[str, Any]:
    """Pick the JSON-RPC response with ``expected_id`` from a JSON body or an SSE stream."""
    if "text/event-stream" in content_type:
        try:
            candidates: list[Any] = [json.loads(payload) for payload in _sse_events(data)]
        except ValueError as err:
            raise NarumiError(f"gaia-library sent an invalid SSE payload: {err}") from None
    else:
        try:
            node = json.loads(data)
        except ValueError as err:
            raise NarumiError(f"gaia-library returned invalid JSON: {err}") from None
        candidates = node if isinstance(node, list) else [node]
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate.get("id") == expected_id:
            return candidate
    raise NarumiError(f"gaia-library response is missing id {expected_id}")


def _sse_events(data: bytes) -> list[str]:
    """``data:`` payloads of every SSE event in the body (multi-line data joined with newlines)."""
    text = data.decode("utf-8", errors="replace")
    events: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            joined = "\n".join(current)
            if joined.strip():
                events.append(joined)
            current.clear()

    for raw_line in text.split("\n"):
        line = raw_line[:-1] if raw_line.endswith("\r") else raw_line
        if not line:
            flush()
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            value = line[len("data:") :]
            if value.startswith(" "):
                value = value[1:]
            current.append(value)
    flush()
    return events
