"""Explicit metadata discovery for a saved provider connection."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from narumi.errors import AuthenticationRequiredError, InvalidArgumentError, ModelUnavailableError
from narumi.providers.metadata import anthropic, ollama
from narumi.providers.metadata.endpoints import validate_endpoint
from narumi.providers.metadata.http import DEFAULT_TIMEOUT, JSONHTTPClient
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
    ) -> None:
        self._http = http or JSONHTTPClient()
        self._now = now or (lambda: datetime.now(UTC))
        self._monotonic = monotonic

    def fetch(self, provider_id: str, endpoint: str, api_key: str | None) -> list[dict[str, Any]]:
        request = self._requester(provider_id, endpoint, api_key)
        fetched_at = self._now().astimezone(UTC).isoformat().replace("+00:00", "Z")
        if provider_id == "ollama":
            return ollama.fetch_models(request, fetched_at=fetched_at)
        return anthropic.fetch_models(request, provider_id=provider_id, fetched_at=fetched_at)

    def require_local_ollama_model(self, endpoint: str, model: str) -> dict[str, Any]:
        selector = ollama.local_selector(model)
        request = self._requester("ollama", endpoint, None)
        fetched_at = self._now().astimezone(UTC).isoformat().replace("+00:00", "Z")
        models = ollama.fetch_models(request, fetched_at=fetched_at, selected_model=selector[:-6])
        if not models or models[0]["availability"] != "available":
            raise ModelUnavailableError(
                "Ollama model is not verified for local generation",
                details={"reason": "local_model_unverified"},
            )
        return models[0]

    def _requester(self, provider_id: str, endpoint: str, api_key: str | None) -> Callable:
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
            headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        deadline = self._monotonic() + DISCOVERY_TIMEOUT

        def request(method: str, route: str, *, payload=None) -> dict[str, Any]:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise invalid_metadata("metadata_timeout")
            result = self._http.request(
                method,
                endpoint + route,
                headers=headers,
                payload=payload,
                timeout=min(DEFAULT_TIMEOUT, remaining),
            )
            if self._monotonic() > deadline:
                raise invalid_metadata("metadata_timeout")
            result = require_object(result)
            check_public_payload(result, secrets=(api_key,) if api_key else ())
            return result

        return request
