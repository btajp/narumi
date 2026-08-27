"""Optional Gaia contract-v1 client over authenticated local MCP Streamable HTTP.

Typed methods return the real Gaia response dictionaries, including entities/facts/refs,
vocabulary_hints, speaker resolution statuses, and proposal IDs. Every typed operation
checks ``get_server_info`` for contract-major compatibility and advertised capabilities.
The check is repeated after the single supported 404/session-expiry retry.

``get_server_info(refresh=True)`` and ``require_capabilities(*tool_names)`` are public
read-only connection checks for the app/server integration. Neither mutates data.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from narumi.errors import (
    ContractMismatchError,
    EngineUnavailableError,
    InvalidArgumentError,
    NarumiError,
)
from narumi.gaia._models import validate_response
from narumi.gaia._protocol import (
    PROTOCOL_VERSION,
    HttpStatusError,
    RpcError,
    Transport,
    rpc_error,
    unwrap_tool_result,
)

ENV_GAIA_URL = "NARUMI_GAIA_URL"
ENV_GAIA_API_KEY = "NARUMI_GAIA_API_KEY"
CLIENT_NAME = "narumi-pipeline"
CLIENT_VERSION = "1"
SUPPORTED_CONTRACT_MAJOR = 1
Scope = str | list[str]
_SEARCH_TYPES = {"person", "organization", "engagement", "entity", "interaction", "glossary"}
_PROPOSAL_TYPES = _SEARCH_TYPES | {"fact", "ref"}
_SEMVER = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
)


class GaiaClient:
    """One authenticated MCP session; no configured endpoint means no Gaia dependency."""

    def __init__(self, url: str, *, api_key: str | None = None, timeout: float = 30.0) -> None:
        self._transport = Transport(url, api_key=api_key, timeout=timeout)
        self.url = self._transport.url
        self.timeout = timeout
        self._initialized = False
        self._server_info: dict[str, Any] | None = None

    @classmethod
    def from_env(cls) -> GaiaClient | None:
        """Use saved app settings, then NARUMI_GAIA_URL/API_KEY; None means no endpoint.

        The historical name remains compatible with pipeline callers. Settings construct
        GaiaClient directly, so this lazy import does not create an import cycle.
        """
        from narumi.gaia.settings import get_default_gaia_client

        return get_default_gaia_client()

    def reset(self) -> None:
        """Drop both the session and its metadata; the next operation checks them anew."""
        self._transport.session_id = None
        self._initialized = False
        self._server_info = None

    def call(self, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Low-level MCP call; product code should prefer the validated typed helpers.

        Structured Gaia RPC/tool errors retain their original code in details.gaia_code.
        Gaia-only unauthorized/conflict codes map to scope_denied/invalid_argument;
        unknown tools and not_implemented map to engine_unavailable.
        """
        return self._perform(tool, args, typed=False)

    def get_server_info(self, *, refresh: bool = False) -> dict[str, Any]:
        """Return validated, compatible metadata (cached per session unless refreshed)."""
        if refresh:
            self._server_info = None
        if self._server_info is None:
            self._accept_server_info(self._perform("get_server_info", {}, typed=False))
        return self._transport.scrub(copy.deepcopy(self._server_info))

    def require_capabilities(self, *tool_names: str) -> dict[str, Any]:
        """Validate compatibility and required tool availability without invoking those tools."""
        info = self.get_server_info()
        if any(not isinstance(name, str) or not name.strip() for name in tool_names):
            raise InvalidArgumentError("required Gaia tool names must be non-empty strings")
        try:
            self._check_capabilities(self._server_info, tool_names)
        except NarumiError as err:
            raise self._transport.scrub_error(err) from None
        return info

    def search_context(
        self,
        query: str,
        *,
        scope: Scope | None = None,
        limit: int | None = None,
        types: list[str] | None = None,
    ) -> dict[str, Any]:
        """Return query/scopes/entities/glossary/interactions/hints from Gaia's search."""
        if not isinstance(query, str):
            raise InvalidArgumentError("query must be a string")
        args = _scope_args(scope)
        args["query"] = query
        if limit is not None:
            if type(limit) is not int or not 1 <= limit <= 50:
                raise InvalidArgumentError("limit must be an integer between 1 and 50")
            args["limit"] = limit
        if types is not None:
            if not isinstance(types, list) or any(
                not isinstance(value, str) or value not in _SEARCH_TYPES for value in types
            ):
                raise InvalidArgumentError("types must contain supported Gaia search types")
            args["types"] = list(types)
        return self._perform("search_context", args, typed=True)

    def get_engagement(self, name: str, *, scope: Scope | None = None) -> dict[str, Any]:
        """Resolve an engagement NAME within the explicit scope; never coerce it to an ID."""
        if not isinstance(name, str) or not name.strip():
            raise InvalidArgumentError("engagement name must be a non-empty string")
        return self._perform("get_engagement", {**_scope_args(scope), "name": name}, typed=True)

    def get_glossary(
        self,
        engagement: str | None = None,
        *,
        scope: Scope | None = None,
        engagement_id: int | None = None,
    ) -> dict[str, Any]:
        """Return terms and vocabulary_hints, optionally resolving a scoped engagement name."""
        args = self._engagement_args(engagement, engagement_id, scope)
        return self._perform("get_glossary", args, typed=True)

    def resolve_speakers(
        self,
        names: list[str],
        *,
        engagement: str | None = None,
        scope: Scope | None = None,
        engagement_id: int | None = None,
    ) -> dict[str, Any]:
        """Return results with matched/ambiguous/unmatched status and explicit candidates."""
        if not isinstance(names, list) or not names or any(not isinstance(n, str) for n in names):
            raise InvalidArgumentError("display names must be a non-empty list of strings")
        args = self._engagement_args(engagement, engagement_id, scope)
        result = self._perform(
            "resolve_speakers", {**args, "display_names": list(names)}, typed=True
        )
        if [item["input"] for item in result["results"]] != names:
            raise ContractMismatchError(
                "gaia-library speaker results do not correspond to the requested display names",
                details={"tool": "resolve_speakers"},
            )
        return result

    def propose_update(
        self,
        *,
        target_type: str,
        action: str,
        patch: dict[str, Any],
        kind: str,
        request_id: str,
        scope: str | None = None,
        provenance: dict[str, Any] | None = None,
        target_id: int | None = None,
    ) -> dict[str, Any]:
        """Queue an idempotent proposal; approval is exclusively a human-side action."""
        if not isinstance(target_type, str) or target_type not in _PROPOSAL_TYPES:
            raise InvalidArgumentError("unsupported Gaia proposal target_type")
        if action not in ("insert", "update", "supersede") or kind not in ("fact", "inference"):
            raise InvalidArgumentError("unsupported Gaia proposal action or kind")
        if not isinstance(patch, dict):
            raise InvalidArgumentError("proposal patch must be an object")
        if not isinstance(request_id, str) or len(request_id) < 8:
            raise InvalidArgumentError("request_id must contain at least 8 characters")
        try:
            if len(request_id.encode("utf-8")) > 256:
                raise InvalidArgumentError("request_id must be at most 256 UTF-8 bytes")
        except UnicodeError:
            raise InvalidArgumentError("request_id must be valid UTF-8") from None
        if (action == "insert" and target_id is not None) or (
            action != "insert" and type(target_id) is not int
        ):
            raise InvalidArgumentError("target_id is required only for update or supersede")
        if action == "supersede" and target_type != "fact":
            raise InvalidArgumentError("supersede is only supported for facts")
        if scope is not None and not isinstance(scope, str):
            raise InvalidArgumentError("proposal scope must be a single string")
        args = {
            **_scope_args(scope),
            "target_type": target_type,
            "action": action,
            "patch": dict(patch),
            "kind": kind,
            "request_id": request_id,
        }
        if target_id is not None:
            args["target_id"] = target_id
        if provenance is not None:
            _validate_provenance(provenance)
            args["provenance"] = dict(provenance)
        return self._perform("propose_update", args, typed=True)

    def _engagement_args(
        self, name: str | None, engagement_id: int | None, scope: Scope | None
    ) -> dict[str, Any]:
        args = _scope_args(scope)
        if name is not None:
            if engagement_id is not None:
                raise InvalidArgumentError("specify engagement name or engagement_id, not both")
            engagement_id = self.get_engagement(name, scope=scope)["engagement"]["id"]
        if engagement_id is not None:
            if type(engagement_id) is not int:
                raise InvalidArgumentError("engagement_id must be an integer")
            args["engagement_id"] = engagement_id
        return args

    def _perform(self, tool: str, args: dict[str, Any] | None, *, typed: bool) -> dict[str, Any]:
        if not isinstance(tool, str) or not tool.strip():
            raise InvalidArgumentError("Gaia tool name must be a non-empty string")
        if args is not None and not isinstance(args, dict):
            raise InvalidArgumentError("Gaia arguments must be an object")
        for attempt in range(2):
            try:
                self._ensure_initialized()
                if typed:
                    if self._server_info is None:
                        self._accept_server_info(self._raw_tool("get_server_info", {}))
                    self._check_capabilities(self._server_info, (tool,))
                result = self._raw_tool(tool, dict(args or {}))
                return validate_response(tool, result) if typed else result
            except HttpStatusError as err:
                if err.status == 404 and attempt == 0:
                    self.reset()
                    continue
                raise self._transport.scrub_error(self._transport.http_error(err)) from None
            except NarumiError as err:
                raise self._transport.scrub_error(err) from None
        raise AssertionError("unreachable retry state")

    def _raw_tool(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            result = self._transport.request("tools/call", {"name": tool, "arguments": args})
        except RpcError as err:
            raise rpc_error(tool, err) from None
        payload = unwrap_tool_result(tool, result)
        safe_payload = self._transport.scrub(payload)
        # All public/typed calls and implicit metadata reads pass this boundary. Do not
        # turn reflected credentials into glossary terms, facts, saved snapshots or prompts.
        # Metadata keeps its existing redacted interface; business content fails closed
        # instead of silently changing its meaning or dropping an additive field.
        if tool != "get_server_info" and safe_payload != payload:
            raise ContractMismatchError(
                "gaia-library returned credential material in a tool response"
            )
        return safe_payload

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        try:
            result = self._transport.request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
                },
            )
        except RpcError as err:
            raise rpc_error("initialize", err) from None
        if result.get("protocolVersion") != PROTOCOL_VERSION:
            raise ContractMismatchError("gaia-library negotiated an unsupported MCP protocol")
        self._transport.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._initialized = True

    def _accept_server_info(self, result: dict[str, Any]) -> None:
        validate_response("get_server_info", result)
        version = result["contract_version"]
        match = _SEMVER.fullmatch(version)
        if (
            result["name"] != "gaia_library"
            or match is None
            or int(match[1]) != SUPPORTED_CONTRACT_MAJOR
        ):
            raise ContractMismatchError(
                "gaia-library contract is incompatible; "
                "narumi requires gaia_library contract major 1",
                details={"tool": "get_server_info", "supported_contract_major": 1},
            )
        self._server_info = copy.deepcopy(result)

    @staticmethod
    def _check_capabilities(info: dict[str, Any], tools: tuple[str, ...]) -> None:
        missing = sorted(set(tools) - set(info["capabilities"]["tools"]))
        if missing:
            raise EngineUnavailableError(
                "gaia-library does not advertise required tools: " + ", ".join(missing),
                details={"missing_tools": missing},
            )


def _scope_args(scope: Scope | None) -> dict[str, Any]:
    if scope is None:
        return {}
    values = scope if isinstance(scope, list) else [scope]
    if not values or any(not isinstance(item, str) or not item.strip() for item in values):
        raise InvalidArgumentError(
            "scope must be a non-empty string or a non-empty list of strings"
        )
    return {"scope": list(scope) if isinstance(scope, list) else scope}


def _validate_provenance(value: dict[str, Any]) -> None:
    allowed = {"ref_id", "system", "uri", "title", "note", "snapshot"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise InvalidArgumentError("provenance must be a Gaia provenance object")
    if "ref_id" in value:
        if type(value["ref_id"]) is not int or len(value) != 1:
            raise InvalidArgumentError("provenance.ref_id must be an integer without inline fields")
        return
    if any(
        not isinstance(value.get(key), str) or not value[key].strip()
        for key in ("system", "uri", "note")
    ):
        raise InvalidArgumentError("inline provenance requires system, uri and note")
    if any(not isinstance(item, str) for item in value.values()):
        raise InvalidArgumentError("inline provenance values must be strings")
