"""MCP server assembly: tools come from ``contracts/``, behaviour from ``handlers``.

``tools/list`` returns every contract tool with its exact inputSchema / outputSchema /
annotations. ``tools/call`` runs ``validate_input → (idempotent) handler → validate_output`` in a
worker thread and answers with structured content; any :class:`NarumiError` becomes the
contract's ``error_envelope`` with ``isError=true``, anything else an ``internal`` error.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import anyio
import mcp_types
from mcp.server import Server, ServerRequestContext
from narumi.contracts import ContractSet
from narumi.errors import AuthenticationRequiredError, ContractMismatchError, ErrorCode, NarumiError

from narumi_server import __version__
from narumi_server.context import ServerContext
from narumi_server.handlers.common import Handler, jsonable
from narumi_server.provider_tools import PROVIDER_TOOLS

logger = logging.getLogger(__name__)

SERVER_NAME = "narumi"
SERVER_TITLE = "narumi"
_GAIA_CONNECTION_TOOLS = frozenset(
    {"get_gaia_connection", "set_gaia_connection", "test_gaia_connection"}
)
INSTRUCTIONS = (
    "narumi records meetings locally (screen / mic / system audio as separate tracks) and "
    "generates minutes. Call get_server_info first; start_recording / stop_recording drive the "
    "recorder; stop_recording enqueues a process job whose progress get_job_status reports. "
    "Write tools take a client-generated request_id (idempotency key). Errors come back as "
    'structured content {"error": {"code", "message", "details"}} with isError=true.'
)


@dataclass(frozen=True)
class ToolOutcome:
    """What ``dispatch`` produced: a result (``is_error=False``) or an error envelope."""

    payload: dict[str, Any]
    is_error: bool

    def to_call_tool_result(self) -> mcp_types.CallToolResult:
        text = json.dumps(self.payload, ensure_ascii=False, indent=2)
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text=text)],
            structured_content=self.payload,
            is_error=self.is_error,
        )


def check_handlers(contracts: ContractSet, handlers: Mapping[str, Handler]) -> None:
    """Every contract tool needs a handler and vice versa (start-up invariant)."""
    missing = [tool for tool in contracts.tool_names() if tool not in handlers]
    extra = [name for name in handlers if name not in contracts]
    if missing or extra:
        raise ContractMismatchError(
            f"tool handlers and contracts disagree (missing handlers: {missing}, "
            f"handlers without contract: {extra})",
            details={"missing_handlers": missing, "unlisted_handlers": extra},
        )


def tool_definitions(contracts: ContractSet) -> list[mcp_types.Tool]:
    return [mcp_types.Tool.model_validate(contract.tool_definition()) for contract in contracts]


def dispatch(ctx: ServerContext, tool: str, arguments: Mapping[str, Any] | None) -> ToolOutcome:
    """Run one tool call end to end. Never raises: every failure is an error envelope."""
    contract = ctx.contracts.get(tool)
    sensitive = (
        tool in _GAIA_CONNECTION_TOOLS
        or tool in PROVIDER_TOOLS
        or (contract is not None and contract.has_write_only_input)
    )
    try:
        ctx.contracts.validate_input(tool, arguments)
        args = dict(arguments or {})
        contract = ctx.contracts[tool]
        if contract.has_write_only_input and "streamable-http" not in ctx.transports:
            raise AuthenticationRequiredError("Credentials require the authenticated server")
        handler = ctx.handlers.get(tool)
        if handler is None:
            raise ContractMismatchError(f"no handler for tool {tool!r}", details={"tool": tool})

        def call_handler() -> Mapping[str, Any]:
            result = handler(ctx, args)
            if sensitive or tool == "configure_recording_permission":
                # Sensitive and permission results must be checked before entering the
                # replay cache, even when optional output validation is disabled.
                if not isinstance(result, Mapping):
                    raise ContractMismatchError("validated tool returned a non-object result")
                ctx.contracts.validate_output(tool, jsonable(dict(result)))
            return result

        # Provider mutations keep their compare-and-set/replay ledger beside the connection
        # files. A catalog replay must not bypass their argument and credential checks.
        result = (
            call_handler()
            if tool in PROVIDER_TOOLS
            else ctx.idempotency.run(contract, args, call_handler)
        )
        if not isinstance(result, Mapping):
            raise ContractMismatchError(
                f"handler for {tool!r} returned {type(result).__name__}, expected an object",
                details={"tool": tool},
            )
        payload = jsonable(dict(result))
        if ctx.validate_output or sensitive:
            ctx.contracts.validate_output(tool, payload)
        return ToolOutcome(payload=payload, is_error=False)
    except NarumiError as exc:
        envelope = _sensitive_error(exc.code, tool) if sensitive else exc.to_payload()
        logger.warning(
            "tool %s failed: %s: %s", tool, envelope["error"]["code"], envelope["error"]["message"]
        )
    except Exception as exc:
        if sensitive:
            # traceback / exception repr can contain credentials, including malformed inputs.
            logger.error("tool %s raised an unexpected error", tool)
            envelope = _sensitive_error(ErrorCode.INTERNAL, tool)
        else:
            logger.exception("tool %s raised an unexpected %s", tool, type(exc).__name__)
            envelope = {
                "error": {
                    "code": str(ErrorCode.INTERNAL),
                    "message": f"{type(exc).__name__}: {exc}",
                    "details": {"exception": type(exc).__name__, "tool": tool},
                }
            }
    return ToolOutcome(
        payload=_checked_envelope(ctx, envelope, tool, sensitive=sensitive), is_error=True
    )


def _sensitive_error(code: ErrorCode, tool: str) -> dict[str, Any]:
    try:
        code = ErrorCode(code)
    except (TypeError, ValueError):
        code = ErrorCode.INTERNAL
    messages = {
        ErrorCode.INVALID_ARGUMENT: "Invalid connection settings or tool arguments",
        ErrorCode.ENGINE_UNAVAILABLE: "Gaia is unconfigured, unreachable, or authentication failed",
        ErrorCode.CONTRACT_MISMATCH: "Gaia connection tool or server response is incompatible",
        ErrorCode.AUTHENTICATION_REQUIRED: "An authenticated resident connection is required",
    }
    if tool in PROVIDER_TOOLS:
        messages = {
            ErrorCode.INVALID_ARGUMENT: "Invalid provider settings or tool arguments",
            ErrorCode.NOT_FOUND: "The requested connection or operation was not found",
            ErrorCode.BUSY: "The provider operation is still active",
            ErrorCode.CANCELLED: "The provider operation was cancelled",
            ErrorCode.CONFIGURATION_CONFLICT: "The connection changed; reload before saving",
            ErrorCode.AUTHENTICATION_REQUIRED: "Provider or resident authentication is required",
            ErrorCode.MODEL_UNAVAILABLE: "The requested model is unavailable",
            ErrorCode.ENGINE_UNAVAILABLE: "The provider runtime or connection is unavailable",
            ErrorCode.CONTRACT_MISMATCH: "The provider response does not match the contract",
            ErrorCode.POLICY_VIOLATION: "The provider operation is not permitted",
        }
    return {
        "error": {
            "code": str(code),
            "message": messages.get(code, "Connection settings could not be processed"),
            "details": {"tool": tool},
        }
    }


def _checked_envelope(
    ctx: ServerContext, envelope: dict[str, Any], tool: str, *, sensitive: bool = False
) -> dict[str, Any]:
    """Make sure what we return as an error really is a contract ``error_envelope``."""
    candidate = jsonable(envelope)
    try:
        ctx.contracts.validate_error_envelope(candidate)
    except ContractMismatchError as exc:
        if sensitive:
            logger.error("error envelope for %s violates the contract", tool)
            return _sensitive_error(ErrorCode.INTERNAL, tool)
        logger.error("error envelope for %s violates the contract: %s", tool, exc.message)
        error = candidate.get("error") if isinstance(candidate, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        candidate = {
            "error": {
                "code": str(ErrorCode.INTERNAL),
                "message": str(message or "internal error"),
                "details": {"tool": tool, "envelope_error": exc.message},
            }
        }
    return candidate


def build_server(ctx: ServerContext) -> Server[Any]:
    """Create the lowlevel MCP ``Server`` wired to ``ctx``; validates handlers vs contracts."""
    check_handlers(ctx.contracts, ctx.handlers)
    tools = tool_definitions(ctx.contracts)

    async def on_list_tools(
        _rctx: ServerRequestContext[Any], _params: mcp_types.PaginatedRequestParams | None
    ) -> mcp_types.ListToolsResult:
        return mcp_types.ListToolsResult(tools=list(tools))

    async def on_call_tool(
        _rctx: ServerRequestContext[Any], params: mcp_types.CallToolRequestParams
    ) -> mcp_types.CallToolResult:
        outcome = await anyio.to_thread.run_sync(dispatch, ctx, params.name, params.arguments)
        return outcome.to_call_tool_result()

    server: Server[Any] = Server(
        SERVER_NAME,
        version=__version__,
        title=SERVER_TITLE,
        instructions=INSTRUCTIONS,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )
    logger.info(
        "narumi MCP server ready: %d tools, contract %s",
        len(tools),
        ctx.contracts.contract_version,
    )
    return server
