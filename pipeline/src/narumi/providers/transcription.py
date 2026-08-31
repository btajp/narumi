"""Resolve explicitly selected audio models and guard each connection-scoped upload."""

from __future__ import annotations

import copy
import re
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

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
    PolicyViolationError,
)
from narumi.models import ExternalSendPolicy, MeetingConfig
from narumi.providers._common import check_provider_idle, check_revision, connection
from narumi.providers.metadata import validate_endpoint
from narumi.providers.metadata.audio_capabilities import (
    AUDIO_CAPABILITY_TABLE_VERSION,
    audio_model_capabilities,
)
from narumi.providers.metadata.validation import check_public_payload
from narumi.transcription_selection import normalize_transcription_language

if TYPE_CHECKING:
    from narumi.providers.audio_response import AudioTranscriptionResult
    from narumi.providers.service import ProviderService

TRANSCRIPTION_MODEL_PROVIDERS = ("openai-api",)
OUTCOME_UNKNOWN = "provider_transcription_outcome_unknown"
CancelCheck = Callable[[], bool]
_HASH = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class _Selection:
    params: dict[str, Any]
    connection_id: str
    endpoint: str
    model_id: str
    language: str
    secret_account: str


class TranscriptionResolver:
    """Validation reads local observations only; credentials are read for an explicit upload."""

    def __init__(self, service: ProviderService) -> None:
        self.service = service

    @contextmanager
    def validated(self, config: MeetingConfig) -> Iterator[dict[str, Any]]:
        if config.transcription_model is None:
            yield {}
            return
        with self.service.store.transaction() as document:
            yield self.validate_in_transaction(config, document)

    def validate(self, config: MeetingConfig) -> dict[str, Any]:
        with self.validated(config) as params:
            return params

    def validate_in_transaction(
        self, config: MeetingConfig, document: dict[str, Any]
    ) -> dict[str, Any]:
        """Share one provider transaction with minutes validation and an atomic config save."""
        if config.transcription_model is None:
            return {}
        return copy.deepcopy(self._selection(config, document).params)

    def resolve(
        self, config: MeetingConfig, *, should_cancel: CancelCheck | None = None
    ) -> _ConnectedTranscriptionProvider:
        with self.service.store.transaction() as document:
            selection = self._selection(config, document)
        return _ConnectedTranscriptionProvider(self, config, selection, should_cancel)

    def _selection(self, config: MeetingConfig, document: dict[str, Any]) -> _Selection:
        try:
            config = MeetingConfig.model_validate(config.model_dump(warnings=False))
            language = normalize_transcription_language(config.language)
        except (ValidationError, ValueError, TypeError, AttributeError):
            raise InvalidArgumentError("The API transcription selection is invalid") from None
        selected = config.transcription_model
        if selected is None:
            raise InvalidArgumentError("An API transcription model selection is required")
        if self.service.closed.is_set():
            raise EngineUnavailableError("Provider service is closed")
        # Permission is checked before credential references, runtime inspection or uploads.
        if config.external_send_policy != ExternalSendPolicy.API_OK:
            raise PolicyViolationError(
                "OpenAI audio transcription requires explicit api_ok permission",
                details={
                    "provider": "openai-api",
                    "data_destination": "openai",
                    "cost_class": "api",
                    "required_policy": "api_ok",
                },
            )
        record = connection(document, selected.connection_id)
        if record["provider_id"] != "openai-api" or record["enabled"] is not True:
            raise ConfigurationConflictError("The selected audio connection is unavailable")
        check_revision(record, selected.connection_revision)
        endpoint = validate_endpoint("openai-api", record["endpoint"])
        secret_account = _credential_account(self.service, record)
        runtime = self.service.runtime._current("openai-api", document)
        runtime_sha = _runtime_fingerprint(runtime)
        catalog = document["catalogs"].get(selected.connection_id, {})
        if (
            record["catalog_state"] != "ready"
            or catalog.get("connection_revision") != selected.connection_revision
            or catalog.get("runtime_catalog_revision") != runtime["catalog_revision"]
        ):
            raise ModelUnavailableError("Refresh the selected audio model catalog")
        observed = catalog.get("models", [])
        if not isinstance(observed, list) or any(not isinstance(model, dict) for model in observed):
            raise ModelUnavailableError("The audio model catalog could not be verified")
        matches = [model for model in observed if model.get("model_id") == selected.model_id]
        if len(matches) != 1:
            raise ModelUnavailableError("The selected audio model is not in the verified catalog")
        model = matches[0]
        capabilities = audio_model_capabilities(selected.model_id)
        if (
            capabilities is None
            or capabilities.availability != "available"
            or capabilities.response_format not in {"verbose_json", "diarized_json"}
            or capabilities.timestamp_support not in {"word", "diarized_segment"}
            or model.get("availability") != "available"
            or model.get("source") != "provider_api"
            or model.get("roles") != ["transcription"]
            or model.get("input_modalities") != ["audio"]
            or model.get("output_modalities") != ["text"]
            or model.get("timestamp_support") != capabilities.timestamp_support
            or model.get("parameter_schema") != capabilities.parameter_schema()
            or not isinstance(model.get("billing"), dict)
            or model.get("billing", {}).get("kind") != "api"
        ):
            raise ModelUnavailableError("The audio model has no verified native timestamp format")
        _check_expiry(model)
        from narumi.providers.audio_transcription import (
            AUDIO_ADAPTER_VERSION,
            fixed_transcription_parameters,
        )

        params = {
            "provider": "openai-api",
            "connection_id": selected.connection_id,
            "connection_revision": selected.connection_revision,
            "model_id": selected.model_id,
            "language": language or "auto",
            "effective_parameters": fixed_transcription_parameters(
                selected.model_id, config.language
            ),
            "adapter_version": AUDIO_ADAPTER_VERSION,
            "capability_table_version": AUDIO_CAPABILITY_TABLE_VERSION,
            "runtime_version": runtime["version"],
            "runtime_sha256": runtime_sha,
            "runtime_catalog_revision": runtime["catalog_revision"],
            "model_capabilities_sha256": sha256_params(
                {
                    "model_id": model["model_id"],
                    "resolved_revision": model.get("resolved_revision"),
                    "response_format": capabilities.response_format,
                    "timestamp_support": capabilities.timestamp_support,
                    "wire_parameters": capabilities.wire_parameters,
                }
            ),
            "endpoint": endpoint,
        }
        return _Selection(
            params,
            selected.connection_id,
            endpoint,
            selected.model_id,
            config.language,
            secret_account,
        )


