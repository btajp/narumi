"""Explicit OpenAI-compatible text generation with no protocol fallback or retry."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from narumi.errors import (
    AuthenticationRequiredError,
    CancelledError,
    EngineUnavailableError,
    InvalidArgumentError,
    ModelUnavailableError,
    NarumiError,
)
from narumi.providers.metadata.http import generation_status_is_unknown
from narumi.providers.metadata.openai_compatible import model_descriptor
from narumi.providers.metadata.openai_compatible_transport import (
    OpenAICompatibleConfig,
    OpenAICompatibleTransport,
    configuration,
)
from narumi.providers.metadata.validation import APP_MAX_OUTPUT_TOKENS, MODEL_ID
from narumi.providers.openai_compatible_response import (
    OUTCOME_UNKNOWN,
    generation_unknown,
    parse_response,
)

GENERATION_TIMEOUT = 600.0
VERIFY_MAX_TOKENS = 32
VERIFY_SENTINEL = "NARUMI_OPENAI_COMPATIBLE_TEXT_OK"
VERIFY_PROMPT = f"Reply with exactly {VERIFY_SENTINEL} and nothing else."
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z\Z")
CancelCheck = Callable[[], bool]


@dataclass(frozen=True)
class OpenAICompatibleCompletionResult:
    text: str
    returned_model: str
    usage: dict[str, int] | None = None


class OpenAICompatibleBackend:
    """One selected surface and one saved endpoint are used for each request."""

    def __init__(
        self,
        *,
        transport: OpenAICompatibleTransport | None = None,
    ) -> None:
        self._transport = transport or OpenAICompatibleTransport()

    def complete(
        self,
        endpoint: str,
        api_key: str | None,
        model: dict[str, Any],
        parameters: dict[str, Any],
        prompt: str,
        *,
        auth_method: str,
        api_surface: str,
        chat_max_tokens_field: str | None = None,
        system: str | None = None,
        should_cancel: CancelCheck | None = None,
    ) -> OpenAICompatibleCompletionResult:
        config = configuration(
            endpoint,
            auth_method=auth_method,
            api_surface=api_surface,
            chat_max_tokens_field=chat_max_tokens_field,
        )
        _check_cancelled(should_cancel)
        _validate_generation_inputs(model, parameters, prompt, system)
        return self._complete_once(
            config,
            api_key,
            model["model_id"],
            parameters["max_tokens"],
            prompt,
            system=system,
            should_cancel=should_cancel,
            exact_text=None,
        )

    def verify_model(
        self,
        endpoint: str,
        api_key: str | None,
        model_id: str,
        *,
        auth_method: str,
        api_surface: str,
        chat_max_tokens_field: str | None = None,
        fetched_at: str | None = None,
        should_cancel: CancelCheck | None = None,
    ) -> dict[str, Any]:
        """Send one fixed paid probe; the caller must enforce explicit confirmation/idempotency."""
        config = configuration(
            endpoint,
            auth_method=auth_method,
            api_surface=api_surface,
            chat_max_tokens_field=chat_max_tokens_field,
        )
        if not isinstance(model_id, str) or MODEL_ID.fullmatch(model_id) is None:
            raise InvalidArgumentError("The exact model ID is invalid")
        _check_cancelled(should_cancel)
        result = self._complete_once(
            config,
            api_key,
            model_id,
            VERIFY_MAX_TOKENS,
            VERIFY_PROMPT,
            system=None,
            should_cancel=should_cancel,
            exact_text=VERIFY_SENTINEL,
        )
        if result.text != VERIFY_SENTINEL or result.returned_model != model_id:
            raise generation_unknown()
        observed_at = fetched_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")
        if not isinstance(observed_at, str) or _TIMESTAMP.fullmatch(observed_at) is None:
            raise InvalidArgumentError("The model observation timestamp is invalid")
        return model_descriptor(model_id, fetched_at=observed_at, verified=True)

    def _complete_once(
        self,
        config: OpenAICompatibleConfig,
        api_key: str | None,
        model_id: str,
        max_tokens: int,
        prompt: str,
        *,
        system: str | None,
        should_cancel: CancelCheck | None,
        exact_text: str | None,
    ) -> OpenAICompatibleCompletionResult:
        route, payload = _request_body(config, model_id, max_tokens, prompt, system)
        _check_cancelled(should_cancel)
        try:
            response = self._transport.request(
                config,
                api_key,
                "POST",
                route,
                payload=payload,
                timeout=GENERATION_TIMEOUT,
                response_kind="generation",
                should_cancel=should_cancel,
            )
        except NarumiError as error:
            raise _safe_transport_error(error) from None
        except Exception:
            raise generation_unknown() from None
        text, returned_model, usage = parse_response(
            config.api_surface,
            model_id,
            response,
            api_key=api_key,
            exact_text=exact_text,
        )
        return OpenAICompatibleCompletionResult(text, returned_model, usage)


def _request_body(
    config: OpenAICompatibleConfig,
    model_id: str,
    max_tokens: int,
    prompt: str,
    system: str | None,
) -> tuple[str, dict[str, Any]]:
    if config.api_surface == "responses":
        payload: dict[str, Any] = {
            "model": model_id,
            "input": prompt,
            "tools": [],
            "tool_choice": "none",
            "parallel_tool_calls": False,
            "store": False,
            "background": False,
            "stream": False,
            "truncation": "disabled",
            "max_output_tokens": max_tokens,
        }
        if system is not None:
            payload["instructions"] = system
        return "/responses", payload
    messages: list[dict[str, str]] = []
    if system is not None:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": model_id,
        "messages": messages,
        "tools": [],
        "tool_choice": "none",
        "parallel_tool_calls": False,
        "store": False,
        "stream": False,
        "n": 1,
        config.chat_max_tokens_field: max_tokens,
    }
    return "/chat/completions", payload


def _validate_generation_inputs(model: Any, parameters: Any, prompt: Any, system: Any) -> None:
    if (
        not isinstance(model, dict)
        or model.get("availability") != "available"
        or not isinstance(model.get("model_id"), str)
        or MODEL_ID.fullmatch(model["model_id"]) is None
        or "text" not in model.get("input_modalities", [])
        or "text" not in model.get("output_modalities", [])
        or "llm" not in model.get("roles", [])
    ):
        raise ModelUnavailableError("The selected model is not verified for text generation")
    if (
        not isinstance(prompt, str)
        or not prompt.strip()
        or (system is not None and not isinstance(system, str))
    ):
        raise InvalidArgumentError("Minutes generation requires text input")
    if not isinstance(parameters, dict) or set(parameters) != {"max_tokens"}:
        raise InvalidArgumentError("The selected generation parameters are not supported")
    max_tokens = parameters["max_tokens"]
    known_max = model.get("max_output_tokens")
    if (
        type(max_tokens) is not int
        or not 1 <= max_tokens <= APP_MAX_OUTPUT_TOKENS
        or (known_max is not None and (type(known_max) is not int or max_tokens > known_max))
    ):
        raise InvalidArgumentError("The generation output limit is invalid")
    schema = model.get("parameter_schema")
    if not isinstance(schema, dict):
        raise ModelUnavailableError("The model parameter capabilities are unavailable")
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(parameters)
    except (SchemaError, ValidationError, TypeError, ValueError, RecursionError):
        raise InvalidArgumentError("The selected model does not support these parameters") from None


def _safe_transport_error(error: NarumiError) -> NarumiError:
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
    if isinstance(error, AuthenticationRequiredError):
        if reason == "credential_required":
            return AuthenticationRequiredError(
                "A saved API key is required", details={"reason": "credential_required"}
            )
        if reason == "credential_rejected":
            return AuthenticationRequiredError(
                "The provider rejected the saved credentials",
                details={"reason": "credential_rejected"},
            )
    status = error.details.get("status")
    if reason == "metadata_http_error" and generation_status_is_unknown(status):
        return generation_unknown()
    if reason == "metadata_http_error" and type(status) is int and 400 <= status <= 499:
        return EngineUnavailableError(
            "The provider rejected this generation request",
            details={"reason": "provider_generation_rejected", "status": status},
        )
    if reason in {"metadata_connection_failed", "invalid_http_options"}:
        return EngineUnavailableError(
            "The provider generation request could not be sent",
            details={"reason": "provider_generation_not_sent"},
        )
    return error if isinstance(error, InvalidArgumentError) else generation_unknown()


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


__all__ = [
    "OpenAICompatibleBackend",
    "OpenAICompatibleCompletionResult",
    "VERIFY_PROMPT",
    "VERIFY_SENTINEL",
]
