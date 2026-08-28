"""Resolve a saved, minutes-only model selection without ambient credentials.

Configuration checks only inspect local connection and model observations. Every actual
completion repeats those checks and holds the provider's durable operation lease until the
reply is known, so authentication and runtime changes cannot race an outgoing request.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

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
from narumi.llm.base import CapabilityProfile, LLMProvider
from narumi.llm.policy import check_policy
from narumi.llm.registry import select_provider
from narumi.models import MeetingConfig
from narumi.providers._common import check_provider_idle, check_revision, connection

if TYPE_CHECKING:
    from narumi.providers.service import ProviderService

PROVIDER_ID = "codex-app-server"
ADAPTER_VERSION = "1"
OUTCOME_UNKNOWN = "codex_generation_outcome_unknown"
CancelCheck = Callable[[], bool]


@dataclass(frozen=True)
class _Selection:
    params: dict[str, Any]
    profile: CapabilityProfile
    connection_id: str
    model_id: str
    parameters: dict[str, Any]


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
            yield copy.deepcopy(self._selection(config, document).params)

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
        selected = config.minutes_model
        if selected is None:
            raise InvalidArgumentError("A minutes model selection is required")
        if self.service.closed.is_set():
            raise EngineUnavailableError("Provider service is closed")
        # Check send policy before consulting authentication or model metadata.
        profile = CapabilityProfile(False, 0, "subscription", "openai", False)
        check_policy(profile, config.external_send_policy, provider=PROVIDER_ID)
        record = connection(document, selected.connection_id)
        if record["provider_id"] != selected.provider:
            raise ConfigurationConflictError("The minutes connection belongs to another provider")
        if not record["enabled"]:
            raise ConfigurationConflictError("The minutes connection is disabled")
        check_revision(record, selected.connection_revision)
        if (
            record["auth_method"] != "chatgpt"
            or record["auth_state"] != "authenticated"
            or not record["credential_present"]
        ):
            raise AuthenticationRequiredError(
                "Codex minutes require verified ChatGPT authentication"
            )
        if record.get("active_auth") is not None:
            raise AuthenticationRequiredError("Codex authentication must finish before generation")
        runtime = self.service.runtime._current(PROVIDER_ID, document)
        if runtime["state"] != "ready" or not runtime.get("version"):
            raise EngineUnavailableError("Prepare the Codex runtime before selecting a model")
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
            raise ModelUnavailableError("The selected Codex model is not in the verified catalog")
        model = matches[0]
        if (
            model.get("availability") != "available"
            or model.get("source") != "runtime"
            or "llm" not in model.get("roles", [])
            or "text" not in model.get("input_modalities", [])
            or "text" not in model.get("output_modalities", [])
            or model.get("billing", {}).get("kind") != "subscription"
        ):
            raise ModelUnavailableError(
                "The selected model is not verified for ChatGPT text generation"
            )
        parameters = self._parameters(model, selected.parameters)
        resources = runtime.get("resources", [])
        if len(resources) != 1 or not resources[0].get("sha256"):
            raise EngineUnavailableError("The Codex runtime fingerprint is unavailable")
        params = {
            "minutes_model": selected.model_dump(mode="json"),
            "model_id": selected.model_id,
            "resolved_revision": model.get("resolved_revision"),
            "effective_parameters": parameters,
            "runtime_version": runtime["version"],
            "runtime_sha256": resources[0]["sha256"],
            "runtime_catalog_revision": runtime["catalog_revision"],
            "adapter_version": ADAPTER_VERSION,
            "context_window": model.get("context_window"),
            "max_output_tokens": model.get("max_output_tokens"),
            "data_destination": "openai",
            "cost_class": "subscription",
        }
        profile = CapabilityProfile(
            vision=False,
            # An unreported window uses the chunker's conservative minimum budget; it is
            # not represented as a claimed provider capability in provenance.
            context_window=model.get("context_window") or 0,
            cost_class="subscription",
            data_destination="openai",
            tool_use=False,
            max_output_tokens=model.get("max_output_tokens") or 4096,
        )
        return _Selection(params, profile, selected.connection_id, selected.model_id, parameters)

    @staticmethod
    def _parameters(model: dict[str, Any], requested: dict[str, Any]) -> dict[str, Any]:
        schema = model.get("parameter_schema", {})
        if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
            raise ModelUnavailableError("The model's parameter capabilities are unavailable")
        result = dict(requested)
        effort = schema.get("properties", {}).get("reasoning_effort", {})
        if "reasoning_effort" not in result and "default" in effort:
            result["reasoning_effort"] = effort["default"]
        try:
            Draft202012Validator.check_schema(schema)
            Draft202012Validator(schema).validate(result)
        except (SchemaError, ValidationError):
            raise InvalidArgumentError(
                "The selected Codex model does not support these parameters"
            ) from None
        if set(result) - {"reasoning_effort"} or any(
            not isinstance(v, str) for v in result.values()
        ):
            raise InvalidArgumentError("The selected Codex parameters are not supported")
        return result


class _ConnectedMinutesProvider:
    name = PROVIDER_ID

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
        self.profile = selection.profile
        self.generation_params = copy.deepcopy(selection.params)
        self.should_cancel = should_cancel

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
        if images:
            raise InvalidArgumentError("Codex minutes generation accepts text only")
        if max_tokens is not None:
            raise InvalidArgumentError("The Codex adapter cannot enforce a token output limit")
        if self._cancelled():
            raise CancelledError("Codex minutes generation was cancelled")
        service = self.resolver.service
        token = uuid.uuid4().hex
        with service.store.transaction() as document:
            current = self.resolver._selection(self.config, document)
            if sha256_params(current.params) != sha256_params(self.generation_params):
                raise ConfigurationConflictError(
                    "The selected model or runtime changed during generation"
                )
            check_provider_idle(document, PROVIDER_ID)
            document["checks"][PROVIDER_ID] = {
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
                raise CancelledError("Codex minutes generation was cancelled")
            result = service.codex_backend.complete(
                self.selection.connection_id,
                self.selection.model_id,
                dict(self.selection.parameters),
                prompt,
                system=system,
                should_cancel=self._cancelled,
            )
            if not isinstance(result, str) or not result.strip():
                raise EngineUnavailableError("Codex returned no usable minutes text")
            state = "succeeded"
            return result
        except CancelledError as error:
            unknown = bool(error.details.get("outcome_unknown", False))
            state = "unknown" if unknown else "cancelled"
            raise CancelledError(
                "Codex minutes generation was cancelled",
                details={"outcome_unknown": unknown},
            ) from None
        except NarumiError as error:
            unknown = error.details.get("reason") == OUTCOME_UNKNOWN
            state = "unknown" if unknown else "failed"
            raise _safe_error(error.code, unknown=unknown) from None
        except Exception:
            raise _safe_error(ErrorCode.ENGINE_UNAVAILABLE, unknown=True) from None
        finally:
            try:
                with service.store.transaction() as document:
                    service.catalog.release_check(document, PROVIDER_ID, token)
                    record = document["connections"].get(self.selection.connection_id)
                    if record is not None:
                        record["last_generation_state"] = state
            except Exception:
                # A known reply with an uncommitted receipt must not become retryable.
                raise _safe_error(ErrorCode.ENGINE_UNAVAILABLE, unknown=True) from None


def _safe_error(code: ErrorCode, *, unknown: bool) -> NarumiError:
    messages = {
        ErrorCode.AUTHENTICATION_REQUIRED: "Codex ChatGPT authentication must be verified again",
        ErrorCode.MODEL_UNAVAILABLE: "The selected Codex model is unavailable",
        ErrorCode.INVALID_ARGUMENT: "The selected Codex generation parameters were rejected",
        ErrorCode.CONFIGURATION_CONFLICT: "The Codex connection changed during generation",
        ErrorCode.BUSY: "Another Codex operation is already running",
    }
    return NarumiError(
        "The Codex generation outcome is unknown; explicitly start a new attempt to resend"
        if unknown
        else messages.get(code, "Codex minutes generation failed"),
        code=ErrorCode.ENGINE_UNAVAILABLE if unknown else code,
        details={"reason": OUTCOME_UNKNOWN if unknown else "codex_generation_failed"},
    )
