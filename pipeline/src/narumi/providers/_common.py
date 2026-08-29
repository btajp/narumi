"""Small, non-secret helpers shared by provider connection operations."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

from narumi.errors import BusyError, ConfigurationConflictError, NotFoundError

PROVIDERS = {
    "anthropic-api": "Anthropic API",
    "claude-agent-sdk": "Claude Agent SDK",
    "codex-app-server": "Codex App Server",
    "ollama": "Ollama",
    "openai-api": "OpenAI API",
}
AUTH_METHODS = {
    "anthropic-api": "api_key",
    "claude-agent-sdk": "api_key",
    "codex-app-server": "chatgpt",
    "ollama": "none",
    "openai-api": "api_key",
}
CONNECTION_FIELDS = (
    "connection_id",
    "revision",
    "provider_id",
    "display_name",
    "enabled",
    "endpoint",
    "auth_method",
    "credential_present",
    "auth_state",
    "catalog_state",
    "checked_at",
    "active_auth",
    "last_generation_state",
)
RUNTIME_FIELDS = (
    "state",
    "version",
    "catalog_revision",
    "resources",
    "active_setup",
    "last_setup",
)


def timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def public_connection(record: dict[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(record[key]) for key in CONNECTION_FIELDS}


def connection(document: dict[str, Any], connection_id: str) -> dict[str, Any]:
    try:
        return document["connections"][connection_id]
    except KeyError:
        raise NotFoundError("Provider connection was not found") from None


def check_revision(record: dict[str, Any], expected: int) -> None:
    if record["revision"] != expected:
        raise ConfigurationConflictError("Provider connection changed; refresh before updating")


def check_provider_idle(document: dict[str, Any], provider_id: str) -> None:
    runtime = document["runtimes"].get(provider_id, {})
    setup = runtime.get("active_setup")
    if setup is not None and setup["state"] in ("queued", "running"):
        raise BusyError("Provider runtime preparation is already active")
    if runtime.get("pending_submission"):
        raise BusyError("Provider runtime preparation acceptance is unresolved")
    for record in document["connections"].values():
        if record["provider_id"] != provider_id:
            continue
        active = record["active_auth"]
        if active is not None and active["state"] in ("pending", "unknown"):
            raise BusyError("Provider authentication is active or unresolved")
    if provider_id in document["checks"]:
        raise BusyError("Provider metadata verification is already active")


def invalidate_checks(document: dict[str, Any], record: dict[str, Any]) -> None:
    record["catalog_state"] = "unfetched"
    record["checked_at"] = None
    record["auth_state"] = (
        "unverified"
        if record["credential_present"] or record["auth_method"] == "none"
        else "unconfigured"
    )
    document["catalogs"].pop(record["connection_id"], None)


def cancel_auth(document: dict[str, Any], record: dict[str, Any], reason: str) -> None:
    active = record["active_auth"]
    if active is None:
        return
    operation = document["auth_operations"].get(active["operation_id"])
    if operation is not None and operation["state"] in ("pending", "unknown"):
        operation.update(
            state="cancelled",
            authorization_url=None,
            user_code=None,
            reason=reason,
            updated_at=timestamp(),
        )
    record["active_auth"] = None
    record["auth_state"] = (
        "unverified"
        if record["credential_present"] or record["auth_method"] == "none"
        else "unconfigured"
    )


def public_runtime(record: dict[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(record[key]) for key in RUNTIME_FIELDS}
