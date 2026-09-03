"""Explicit, non-retrying HTTP generation for saved text-minutes selections."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from narumi.errors import (
    AuthenticationRequiredError,
    CancelledError,
    ConfigurationConflictError,
    EngineUnavailableError,
    InvalidArgumentError,
    ModelUnavailableError,
    NarumiError,
)
from narumi.providers.http_generation_response import (
    OUTCOME_UNKNOWN,
    generation_unknown,
    parse_response,
)
from narumi.providers.metadata import MetadataClient, validate_endpoint
from narumi.providers.metadata.http import JSONHTTPClient, generation_status_is_unknown
from narumi.providers.metadata.ollama import local_selector
from narumi.providers.metadata.openai_capabilities import reasoning_payload
from narumi.providers.metadata.validation import MODEL_ID

GENERATION_TIMEOUT = 600.0
MAX_OUTPUT_TOKENS = 32_768
_DIGEST = re.compile(r"(?:sha256:)?[a-f0-9]{64}\Z")
CancelCheck = Callable[[], bool]


@dataclass(frozen=True)
class HTTPCompletionResult:
    text: str
    returned_model: str
    usage: dict[str, int] | None = None


class HTTPMinutesBackend:
    """Each call owns its transport; no environment or another connection supplies state."""

    def __init__(
        self, *, metadata: MetadataClient | None = None, http: JSONHTTPClient | None = None
    ) -> None:
        self._http = http if http is not None else JSONHTTPClient()
        self._metadata = metadata if metadata is not None else MetadataClient(http=self._http)

    def complete(
        self,
        provider_id: str,
        endpoint: str,
        api_key: str | None,
        model: dict[str, Any],
        parameters: dict[str, Any],
        prompt: str,
        *,
        system: str | None = None,
        should_cancel: CancelCheck | None = None,
    ) -> HTTPCompletionResult:
        _check_cancelled(should_cancel)
        if provider_id not in {"openai-api", "anthropic-api", "ollama"}:
            raise InvalidArgumentError("This provider has no HTTP minutes adapter")
        endpoint = validate_endpoint(provider_id, endpoint)
        headers = _headers(provider_id, api_key)
        _validate_inputs(model, parameters, prompt, system, provider_id=provider_id)
        model_id = model["model_id"]
        route, payload = _request_body(provider_id, model_id, parameters, prompt, system)
        if provider_id == "ollama":
            self._verify_local_model(endpoint, model, should_cancel)
        _check_cancelled(should_cancel)
        try:
            response = self._http.request(
                "POST",
                endpoint + route,
                headers=headers,
                payload=payload,
                timeout=GENERATION_TIMEOUT,
                response_kind="generation",
                should_cancel=should_cancel,
            )
        except NarumiError as error:
            raise _safe_http_error(error) from None
        except Exception:
            raise generation_unknown() from None
        text, returned_model, usage = parse_response(
            provider_id, model_id, response, api_key=api_key
        )
        # Preserve a fully received reply for the checkpoint even if cancellation
        # arrived after the transport finished. The caller stops before its next call.
        return HTTPCompletionResult(text, returned_model, usage)

    def _verify_local_model(
        self, endpoint: str, model: dict[str, Any], should_cancel: CancelCheck | None
    ) -> None:
        revision = model.get("resolved_revision")
        if not isinstance(revision, str) or _DIGEST.fullmatch(revision) is None:
            raise ModelUnavailableError(
                "The saved Ollama model has no verified local digest",
                details={"reason": "local_model_unverified"},
            )
        try:
            options = {"should_cancel": should_cancel} if should_cancel is not None else {}
            current = self._metadata.require_local_ollama_model(
                endpoint, model["model_id"], **options
            )
            valid = (
                _verified_text_model(current)
                and current.get("source") == "runtime"
                and isinstance(current.get("billing"), dict)
                and current["billing"].get("kind") == "local"
            )
        except CancelledError:
            raise _cancelled() from None
        except Exception:
            raise ModelUnavailableError(
                "The Ollama model could not be verified for local generation",
                details={"reason": "local_model_unverified"},
            ) from None
        if not valid:
            raise ModelUnavailableError(
                "The Ollama model is not verified for local text generation",
                details={"reason": "local_model_unverified"},
            )
        if (
            current.get("model_id") != model["model_id"]
            or current.get("resolved_revision") != revision
        ):
            raise ConfigurationConflictError(
                "The local Ollama model changed; refresh and select the model again",
                details={"reason": "local_model_changed"},
            )


def _headers(provider_id: str, api_key: str | None) -> dict[str, str]:
    if provider_id == "ollama":
        if api_key is not None:
            raise InvalidArgumentError("Local Ollama does not accept an API key")
        return {}
    if api_key is None or api_key == "":
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
        return {"Authorization": "Bearer " + api_key}
    return {"x-api-key": api_key, "anthropic-version": "2023-06-01"}


def _validate_inputs(
    model: dict[str, Any],
    parameters: dict[str, Any],
    prompt: str,
    system: str | None,
    *,
    provider_id: str,
) -> None:
    if (
        not isinstance(prompt, str)
        or not prompt.strip()
        or (system is not None and not isinstance(system, str))
    ):
        raise InvalidArgumentError("Minutes generation requires text input")
    if (
        not _verified_text_model(model)
        or not isinstance(model.get("model_id"), str)
        or MODEL_ID.fullmatch(model["model_id"]) is None
    ):
        raise ModelUnavailableError("The selected model is not verified for text generation")
    allowed = {"max_tokens", "reasoning_effort"} if provider_id == "openai-api" else {"max_tokens"}
    if not isinstance(parameters, dict) or set(parameters) - allowed:
        raise InvalidArgumentError("The selected generation parameters are not supported")
    maximum, requested = model.get("max_output_tokens"), parameters.get("max_tokens")
    if (
        type(requested) is not int
        or not 1 <= requested <= MAX_OUTPUT_TOKENS
        or (
            maximum is not None
            and (type(maximum) is not int or maximum <= 0 or requested > maximum)
        )
    ):
        raise InvalidArgumentError("The generation output limit is invalid")
    schema = model.get("parameter_schema")
    if (
        not isinstance(schema, dict)
        or schema.get("type") != "object"
        or schema.get("additionalProperties") is not False
    ):
        raise ModelUnavailableError("The model parameter capabilities are unavailable")
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(parameters)
    except (SchemaError, ValidationError, TypeError, ValueError, RecursionError):
        raise InvalidArgumentError("The selected model does not support these parameters") from None


def _verified_text_model(model: Any) -> bool:
    return (
        isinstance(model, dict)
        and model.get("availability") == "available"
        and all(
            isinstance(model.get(field), list) and required in model[field]
            for field, required in (
                ("input_modalities", "text"),
                ("output_modalities", "text"),
                ("roles", "llm"),
            )
        )
    )


def _request_body(
    provider_id: str, model_id: str, parameters: dict[str, Any], prompt: str, system: str | None
) -> tuple[str, dict[str, Any]]:
    if provider_id == "openai-api":
        payload: dict[str, Any] = {
            "model": model_id,
            "input": prompt,
            "tools": [],
            "tool_choice": "none",
            "store": False,
            "background": False,
            "stream": False,
            "truncation": "disabled",
            "max_output_tokens": parameters["max_tokens"],
        }
        if system is not None:
            payload["instructions"] = system
        reasoning = reasoning_payload(model_id, parameters.get("reasoning_effort"))
        if reasoning is not None:
            payload["reasoning"] = reasoning
        return "/v1/responses", payload
    if provider_id == "anthropic-api":
        payload = {
            "model": model_id,
            "max_tokens": parameters["max_tokens"],
            "stream": False,
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
        }
        if system is not None:
            payload["system"] = system
        return "/v1/messages", payload
    payload = {
        "model": local_selector(model_id),
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": parameters["max_tokens"]},
    }
    if system is not None:
        payload["system"] = system
    return "/api/generate", payload


def _safe_http_error(error: NarumiError) -> NarumiError:
    reason = error.details.get("reason")
    unknown = bool(error.details.get("outcome_unknown")) or reason == OUTCOME_UNKNOWN
    if isinstance(error, CancelledError):
        if unknown:
            return CancelledError(
                "The generation connection was cancelled; provider completion is unknown",
                details={"reason": OUTCOME_UNKNOWN, "outcome_unknown": True},
            )
        if reason == "provider_generation_cancelled":
            return _cancelled()
    if unknown:
        return generation_unknown()
    if isinstance(error, AuthenticationRequiredError) and reason == "credential_rejected":
        return AuthenticationRequiredError(
            "The provider rejected the saved credentials", details={"reason": "credential_rejected"}
        )
    status = error.details.get("status")
    if reason == "metadata_http_error" and generation_status_is_unknown(status):
        return generation_unknown()
    if reason == "metadata_http_error" and type(status) is int and 400 <= status <= 499:
        return EngineUnavailableError(
            "The provider rejected this generation request",
            details={"reason": "provider_generation_rejected", "status": status},
        )
    if isinstance(reason, str) and reason in {"metadata_connection_failed", "invalid_http_options"}:
        return EngineUnavailableError(
            "The provider generation request could not be sent",
            details={"reason": "provider_generation_not_sent"},
        )
    return generation_unknown()


def _check_cancelled(should_cancel: CancelCheck | None) -> None:
    try:
        cancelled = should_cancel is not None and should_cancel()
    except Exception:
        raise _cancelled() from None
    if cancelled:
        raise _cancelled()


def _cancelled() -> CancelledError:
    return CancelledError(
        "Minutes generation was cancelled before sending",
        details={"reason": "provider_generation_cancelled"},
    )
