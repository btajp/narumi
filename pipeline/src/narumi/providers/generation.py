"""Resolve saved minutes-only model selections without ambient credentials.

Each completion rechecks policy and the pinned connection/model/runtime and holds a durable
provider lease. Secret values are obtained only for that call, never kept in provenance.
"""

from __future__ import annotations

import copy
import ipaddress
import re
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from pydantic import ValidationError as ModelValidationError

from narumi.bundle.hashing import sha256_params
from narumi.errors import (
    AuthenticationRequiredError,
    CancelledError,
    ConfigurationConflictError,
    EngineUnavailableError,
    ErrorCode,
    InvalidArgumentError,
    ModelUnavailableError,
    NarumiError,
)
from narumi.llm.base import CapabilityProfile, CostClass, LLMProvider
from narumi.llm.policy import check_policy
from narumi.llm.registry import select_provider
from narumi.model_selection import MINUTES_PARAMETER_NAMES, ModelSelection
from narumi.models import MeetingConfig
from narumi.providers._common import check_provider_idle, check_revision, connection
from narumi.providers.catalog import (
    expected_runtime_evidence,
    model_verification_evidence,
    returned_runtime_matches,
)
from narumi.providers.metadata import validate_endpoint

if TYPE_CHECKING:
    from narumi.providers.service import ProviderService

PROVIDER_ID = "codex-app-server"
MINUTES_MODEL_PROVIDERS = (
    "codex-app-server",
    "claude-agent-sdk",
    "openai-api",
    "openai-compatible-api",
    "anthropic-api",
    "ollama",
)
ADAPTER_VERSION = "1"
OUTCOME_UNKNOWN = "provider_generation_outcome_unknown"
LEGACY_OUTCOME_UNKNOWN = "codex_generation_outcome_unknown"
CancelCheck = Callable[[], bool]
MAX_OUTPUT_TOKENS = 32768
DEFAULT_OUTPUT_TOKENS = 4096
USAGE_FIELDS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "reasoning_output_tokens",
    }
)


@dataclass(frozen=True)
class _ProviderSpec:
    auth_methods: tuple[str, ...]
    cost_class: CostClass
    destination: str
    source: str

    def profile(self) -> CapabilityProfile:
        return CapabilityProfile(False, 0, self.cost_class, self.destination, False)


_PROVIDERS = {
    "codex-app-server": _ProviderSpec(("chatgpt",), "subscription", "openai", "runtime"),
    "claude-agent-sdk": _ProviderSpec(("api_key",), "api", "anthropic", "provider_api"),
    "openai-api": _ProviderSpec(("api_key",), "api", "openai", "provider_api"),
    "openai-compatible-api": _ProviderSpec(
        ("api_key", "none"), "api", "configured-openai-compatible-api", "provider_api"
    ),
    "anthropic-api": _ProviderSpec(("api_key",), "api", "anthropic", "provider_api"),
    "ollama": _ProviderSpec(("none",), "local", "local", "runtime"),
}


@dataclass(frozen=True)
class _Selection:
    params: dict[str, Any]
    profile: CapabilityProfile
    connection_id: str
    provider_id: str
    model_id: str
    model: dict[str, Any]
    parameters: dict[str, Any]
    endpoint: str
    auth_method: str
    secret_account: str | None
    api_surface: str | None
    chat_max_tokens_field: str | None
    expected_runtime: dict[str, str] | None


