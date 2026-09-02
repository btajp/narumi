"""Pinned, non-retrying transport for one saved OpenAI-compatible connection."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from narumi.errors import AuthenticationRequiredError, EngineUnavailableError, InvalidArgumentError
from narumi.providers.metadata.endpoints import (
    is_loopback_endpoint,
    resolve_openai_compatible_addresses,
    validate_openai_compatible_endpoint,
)
from narumi.providers.metadata.http import JSONHTTPClient

API_SURFACES = {"responses", "chat_completions"}
AUTH_METHODS = {"api_key", "none"}
CHAT_MAX_TOKEN_FIELDS = {"max_tokens", "max_completion_tokens"}


@dataclass(frozen=True)
class OpenAICompatibleConfig:
    endpoint: str
    auth_method: str
    api_surface: str
    chat_max_tokens_field: str | None


def configuration(
    endpoint: str,
    *,
    auth_method: str,
    api_surface: str,
    chat_max_tokens_field: str | None = None,
    require_chat_max_tokens_field: bool = True,
) -> OpenAICompatibleConfig:
    if auth_method not in AUTH_METHODS or api_surface not in API_SURFACES:
        raise InvalidArgumentError(
            "OpenAI-compatible connection protocol is invalid",
            details={"reason": "invalid_provider_configuration"},
        )
    endpoint = validate_openai_compatible_endpoint(endpoint, auth_method=auth_method)
    if api_surface == "responses" and chat_max_tokens_field is not None:
        raise InvalidArgumentError(
            "Responses connections do not accept a Chat Completions token field",
            details={"reason": "invalid_provider_configuration"},
        )
    if api_surface == "chat_completions" and (
        chat_max_tokens_field not in CHAT_MAX_TOKEN_FIELDS
        and (require_chat_max_tokens_field or chat_max_tokens_field is not None)
    ):
        raise InvalidArgumentError(
            "Chat Completions requires an explicit output-token field",
            details={"reason": "invalid_provider_configuration"},
        )
    return OpenAICompatibleConfig(
        endpoint=endpoint,
        auth_method=auth_method,
        api_surface=api_surface,
        chat_max_tokens_field=chat_max_tokens_field,
    )


def authentication_headers(config: OpenAICompatibleConfig, api_key: str | None) -> dict[str, str]:
    if config.auth_method == "none":
        if api_key is not None or not is_loopback_endpoint(config.endpoint):
            raise InvalidArgumentError(
                "This OpenAI-compatible connection does not accept credentials",
                details={"reason": "unexpected_key"},
            )
        return {}
    if api_key is None or api_key == "":
        raise AuthenticationRequiredError(
            "A saved API key is required", details={"reason": "credential_required"}
        )
    if (
        not isinstance(api_key, str)
        or len(api_key) > 4096
        or any(not 33 <= ord(char) <= 126 for char in api_key)
        or (len(api_key) >= 8 and api_key in urlsplit(config.endpoint).path.split("/"))
    ):
        raise InvalidArgumentError(
            "API key has an invalid format", details={"reason": "invalid_credential"}
        )
    return {"Authorization": "Bearer " + api_key}


class OpenAICompatibleTransport:
    """Resolve, approve and pin every destination before a single HTTP attempt."""

    def __init__(
        self,
        *,
        http: JSONHTTPClient | None = None,
        resolver: Callable[..., list[tuple]] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = http or JSONHTTPClient()
        self._resolver = resolver
        self._monotonic = monotonic

    def request(
        self,
        config: OpenAICompatibleConfig,
        api_key: str | None,
        method: str,
        route: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: float,
        response_kind: Literal["metadata", "generation"] = "metadata",
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        expected_route = "/responses" if config.api_surface == "responses" else "/chat/completions"
        valid_request = (
            response_kind == "metadata"
            and method == "GET"
            and route == "/models"
            and payload is None
        ) or (
            response_kind == "generation"
            and method == "POST"
            and route == expected_route
            and isinstance(payload, dict)
        )
        if not valid_request:
            raise InvalidArgumentError(
                "OpenAI-compatible request route is invalid",
                details={"reason": "invalid_provider_configuration"},
            )
        headers = authentication_headers(config, api_key)
        started_at = self._monotonic()
        addresses = resolve_openai_compatible_addresses(
            config.endpoint,
            timeout=min(timeout, 10.0),
            resolver=self._resolver,
            should_cancel=should_cancel,
        )
        remaining = timeout - (self._monotonic() - started_at)
        if remaining <= 0:
            raise EngineUnavailableError(
                "Provider endpoint resolution exceeded the request deadline",
                details={"reason": "metadata_connection_failed"},
            )
        options: dict[str, Any] = {
            "headers": headers,
            "payload": payload,
            "timeout": remaining,
            "response_kind": response_kind,
            "resolved_addresses": addresses,
        }
        if should_cancel is not None:
            options["should_cancel"] = should_cancel
        return self._http.request(method, config.endpoint + route, **options)


__all__ = [
    "OpenAICompatibleConfig",
    "OpenAICompatibleTransport",
    "authentication_headers",
    "configuration",
]