def _credential_account(service: ProviderService, record: dict[str, Any]) -> str:
    if (
        record["auth_method"] != "api_key"
        or record["auth_state"] != "authenticated"
        or record.get("active_auth") is not None
        or record["credential_present"] is not True
    ):
        raise AuthenticationRequiredError("The audio connection must be authenticated again")
    account = record.get("secret_account")
    prefix = f"providers:{service.namespace}:{record['connection_id']}:"
    if (
        not isinstance(account, str)
        or not account.startswith(prefix)
        or re.fullmatch(r"[a-f0-9]{32}", account[len(prefix) :]) is None
    ):
        raise AuthenticationRequiredError("The selected audio connection has no saved credential")
    return account


def _runtime_fingerprint(runtime: dict[str, Any]) -> str:
    resources = runtime.get("resources", [])
    if (
        runtime.get("state") != "ready"
        or not isinstance(runtime.get("version"), str)
        or not runtime["version"]
        or not isinstance(runtime.get("catalog_revision"), str)
        or _HASH.fullmatch(runtime["catalog_revision"]) is None
        or not isinstance(resources, list)
        or len(resources) != 1
        or not isinstance(resources[0], dict)
        or resources[0].get("resource_id") != "openai-client"
        or not isinstance(resources[0].get("sha256"), str)
        or _HASH.fullmatch(resources[0]["sha256"]) is None
    ):
        raise EngineUnavailableError("Prepare the audio provider runtime before selecting a model")
    return resources[0]["sha256"]


