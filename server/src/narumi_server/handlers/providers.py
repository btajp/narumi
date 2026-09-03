"""Public connection operations; no meeting data or credential values enter audit."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from narumi.errors import AuthenticationRequiredError

if TYPE_CHECKING:
    from narumi_server.context import ServerContext


def _resident(ctx: ServerContext) -> None:
    if "streamable-http" not in ctx.transports:
        raise AuthenticationRequiredError("Provider operations require the authenticated server")


def _audit(ctx: ServerContext, action: str, result: dict[str, Any]) -> None:
    ctx.contracts.validate_output(action, result)
    connection = result.get("connection", {})
    operation = result.get("operation", {})
    detail = {
        key: value
        for key, value in {
            "connection_id": connection.get("connection_id")
            or result.get("connection_id")
            or operation.get("connection_id"),
            "revision": connection.get("revision"),
            "provider_id": connection.get("provider_id"),
            "enabled": connection.get("enabled"),
            "credential_present": connection.get("credential_present"),
            "job_id": result.get("job_id"),
            "operation_id": operation.get("operation_id"),
        }.items()
        if value is not None
    }
    ctx.catalog.audit(ctx.actor, action, detail)


def list_providers(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    _resident(ctx)
    return ctx.providers.list_providers()


def list_provider_connections(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    _resident(ctx)
    return ctx.providers.list_connections()


def set_provider_connection(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    _resident(ctx)
    result = ctx.providers.set_connection(args)
    _audit(ctx, "set_provider_connection", result)
    return result


def delete_provider_connection(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    _resident(ctx)
    result = ctx.providers.delete_connection(args)
    _audit(ctx, "delete_provider_connection", result)
    return result


def prepare_provider_runtime(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    _resident(ctx)
    result = ctx.providers.prepare_runtime(args)
    _audit(ctx, "prepare_provider_runtime", result)
    return result


def authenticate_provider_connection(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    _resident(ctx)
    result = ctx.providers.authenticate(args)
    _audit(ctx, "authenticate_provider_connection", result)
    return result


def get_provider_auth_status(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    _resident(ctx)
    return ctx.providers.auth_status(args)


def test_provider_connection(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    _resident(ctx)
    return ctx.providers.test_connection(args)


def list_provider_models(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    _resident(ctx)
    return ctx.providers.list_models(args)


def verify_provider_model(ctx: ServerContext, args: dict[str, Any]) -> dict[str, Any]:
    _resident(ctx)
    result = ctx.providers.verify_model(args)
    _audit(ctx, "verify_provider_model", result)
    return result
