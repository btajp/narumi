"""App-facing Gaia connection tools; credentials never enter public projections or audit."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from narumi.errors import EngineUnavailableError, ErrorCode, NarumiError
from narumi.gaia.settings import UNSET

if TYPE_CHECKING:
    from narumi_server.context import ServerContext

_REQUIRED_CAPABILITIES = ("search_context", "get_glossary", "resolve_speakers", "propose_update")


def get_gaia_connection(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    return {"connection": ctx.gaia.get()}


def set_gaia_connection(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    connection = ctx.gaia.set(url=args.get("url", UNSET), api_key=args.get("api_key", UNSET))
    ctx.catalog.audit(
        ctx.actor,
        "set_gaia_connection",
        {
            "updated": [key for key in ("url", "api_key") if key in args],
            "enabled": connection["url"] is not None,
            "has_api_key": connection["has_api_key"],
        },
    )
    return {"connection": connection}


def test_gaia_connection(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    client = ctx.gaia.client(timeout=args.get("timeout_seconds", 5))
    if client is None:
        raise EngineUnavailableError("Gaia connection is not configured")
    try:
        client.require_capabilities(*_REQUIRED_CAPABILITIES)
        info = client.get_server_info()
    except NarumiError as exc:
        if exc.code == ErrorCode.SCOPE_DENIED:
            raise EngineUnavailableError("Gaia authentication or access was denied") from None
        raise
    identity = info["client"]
    return {
        "connected": True,
        "name": info["name"],
        "version": info["version"],
        "contract_version": info["contract_version"],
        "client": {
            "name": identity["name"],
            "role": identity["role"],
            "default_scope": identity.get("default_scope"),
        },
    }