class MinutesResolver:
    """An explicitly injected provider service is required for connected generation."""

    def __init__(self, service: ProviderService) -> None:
        self.service = service

    @contextmanager
    def validated(self, config: MeetingConfig) -> Iterator[dict[str, Any]]:
        """Keep a connection stable through a caller's atomic profile/manifest save.

        Callers must not enter another provider-store transaction inside this context.
        The connection reference callback only reads atomic files; it takes no meeting lock.
        """
        if config.minutes_model is None:
            yield {}
            return
        with self.service.store.transaction() as document:
            yield self.validate_in_transaction(config, document)

    def validate_in_transaction(
        self, config: MeetingConfig, document: dict[str, Any]
    ) -> dict[str, Any]:
        """Validate with the caller's provider transaction while atomically saving config."""
        if config.minutes_model is None:
            return {}
        return copy.deepcopy(self._selection(config, document).params)

    def validate(self, config: MeetingConfig) -> dict[str, Any]:
        with self.validated(config) as params:
            return params

    def resolve(
        self, config: MeetingConfig, *, should_cancel: CancelCheck | None = None
    ) -> LLMProvider:
        if config.minutes_model is None:
            return select_provider(config)
        with self.service.store.transaction() as document:
            selection = self._selection(config, document)
        return _ConnectedMinutesProvider(self, config, selection, should_cancel)

    def _selection(self, config: MeetingConfig, document: dict[str, Any]) -> _Selection:
        if config.minutes_model is None:
            raise InvalidArgumentError("A minutes model selection is required")
        try:
            # Revalidate mutable nested values too; bools and floats are not token counts.
            selected = ModelSelection.model_validate(
                config.minutes_model.model_dump(warnings=False)
            )
        except ModelValidationError:
            raise InvalidArgumentError("The minutes model selection is invalid") from None
        if self.service.closed.is_set():
            raise EngineUnavailableError("Provider service is closed")
        spec = _PROVIDERS[selected.provider]
        # Check send policy before consulting authentication or model metadata.
        check_policy(spec.profile(), config.external_send_policy, provider=selected.provider)
        record = connection(document, selected.connection_id)
        if record["provider_id"] != selected.provider:
            raise ConfigurationConflictError("The minutes connection belongs to another provider")
        if record["enabled"] is not True:
            raise ConfigurationConflictError("The minutes connection is disabled")
        check_revision(record, selected.connection_revision)
        endpoint = validate_endpoint(selected.provider, record["endpoint"])
        if selected.provider == "openai-compatible-api":
            _validate_openai_compatible_settings(record, endpoint)
        secret_account = self._authentication(record, spec)
        runtime = self.service.runtime._current(selected.provider, document)
        if runtime["state"] != "ready" or not runtime.get("version"):
            raise EngineUnavailableError("Prepare the provider runtime before selecting a model")
        catalog = document["catalogs"].get(selected.connection_id, {})
        if (
            record["catalog_state"] != "ready"
            or catalog.get("connection_revision") != selected.connection_revision
            or catalog.get("runtime_catalog_revision") != runtime["catalog_revision"]
        ):
            raise ModelUnavailableError("Refresh this connection's model catalog before generation")
        matches = [
            model
            for model in catalog.get("models", [])
            if model.get("model_id") == selected.model_id
        ]
        if len(matches) != 1:
            raise ModelUnavailableError("The selected model is not in the verified catalog")
        model = matches[0]
        if (
            model.get("availability") != "available"
            or model.get("source") != spec.source
            or "llm" not in model.get("roles", [])
            or "text" not in model.get("input_modalities", [])
            or "text" not in model.get("output_modalities", [])
            or model.get("billing", {}).get("kind") != spec.cost_class
        ):
            raise ModelUnavailableError("The selected model is not verified for text generation")
        verification = None
        if selected.provider in {"claude-agent-sdk", "openai-compatible-api"}:
            verification = model_verification_evidence(catalog, model, runtime["catalog_revision"])
            if verification is None:
                raise ModelUnavailableError(
                    "Run the explicit generation probe before selecting this model"
                )
        _check_availability(model)
        context = _positive_capability(model, "context_window")
        output_limit = _positive_capability(model, "max_output_tokens")
        if selected.provider == "ollama" and not re.fullmatch(
            r"(?:sha256:)?[a-f0-9]{64}", model.get("resolved_revision") or ""
        ):
            raise ModelUnavailableError("The local model digest has not been verified")
        parameters = self._parameters(selected.provider, model, selected.parameters)
        resources = runtime.get("resources", [])
        if len(resources) != 1 or not resources[0].get("sha256"):
            raise EngineUnavailableError("The provider runtime fingerprint is unavailable")
        expected_runtime = expected_runtime_evidence(self.service, selected.provider, runtime)
        params = {
            "minutes_model": selected.model_dump(mode="json"),
            "model_id": selected.model_id,
            "resolved_revision": model.get("resolved_revision"),
            "effective_parameters": parameters,
            "runtime_version": runtime["version"],
            "runtime_sha256": resources[0]["sha256"],
            "runtime_catalog_revision": runtime["catalog_revision"],
            "adapter_version": ADAPTER_VERSION,
            "context_window": context,
            "max_output_tokens": output_limit,
            "data_destination": spec.destination,
            "cost_class": spec.cost_class,
        }
        # Retain the exact v0.3 Codex fingerprint: adding HTTP adapters alone must not
        # cause existing subscription minutes to be sent again.
        if selected.provider != PROVIDER_ID:
            params["endpoint"] = endpoint
            params["model_capabilities_sha256"] = _capabilities_fingerprint(model)
        if selected.provider == "openai-api":
            from narumi.providers.metadata.openai_capabilities import (
                CAPABILITY_TABLE_VERSION,
                model_capabilities,
            )

            if model_capabilities(selected.model_id) is None:
                raise ModelUnavailableError("The selected OpenAI model's capabilities are unknown")
            params["capability_table_version"] = CAPABILITY_TABLE_VERSION
        if selected.provider == "openai-compatible-api":
            params["api_surface"] = record.get("api_surface")
            params["chat_max_tokens_field"] = record.get("chat_max_tokens_field")
        if verification is not None:
            params["model_verification_sha256"] = verification["fingerprint"]
            params["model_verified_at"] = verification["verified_at"]
        profile = CapabilityProfile(
            vision=False,
            # Zero is the chunker's unknown-capacity sentinel, not a model capability.
            context_window=context or 0,
            cost_class=spec.cost_class,
            data_destination=spec.destination,
            tool_use=False,
            max_output_tokens=output_limit or parameters.get("max_tokens", DEFAULT_OUTPUT_TOKENS),
        )
        return _Selection(
            params,
            profile,
            selected.connection_id,
            selected.provider,
            selected.model_id,
            copy.deepcopy(model),
            parameters,
            endpoint,
            record["auth_method"],
            secret_account,
            record.get("api_surface"),
            record.get("chat_max_tokens_field"),
            expected_runtime,
        )

    def _authentication(self, record: dict[str, Any], spec: _ProviderSpec) -> str | None:
        if (
            record["auth_method"] not in spec.auth_methods
            or record["auth_state"] != "authenticated"
        ):
            raise AuthenticationRequiredError("The minutes connection must be verified again")
        if record.get("active_auth") is not None:
            raise AuthenticationRequiredError(
                "Provider authentication must finish before generation"
            )
        if record["auth_method"] == "none":
            if record["credential_present"] or record.get("secret_account") is not None:
                raise AuthenticationRequiredError(
                    "This unauthenticated connection cannot use API credentials"
                )
            return None
        if record["credential_present"] is not True:
            raise AuthenticationRequiredError("The minutes connection requires authentication")
        if record["auth_method"] == "chatgpt":
            return None
        account = record.get("secret_account")
        prefix = f"providers:{self.service.namespace}:{record['connection_id']}:"
        if (
            not isinstance(account, str)
            or not account.startswith(prefix)
            or not re.fullmatch(r"[a-f0-9]{32}", account[len(prefix) :])
        ):
            raise AuthenticationRequiredError("The selected connection's credential is unavailable")
        return account

    @staticmethod
    def _parameters(
        provider_id: str, model: dict[str, Any], requested: dict[str, Any]
    ) -> dict[str, Any]:
        schema = model.get("parameter_schema", {})
        if (
            schema.get("type") != "object"
            or schema.get("additionalProperties") is not False
            or not isinstance(schema.get("properties"), dict)
        ):
            raise ModelUnavailableError("The model's parameter capabilities are unavailable")
        result = dict(requested)
        effort = schema["properties"].get("reasoning_effort", {})
        if "reasoning_effort" not in result and "default" in effort:
            result["reasoning_effort"] = effort["default"]
        if provider_id not in {PROVIDER_ID, "claude-agent-sdk"}:
            known_max = _positive_capability(model, "max_output_tokens")
            result.setdefault(
                "max_tokens", min(DEFAULT_OUTPUT_TOKENS, known_max or DEFAULT_OUTPUT_TOKENS)
            )
            value = result["max_tokens"]
            if (
                type(value) is not int
                or not 1 <= value <= MAX_OUTPUT_TOKENS
                or (known_max is not None and value > known_max)
            ):
                raise InvalidArgumentError("The selected model's output token limit is invalid")
        if set(result) - MINUTES_PARAMETER_NAMES[provider_id] or (
            "reasoning_effort" in result and type(result["reasoning_effort"]) is not str
        ):
            raise InvalidArgumentError("The selected minutes parameters are not supported")
        try:
            Draft202012Validator.check_schema(schema)
            Draft202012Validator(schema).validate(result)
        except (SchemaError, ValidationError):
            raise InvalidArgumentError(
                "The selected model does not support these parameters"
            ) from None
        return result