def _check_expiry(model: dict[str, Any]) -> None:
    value = model.get("availability_expires_on")
    if value is None:
        return
    try:
        if not isinstance(value, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
            raise ValueError
        expires = date.fromisoformat(value)
    except ValueError:
        raise ModelUnavailableError("The audio model's availability is unverified") from None
    if datetime.now(UTC).date() >= expires:
        raise ModelUnavailableError("The selected audio model's availability date has expired")


class _ConnectedTranscriptionProvider:
    def __init__(self, resolver, config, selection, should_cancel) -> None:
        self.resolver = resolver
        self._config = config.model_copy(deep=True)
        self._selection = selection
        self.transcription_params = copy.deepcopy(selection.params)
        self.should_cancel = should_cancel

    def _cancelled(self) -> bool:
        return self.resolver.service.closed.is_set() or bool(
            self.should_cancel is not None and self.should_cancel()
        )

    def transcribe_chunk(self, audio: bytes, duration_sec: float) -> AudioTranscriptionResult:
        """Upload one anonymous chunk; its durable attempt/result ledger belongs to the caller."""
        if self._cancelled():
            raise CancelledError("Audio transcription was cancelled")
        service = self.resolver.service
        token = uuid.uuid4().hex
        with service.store.transaction() as document:
            current = self.resolver._selection(self._config, document)
            if sha256_params(current.params) != sha256_params(self.transcription_params):
                raise ConfigurationConflictError("The selected audio model or runtime changed")
            check_provider_idle(document, "openai-api")
            document["checks"]["openai-api"] = {
                "token": token,
                "server_instance_id": service.server_instance_id,
                "connection_id": current.connection_id,
                "kind": "generation",
            }
        try:
            if self._cancelled():
                raise CancelledError("Audio transcription was cancelled")
            credential = self._credential(current.secret_account)
            if self._cancelled():
                raise CancelledError("Audio transcription was cancelled")
            result = service.audio_backend.transcribe(
                current.endpoint,
                credential,
                current.model_id,
                audio,
                language=current.language,
                parameters={},
                chunk_duration=duration_sec,
                should_cancel=self._cancelled,
            )
            return _verified_result(result, current.model_id, duration_sec, credential)
        except CancelledError as error:
            raise CancelledError(
                "Audio transcription was cancelled",
                details={"outcome_unknown": bool(error.details.get("outcome_unknown"))},
            ) from None
        except NarumiError as error:
            raise _safe_error(
                error.code, unknown=bool(error.details.get("outcome_unknown"))
            ) from None
        except Exception:
            raise _safe_error(ErrorCode.ENGINE_UNAVAILABLE, unknown=True) from None
        finally:
            try:
                with service.store.transaction() as document:
                    service.catalog.release_check(document, "openai-api", token)
            except Exception:
                raise _safe_error(ErrorCode.ENGINE_UNAVAILABLE, unknown=True) from None

    def _credential(self, account: str) -> str:
        try:
            credential = self.resolver.service.secrets.get(account)
        except Exception:
            raise AuthenticationRequiredError("The saved audio credential is unavailable") from None
        if (
            not isinstance(credential, str)
            or not 1 <= len(credential) <= 4096
            or any(not 33 <= ord(char) <= 126 for char in credential)
        ):
            raise AuthenticationRequiredError("The saved audio credential is unavailable")
        return credential


def _verified_result(result, model_id: str, duration: float, credential: str):
    from narumi.providers.audio_response import MAX_AUDIO_RESPONSE_NODES, parse_saved_result

    try:
        payload = asdict(result)
        # asdict retains tuples, while the public JSON guard walks lists. Inspect
        # every native label/word before returning anything that a ledger may save.
        for field in ("segments", "words"):
            if isinstance(payload.get(field), tuple):
                payload[field] = list(payload[field])
        check_public_payload(
            payload,
            secrets=(credential, "Bearer " + credential),
            reject_credentials=False,
            max_nodes=MAX_AUDIO_RESPONSE_NODES,
        )
        return parse_saved_result(payload, model_id=model_id, chunk_duration=duration)
    except Exception:
        raise _safe_error(ErrorCode.ENGINE_UNAVAILABLE, unknown=True) from None


def _safe_error(code: ErrorCode, *, unknown: bool) -> NarumiError:
    return NarumiError(
        "The audio transcription outcome is unknown; explicitly confirm a retry"
        if unknown
        else "The selected audio transcription could not be completed",
        code=ErrorCode.ENGINE_UNAVAILABLE if unknown else code,
        details={
            "reason": OUTCOME_UNKNOWN if unknown else "provider_transcription_failed",
            "outcome_unknown": unknown,
        },
    )
