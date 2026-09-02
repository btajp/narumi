"""Explicit metadata discovery for a saved provider connection."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from narumi.errors import (
    AuthenticationRequiredError,
    CancelledError,
    InvalidArgumentError,
    ModelUnavailableError,
)
from narumi.providers.metadata import anthropic, ollama, openai, openai_compatible
from narumi.providers.metadata.endpoints import validate_endpoint
from narumi.providers.metadata.http import DEFAULT_TIMEOUT, JSONHTTPClient
from narumi.providers.metadata.openai_compatible_transport import (
    OpenAICompatibleTransport,
)
from narumi.providers.metadata.openai_compatible_transport import (
    configuration as compatible_configuration,
)
from narumi.providers.metadata.validation import (
    check_public_payload,
    invalid_metadata,
    require_object,
)

DISCOVERY_TIMEOUT = 30.0


class MetadataClient:
    """No environment credentials, SDK startup, model downloads or generation."""

    def __init__(
        self,
        *,
        http: JSONHTTPClient | None = None,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        resolver: Callable[..., list[tuple]] | None = None,
    ) -> None:
        self._http = http or JSONHTTPClient()
        self._now = now or (lambda: datetime.now(UTC))
        self._monotonic = monotonic
        self._resolver = resolver

    def fetch(self, provider_id: str, endpoint: str, api_key: str | None) -> list[dict[str, Any]]:
        request = self._requester(provider_id, endpoint, api_key)
        now = self._now().astimezone(UTC)
        fetched_at = now.isoformat().replace("+00:00", "Z")
        if provider_id == "ollama":
            return ollama.fetch_models(request, fetched_at=fetched_at)
        if provider_id == "openai-api":
            return openai.fetch_models(request, fetched_at=fetched_at, now=now)
        return anthropic.fetch_models(request, provider_id=provider_id, fetched_at=fetched_at)

    def fetch_openai_compatible(
        self,
        endpoint: str,
        api_key: str | None,
        *,
        auth_method: str,
        api_surface: str,
    ) -> list[dict[str, Any]]:
        """List display-only candidates; a separate paid probe verifies generation."""
        config = compatible_configuration(
            endpoint,
            auth_method=auth_method,
            api_surface=api_surface,
            require_chat_max_tokens_field=False,
        )
        transport = OpenAICompatibleTransport(
            http=self._http,
            monotonic=self._monotonic,
            **({"resolver": self._resolver} if self._resolver is not None else {}),
        )
        deadline = self._monotonic() + DISCOVERY_TIMEOUT

        def request(method: str, route: str, *, payload=None) -> dict[str, Any]:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise invalid_metadata("metadata_timeout")
            result = transport.request(
                config,
                api_key,
                method,
                route,
                payload=payload,
                timeout=min(DEFAULT_TIMEOUT, remaining),
            )
            if self._monotonic() > deadline:
                raise invalid_metadata("metadata_timeout")
            result = require_object(result)
            secrets = (api_key, "Bearer " + api_key) if api_key else ()
            check_public_payload(result, secrets=secrets)
            return result

        fetched_at = self._now().astimezone(UTC).isoformat().replace("+00:00", "Z")
        return openai_compatible.fetch_models(request, fetched_at=fetched_at)

    def require_local_ollama_model(
        self, endpoint: str, model: str, *, should_cancel: Callable[[], bool] | None = None
    ) -> dict[str, Any]:
        selector = ollama.local_selector(model)
        request = self._requester("ollama", endpoint, None, should_cancel=should_cancel)
        fetched_at = self._now().astimezone(UTC).isoformat().replace("+00:00", "Z")
        models = ollama.fetch_models(request, fetched_at=fetched_at, selected_model=selector[:-6])
        if not models or models[0]["availability"] != "available":
            raise ModelUnavailableError(
                "Ollama model is not verified for local generation",
                details={"reason": "local_model_unverified"},
            )
        return models[0]

    def _requester(
        self,
        provider_id: str,
        endpoint: str,
        api_key: str | None,
        *,
        should_cancel: Callable[[], bool] | None = None,
    ) -> Callable:
        if provider_id not in {"anthropic-api", "claude-agent-sdk", "ollama", "openai-api"}:
            raise InvalidArgumentError("This provider does not use the HTTP metadata adapter")
        endpoint = validate_endpoint(provider_id, endpoint)
        headers: dict[str, str] = {}
        if provider_id == "ollama":
            if api_key is not None:
                raise InvalidArgumentError(
                    "Local Ollama does not accept an API key", details={"reason": "unexpected_key"}
                )
        else:
            if not api_key:
                raise AuthenticationRequiredError(
                    "A saved API key is required", details={"reason": "credential_required"}
                )
            if (
                not isinstance(api_key, str)
                or len(api_key) > 4096
                or any(not 33 <= ord(char) <= 126 for char in api_key)
            ):
                raise InvalidArgumentError(
                    "API key has an invalid format", details={"reason": "invalid_credential"}
                )
            if provider_id == "openai-api":
                headers = {"Authorization": "Bearer " + api_key}
            else:
                headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        deadline = self._monotonic() + DISCOVERY_TIMEOUT

        def request(method: str, route: str, *, payload=None) -> dict[str, Any]:
            check_cancelled(should_cancel)
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise invalid_metadata("metadata_timeout")
            result = self._http.request(
                method,
                endpoint + route,
                headers=headers,
                payload=payload,
                timeout=min(DEFAULT_TIMEOUT, remaining),
                **({"should_cancel": should_cancel} if should_cancel is not None else {}),
            )
            check_cancelled(should_cancel)
            if self._monotonic() > deadline:
                raise invalid_metadata("metadata_timeout")
            result = require_object(result)
            # Reject the raw key even when HTTP is substituted by a test adapter.
            # A Bearer header reflection must also never become public metadata.
            secrets = (api_key, headers.get("Authorization", "")) if api_key else ()
            check_public_payload(result, secrets=secrets)
            return result

        return request


def check_cancelled(should_cancel: Callable[[], bool] | None) -> None:
    if should_cancel is not None and should_cancel():
        raise CancelledError(
            "Provider metadata request cancelled",
            details={"reason": "provider_generation_cancelled"},
        )