def _positive_capability(model: dict[str, Any], field: str) -> int | None:
    value = model.get(field)
    if value is not None and (type(value) is not int or value <= 0):
        raise ModelUnavailableError("The model's capacity has not been verified")
    return value


def _validate_openai_compatible_settings(record: dict[str, Any], endpoint: str) -> None:
    surface = record.get("api_surface")
    token_field = record.get("chat_max_tokens_field")
    if surface == "responses":
        if token_field is not None:
            raise ConfigurationConflictError("The compatible API settings are inconsistent")
    elif surface == "chat_completions":
        if token_field not in {"max_tokens", "max_completion_tokens"}:
            raise ConfigurationConflictError("The compatible API settings are incomplete")
    else:
        raise ConfigurationConflictError("The compatible API surface is not configured")
    if record.get("auth_method") != "none":
        return
    host = urlsplit(endpoint).hostname
    try:
        address = ipaddress.ip_address(host or "")
    except ValueError:
        address = None
    if address is None or not address.is_loopback:
        raise AuthenticationRequiredError(
            "Unauthenticated compatible APIs are restricted to numeric loopback"
        )


def _check_availability(model: dict[str, Any]) -> None:
    value = model.get("availability_expires_on")
    if value is None:
        return
    try:
        if not isinstance(value, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
            raise ValueError
        expires = date.fromisoformat(value)
    except ValueError:
        raise ModelUnavailableError(
            "The selected model's availability could not be verified"
        ) from None
    # Providers report a date without a time zone. Refuse from that UTC date as a
    # conservative app rule; do not present it as the provider's exact shutdown time.
    if datetime.now(UTC).date() >= expires:
        raise ModelUnavailableError("The selected model's availability date has expired")


def _capabilities_fingerprint(model: dict[str, Any]) -> str:
    # Observation/display timestamps do not change the selected model or its semantics.
    fields = (
        "model_id",
        "resolved_revision",
        "source",
        "roles",
        "input_modalities",
        "output_modalities",
        "context_window",
        "max_output_tokens",
        "parameter_schema",
    )
    return sha256_params(
        {**{key: model.get(key) for key in fields}, "billing_kind": model["billing"]["kind"]}
    )


class _ConnectedMinutesProvider:
    def __init__(
        self,
        resolver: MinutesResolver,
        config: MeetingConfig,
        selection: _Selection,
        should_cancel: CancelCheck | None,
    ) -> None:
        self.resolver = resolver
        self.config = config.model_copy(deep=True)
        self.selection = selection
        self.name = selection.provider_id
        self.profile = selection.profile
        self.generation_params = copy.deepcopy(selection.params)
        self.should_cancel = should_cancel
        self.last_completion_metadata: dict[str, Any] | None = None

    def _cancelled(self) -> bool:
        return self.resolver.service.closed.is_set() or bool(
            self.should_cancel is not None and self.should_cancel()
        )

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        images: list[Path] | None = None,
        max_tokens: int | None = None,
    ) -> str:
        self.last_completion_metadata = None
        if images:
            raise InvalidArgumentError("Connected minutes generation accepts text only")
        if max_tokens is not None:
            # All output limits belong to the saved selection/fingerprint, not a caller
            # override. Codex cannot enforce such a limit at all.
            raise InvalidArgumentError("Use the saved model selection for output token limits")
        if self._cancelled():
            raise CancelledError("Minutes generation was cancelled")
        service = self.resolver.service
        token = uuid.uuid4().hex
        with service.store.transaction() as document:
            current = self.resolver._selection(self.config, document)
            if sha256_params(current.params) != sha256_params(self.generation_params):
                raise ConfigurationConflictError(
                    "The selected model or runtime changed during generation"
                )
            check_provider_idle(document, self.name)
            document["checks"][self.name] = {
                "token": token,
                "server_instance_id": service.server_instance_id,
                "connection_id": self.selection.connection_id,
                "kind": "generation",
            }
            document["connections"][self.selection.connection_id]["last_generation_state"] = (
                "unknown"
            )
        state = "unknown"
        try:
            if self._cancelled():
                raise CancelledError("Minutes generation was cancelled")
            if self.name == PROVIDER_ID:
                result = service.codex_backend.complete(
                    current.connection_id,
                    current.model_id,
                    dict(current.parameters),
                    prompt,
                    system=system,
                    should_cancel=self._cancelled,
                )
            elif self.name == "claude-agent-sdk":
                result = self._claude_complete(current, prompt, system)
            elif self.name == "openai-compatible-api":
                result = self._openai_compatible_complete(current, prompt, system)
            else:
                result = self._http_complete(current, prompt, system)
            if not isinstance(result, str) or not result.strip():
                raise _safe_error(ErrorCode.ENGINE_UNAVAILABLE, unknown=True)
            state = "succeeded"
            return result
        except CancelledError as error:
            unknown = _outcome_unknown(error)
            state = "unknown" if unknown else "cancelled"
            raise CancelledError(
                "Minutes generation was cancelled",
                details={
                    "outcome_unknown": unknown,
                    **({"reason": OUTCOME_UNKNOWN} if unknown else {}),
                },
            ) from None
        except NarumiError as error:
            unknown = _outcome_unknown(error)
            state = "unknown" if unknown else "failed"
            raise _safe_error(error.code, unknown=unknown) from None
        except Exception:
            raise _safe_error(ErrorCode.ENGINE_UNAVAILABLE, unknown=True) from None
        finally:
            try:
                with service.store.transaction() as document:
                    service.catalog.release_check(document, self.name, token)
                    record = document["connections"].get(self.selection.connection_id)
                    if record is not None:
                        record["last_generation_state"] = state
            except Exception:
                # A known reply with an uncommitted receipt must not become retryable.
                raise _safe_error(ErrorCode.ENGINE_UNAVAILABLE, unknown=True) from None

    def _http_complete(self, current: _Selection, prompt: str, system: str | None) -> str:
        credential = self._credential(current)
        if self._cancelled():
            raise CancelledError("Minutes generation was cancelled")
        result = self.resolver.service.http_backend.complete(
            self.name,
            current.endpoint,
            credential,
            copy.deepcopy(current.model),
            dict(current.parameters),
            prompt,
            system=system,
            should_cancel=self._cancelled,
        )
        text, returned_model, usage = result.text, result.returned_model, result.usage
        if (
            not isinstance(text, str)
            or not text.strip()
            or not isinstance(returned_model, str)
            or not returned_model
            or len(returned_model) > 256
            or not returned_model.isprintable()
            or (credential is not None and (credential in text or credential in returned_model))
            or not _valid_usage(usage)
        ):
            raise _safe_error(ErrorCode.ENGINE_UNAVAILABLE, unknown=True)
        self.last_completion_metadata = {
            "returned_model": returned_model,
            "usage": copy.deepcopy(usage),
        }
        return text

    def _claude_complete(self, current: _Selection, prompt: str, system: str | None) -> str:
        credential = self._credential(current)
        if credential is None:
            raise AuthenticationRequiredError("The selected connection's credential is unavailable")
        result = self.resolver.service.claude_backend.complete(
            current.connection_id,
            credential,
            current.model_id,
            prompt,
            system=system,
            expected_runtime=current.expected_runtime,
            should_cancel=self._cancelled,
        )
        return self._validated_backend_result(
            current,
            credential,
            result,
            exact_model=True,
            expected_runtime=current.expected_runtime,
        )

    def _openai_compatible_complete(
        self, current: _Selection, prompt: str, system: str | None
    ) -> str:
        credential = self._credential(current)
        result = self.resolver.service.openai_compatible_backend.complete(
            current.endpoint,
            credential,
            copy.deepcopy(current.model),
            dict(current.parameters),
            prompt,
            auth_method=current.auth_method,
            api_surface=current.api_surface,
            chat_max_tokens_field=current.chat_max_tokens_field,
            system=system,
            should_cancel=self._cancelled,
        )
        return self._validated_backend_result(current, credential, result, exact_model=True)

    def _credential(self, current: _Selection) -> str | None:
        if current.secret_account is None:
            return None
        try:
            credential = self.resolver.service.secrets.get(current.secret_account)
        except Exception:
            raise AuthenticationRequiredError(
                "The selected connection's credential is unavailable"
            ) from None
        if not isinstance(credential, str) or not credential:
            raise AuthenticationRequiredError("The selected connection's credential is unavailable")
        return credential

    def _validated_backend_result(
        self,
        current: _Selection,
        credential: str | None,
        result: Any,
        *,
        exact_model: bool,
        expected_runtime: dict[str, str] | None = None,
    ) -> str:
        text = getattr(result, "text", None)
        returned_model = getattr(result, "returned_model", None)
        usage = getattr(result, "usage", None)
        if (
            not isinstance(text, str)
            or not text.strip()
            or not isinstance(returned_model, str)
            or not returned_model
            or len(returned_model) > 256
            or not returned_model.isprintable()
            or (exact_model and returned_model != current.model_id)
            or (credential is not None and (credential in text or credential in returned_model))
            or not _valid_usage(usage)
            or (
                expected_runtime is not None
                and not returned_runtime_matches(result, expected_runtime)
            )
        ):
            raise _safe_error(ErrorCode.ENGINE_UNAVAILABLE, unknown=True)
        self.last_completion_metadata = {
            "returned_model": returned_model,
            "usage": copy.deepcopy(usage),
        }
        return text.strip()


