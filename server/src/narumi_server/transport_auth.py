"""Authenticate every HTTP method before the MCP application can consume its body."""

from __future__ import annotations

import hmac
import json
from typing import Any
from urllib.parse import urlsplit

import mcp_types
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_REQUEST_BYTES = 4 * 1024 * 1024
_RPC_ERROR_MESSAGE = "The MCP request could not be processed"


def safe_rpc_message(message: mcp_types.JSONRPCMessage) -> mcp_types.JSONRPCMessage:
    """Transport/parser errors can contain input values; tool result envelopes are untouched."""
    if isinstance(message, mcp_types.JSONRPCError):
        return message.model_copy(
            update={
                "error": mcp_types.ErrorData(code=message.error.code, message=_RPC_ERROR_MESSAGE)
            }
        )
    return message


class LocalAuthenticationMiddleware:
    def __init__(self, app: ASGIApp, *, url: str, client_token: str) -> None:
        self.app = app
        parsed = urlsplit(url)
        self.authority = parsed.netloc.encode("ascii")
        self.origin = f"https://{parsed.netloc}".encode("ascii")
        self.path = parsed.path
        self._authorization = f"Bearer {client_token}".encode("ascii")

    def _authorized(self, scope: Scope) -> bool:
        if scope.get("scheme") != "https" or scope.get("path") != self.path:
            return False
        values: dict[bytes, list[bytes]] = {}
        for name, value in scope.get("headers", []):
            name = name.lower()
            if name == b"forwarded" or name.startswith(b"x-forwarded-"):
                return False
            values.setdefault(name, []).append(value)
        if values.get(b"host") != [self.authority]:
            return False
        if b"origin" in values and values[b"origin"] != [self.origin]:
            return False
        credentials = values.get(b"authorization", [])
        return len(credentials) == 1 and hmac.compare_digest(credentials[0], self._authorization)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            await send({"type": "websocket.close", "code": 1008})
            return
        if not self._authorized(scope):
            await JSONResponse(
                {"error": "Authenticated local TLS transport is required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
            )(scope, receive, send)
            return
        if scope["method"] not in {"GET", "POST", "DELETE"}:
            await JSONResponse({"error": "Unsupported method"}, status_code=405)(
                scope, receive, send
            )
            return
        safe_receive = receive
        if scope["method"] == "POST":
            body = bytearray()
            while True:
                event = await receive()
                if event["type"] == "http.disconnect":
                    return
                body.extend(event.get("body", b""))
                if len(body) > MAX_REQUEST_BYTES:
                    await JSONResponse({"error": "Request is too large"}, status_code=413)(
                        scope, receive, send
                    )
                    return
                if not event.get("more_body", False):
                    break
            try:
                request = mcp_types.jsonrpc_message_adapter.validate_json(bytes(body))
                if isinstance(request, mcp_types.JSONRPCRequest):
                    # The legacy MCP parser returns ValidationError input values on the wire.
                    # Validate the same protocol models here and keep failures value-free.
                    mcp_types.client_request_adapter.validate_python(
                        request.model_dump(by_alias=True, exclude_none=True)
                    )
                elif isinstance(request, mcp_types.JSONRPCNotification):
                    mcp_types.client_notification_adapter.validate_python(
                        request.model_dump(by_alias=True, exclude_none=True)
                    )
            except Exception:
                await JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32600, "message": "Invalid MCP request"},
                    },
                    status_code=400,
                )(scope, receive, send)
                return
            delivered = False

            async def replay_body() -> Message:
                nonlocal delivered
                if delivered:
                    return await receive()
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}

            safe_receive = replay_body
        await self.app(scope, safe_receive, _SafeResponse(send))


class _SafeResponse:
    """Suppress lower-level HTTP/parser errors without modifying valid MCP tool results."""

    def __init__(self, send: Send) -> None:
        self.send = send
        self.start: Message | None = None
        self.buffer: list[bytes] = []
        self.is_json = False
        self.is_error = False
        self.sent_error = False

    async def __call__(self, event: Message) -> None:
        if event["type"] == "http.response.start":
            self.start = dict(event)
            headers = dict(event.get("headers", []))
            self.is_json = headers.get(b"content-type", b"").startswith(b"application/json")
            self.is_error = event["status"] >= 400
            if not self.is_json and not self.is_error:
                await self.send(event)
            return
        if event["type"] != "http.response.body" or self.start is None:
            await self.send(event)
            return
        if self.is_error:
            if not self.sent_error:
                self.sent_error = True
                await self._send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32600, "message": _RPC_ERROR_MESSAGE},
                    }
                )
            return
        if self.is_json:
            self.buffer.append(event.get("body", b""))
            if event.get("more_body", False):
                return
            raw = b"".join(self.buffer)
            try:
                document = json.loads(raw)
            except (ValueError, UnicodeError):
                document = None
            if isinstance(document, dict) and isinstance(document.get("error"), dict):
                code = document["error"].get("code", -32603)
                document["error"] = {
                    "code": code if type(code) is int else -32603,
                    "message": _RPC_ERROR_MESSAGE,
                }
                await self._send_json(document)
            else:
                await self.send(self.start)
                await self.send({"type": "http.response.body", "body": raw})
            return
        await self.send(event)

    async def _send_json(self, document: dict[str, Any]) -> None:
        assert self.start is not None
        raw = json.dumps(document, separators=(",", ":")).encode("utf-8")
        headers = [
            (name, value)
            for name, value in self.start.get("headers", [])
            if name not in {b"content-type", b"content-length"}
        ]
        self.start["headers"] = [
            *headers,
            (b"content-type", b"application/json"),
            (b"content-length", str(len(raw)).encode("ascii")),
        ]
        await self.send(self.start)
        await self.send({"type": "http.response.body", "body": raw})