def _valid_usage(usage: Any) -> bool:
    return usage is None or (
        isinstance(usage, dict)
        and not (set(usage) - USAGE_FIELDS)
        and all(type(value) is int and 0 <= value <= 2**53 - 1 for value in usage.values())
    )


def _outcome_unknown(error: NarumiError) -> bool:
    return bool(error.details.get("outcome_unknown")) or error.details.get("reason") in {
        OUTCOME_UNKNOWN,
        LEGACY_OUTCOME_UNKNOWN,
    }


def _safe_error(code: ErrorCode, *, unknown: bool) -> NarumiError:
    messages = {
        ErrorCode.AUTHENTICATION_REQUIRED: "The provider authentication must be verified again",
        ErrorCode.MODEL_UNAVAILABLE: "The selected minutes model is unavailable",
        ErrorCode.INVALID_ARGUMENT: "The selected minutes generation parameters were rejected",
        ErrorCode.CONFIGURATION_CONFLICT: "The minutes connection changed during generation",
        ErrorCode.BUSY: "Another operation for the selected provider is already running",
    }
    return NarumiError(
        "The generation outcome is unknown; explicitly start a new attempt to resend"
        if unknown
        else messages.get(code, "Minutes generation failed"),
        code=ErrorCode.ENGINE_UNAVAILABLE if unknown else code,
        details={
            "reason": OUTCOME_UNKNOWN if unknown else "provider_generation_failed",
            "outcome_unknown": unknown,
        },
    )
