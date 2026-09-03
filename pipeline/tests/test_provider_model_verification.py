"""Explicit Claude SDK and OpenAI-compatible model verification integration."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from narumi.errors import (
    BusyError,
    ConfigurationConflictError,
    EngineUnavailableError,
    InvalidArgumentError,
    ModelUnavailableError,
    NarumiError,
)
from narumi.model_selection import ModelSelection
from narumi.models import MeetingConfig
from narumi.providers.generation import MinutesResolver
from narumi.providers.metadata import MetadataClient
from narumi.providers.metadata.openai_compatible import model_descriptor
from narumi.providers.runtime_catalog import RuntimeInspector, _claude_runtime_digest
from narumi.providers.service import ProviderService

from .provider_fakes import FakeCodexBackend, MemorySecretStore

SECRET = "fixture-model-probe-key-7219"
MODEL_IDS = {
    "claude-agent-sdk": "claude-fixture-text-model",
    "openai-compatible-api": "compatible-fixture-text-model",
}
CLAUDE_RUNTIME_EVIDENCE = {
    "resource_id": "claude-agent-sdk-0-2-144",
    "sdk_version": "0.2.144",
    "cli_version": "2.1.239",
    "cli_sha256": "1" * 64,
    "sdk_source_sha256": "2" * 64,
    "isolation_profile_sha256": "3" * 64,
}


def candidate(provider_id: str) -> dict:
    bounded = provider_id == "openai-compatible-api"
    return {
        "model_id": MODEL_IDS[provider_id],
        "display_name": f"{provider_id} fixture",
        "resolved_revision": None,
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "roles": ["llm"],
        "timestamp_support": "none",
        "context_window": None,
        "max_output_tokens": 2048 if bounded else None,
        "parameter_schema": {
            "type": "object",
            "properties": (
                {
                    "max_tokens": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 2048,
                        "default": 2048,
                    }
                }
                if bounded
                else {}
            ),
            "required": [],
            "additionalProperties": False,
        },
        "availability": "unverified",
        "availability_expires_on": None,
        "reason": "adapter_capability_verification_required",
        "source": "provider_api",
        "fetched_at": "2026-09-02T00:00:00Z",
        "billing": {
            "kind": "api",
            "input_usd_per_million_tokens": None,
            "output_usd_per_million_tokens": None,
            "audio_usd_per_minute": None,
            "fetched_at": None,
        },
    }


def discovered_compatible_candidate() -> dict:
    """The generic /models route proves identity, not generation capabilities."""
    model = candidate("openai-compatible-api")
    model.update(
        input_modalities=[],
        output_modalities=[],
        roles=[],
        context_window=None,
        max_output_tokens=None,
        parameter_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    )
    return model


class VerificationMetadata:
    def __init__(self):
        self.calls: list[tuple] = []
        self.expected_api_key = SECRET
        self.compatible_display_name = candidate("openai-compatible-api")["display_name"]
        self.compatible_resolved_revision: str | None = None

    def fetch(self, provider_id: str, endpoint: str, api_key: str | None) -> list[dict]:
        assert provider_id == "claude-agent-sdk"
        assert endpoint == "https://api.anthropic.com"
        assert api_key == self.expected_api_key
        self.calls.append((provider_id, endpoint))
        return [copy.deepcopy(candidate(provider_id))]

    def fetch_openai_compatible(
        self,
        endpoint: str,
        api_key: str | None,
        *,
        auth_method: str,
        api_surface: str,
    ) -> list[dict]:
        assert endpoint == "https://127.0.0.1:9443/v1"
        assert api_key == self.expected_api_key
        assert auth_method == "api_key"
        assert api_surface in {"responses", "chat_completions"}
        self.calls.append(("openai-compatible-api", endpoint, api_surface))
        model = discovered_compatible_candidate()
        model["display_name"] = self.compatible_display_name
        model["resolved_revision"] = self.compatible_resolved_revision
        return [model]


class VerificationRuntimeInspector(RuntimeInspector):
    def __init__(self):
        super().__init__()
        self.claude_evidence = copy.deepcopy(CLAUDE_RUNTIME_EVIDENCE)
        self.compatible_version = "1.0.0"

    def _inspect_claude(self, *, refresh: bool = False) -> dict[str, str]:
        return copy.deepcopy(self.claude_evidence)

    def resource(self, provider_id: str) -> dict:
        assert provider_id in {"claude-agent-sdk", "openai-compatible-api"}
        if provider_id == "claude-agent-sdk":
            version = self.claude_evidence["sdk_version"]
            digest = _claude_runtime_digest(self.claude_evidence)
            resource_id = self.claude_evidence["resource_id"]
        else:
            version = self.compatible_version
            digest = "b" * 64
            resource_id = "openai-compatible-client"
        return {
            "resource_id": resource_id,
            "display_name": f"{provider_id} fixture runtime",
            "kind": "runtime",
            "version": version,
            "source": "installed",
            "download_host": None,
            "sha256": digest,
            "license": "Fixture license",
        }


class ProbeBackend:
    def __init__(self, provider_id: str):
        self.provider_id = provider_id
        self.verify_calls: list[tuple] = []
        self.complete_calls: list[tuple] = []
        self.verify_error: Exception | None = None
        self.verify_runtime_evidence: dict[str, str] | None = None
        self.complete_runtime_evidence: dict[str, str] | None = None
        self.compatible_resolved_revision: str | None = None

    def verify_model(self, *args, **kwargs):
        self.verify_calls.append(
            (
                copy.deepcopy(args),
                {key: value for key, value in kwargs.items() if key != "should_cancel"},
            )
        )
        if self.verify_error is not None:
            raise self.verify_error
        model_id = args[2]
        if self.provider_id == "openai-compatible-api":
            promoted = candidate(self.provider_id)
            promoted.update(
                availability="available",
                reason=None,
                resolved_revision=self.compatible_resolved_revision,
            )
            return promoted
        return SimpleNamespace(
            model_id=model_id,
            usage={"input_tokens": 1, "output_tokens": 1},
            runtime_evidence=copy.deepcopy(
                self.verify_runtime_evidence or kwargs.get("expected_runtime")
            ),
        )

    def complete(self, *args, **kwargs):
        self.complete_calls.append(
            (
                copy.deepcopy(args),
                {key: value for key, value in kwargs.items() if key != "should_cancel"},
            )
        )
        model_id = args[2] if self.provider_id == "claude-agent-sdk" else args[2]["model_id"]
        result = SimpleNamespace(
            text="## 議事録\n\n検証済みモデルの生成結果",
            returned_model=model_id,
            usage={"input_tokens": 4, "output_tokens": 8},
        )
        if self.provider_id == "claude-agent-sdk":
            result.runtime_evidence = copy.deepcopy(
                self.complete_runtime_evidence or kwargs.get("expected_runtime")
            )
        return result

    def close(self):
        pass

    def ensure_workspace_ready(self):
        pass


class RawModelsHTTP:
    def __init__(self, model_id: str):
        self.model_id = model_id
        self.calls: list[dict] = []

    def request(self, method: str, url: str, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return {
            "object": "list",
            "data": [
                {
                    "id": self.model_id,
                    "object": "model",
                    "created": 1,
                    "owned_by": "fixture",
                }
            ],
        }


class RawCompatibleProbeBackend(ProbeBackend):
    def __init__(self):
        super().__init__("openai-compatible-api")

    def verify_model(self, *args, **kwargs):
        self.verify_calls.append(
            (
                copy.deepcopy(args),
                {key: value for key, value in kwargs.items() if key != "should_cancel"},
            )
        )
        return model_descriptor(
            args[2],
            fetched_at="2026-09-02T00:00:00Z",
            verified=True,
        )


def prepared_service(
    tmp_path: Path,
    provider_id: str,
    *,
    compatible_resolved_revision: str | None = None,
):
    secrets = MemorySecretStore()
    metadata = VerificationMetadata()
    runtime = VerificationRuntimeInspector()
    backend = ProbeBackend(provider_id)
    metadata.compatible_resolved_revision = compatible_resolved_revision
    backend.compatible_resolved_revision = compatible_resolved_revision
    service = ProviderService(
        tmp_path,
        secret_store=secrets,
        metadata_client=metadata,
        runtime_inspector=runtime,
        codex_backend=FakeCodexBackend(),
        claude_backend=backend if provider_id == "claude-agent-sdk" else None,
        openai_compatible_backend=(backend if provider_id == "openai-compatible-api" else None),
    )
    args = {
        "provider_id": provider_id,
        "display_name": f"{provider_id} fixture",
        "auth_method": "api_key",
        "api_key": SECRET,
        "request_id": f"create-{provider_id}",
    }
    if provider_id == "openai-compatible-api":
        args.update(
            endpoint="https://127.0.0.1:9443/v1",
            api_surface="responses",
        )
    record = service.set_connection(args)["connection"]
    with service.store.transaction() as document:
        current = service.runtime._current(provider_id, document)
        current["state"] = "ready"
        document["runtimes"][provider_id] = current
    checked = service.test_connection(
        {
            "connection_id": record["connection_id"],
            "expected_revision": record["revision"],
        }
    )
    assert checked["connected"] is True
    return service, secrets, metadata, runtime, backend, checked["connection"]


def selection(record: dict, provider_id: str) -> MeetingConfig:
    return MeetingConfig(
        external_send_policy="api_ok",
        minutes_model=ModelSelection(
            provider=provider_id,
            connection_id=record["connection_id"],
            connection_revision=record["revision"],
            model_id=MODEL_IDS[provider_id],
            parameters={"max_tokens": 512} if provider_id == "openai-compatible-api" else {},
        ),
    )


def verify_args(record: dict, provider_id: str, request_id: str = "verify-model-001") -> dict:
    return {
        "connection_id": record["connection_id"],
        "expected_revision": record["revision"],
        "model_id": MODEL_IDS[provider_id],
        "confirmation": "send_test_prompt_and_may_charge",
        "request_id": f"{provider_id}-{request_id}",
    }


def expected_claude_runtime(runtime: VerificationRuntimeInspector) -> dict[str, str]:
    return {
        **runtime.claude_evidence,
        "resource_sha256": _claude_runtime_digest(runtime.claude_evidence),
    }


@pytest.mark.parametrize("provider_id", list(MODEL_IDS))
def test_explicit_probe_unlocks_selection_generation_and_respects_refresh_evidence(
    tmp_path: Path, provider_id: str
):
    service, _, metadata, runtime, backend, record = prepared_service(
        tmp_path / provider_id, provider_id
    )
    config = selection(record, provider_id)
    try:
        with pytest.raises(ModelUnavailableError):
            MinutesResolver(service).validate(config)
        assert backend.verify_calls == []

        refused = verify_args(record, provider_id)
        refused["confirmation"] = "not-confirmed"
        with pytest.raises(InvalidArgumentError):
            service.verify_model(refused)
        assert backend.verify_calls == []

        args = verify_args(record, provider_id)
        verified = service.verify_model(args)
        assert verified["model"]["model_id"] == MODEL_IDS[provider_id]
        assert verified["model"]["availability"] == "available"
        assert verified["model"]["reason"] is None
        assert len(backend.verify_calls) == 1
        if provider_id == "claude-agent-sdk":
            assert backend.verify_calls[0][1]["expected_runtime"] == expected_claude_runtime(
                runtime
            )

        assert service.verify_model(args) == verified
        assert len(backend.verify_calls) == 1

        provider = MinutesResolver(service).resolve(config)
        assert provider.complete("fixture meeting transcript").startswith("## 議事録")
        assert len(backend.complete_calls) == 1
        if provider_id == "claude-agent-sdk":
            assert backend.complete_calls[0][1]["expected_runtime"] == expected_claude_runtime(
                runtime
            )
            assert "runtime_evidence" not in provider.generation_params

        cached = service.list_models({"connection_id": record["connection_id"]})
        assert cached["models"][0]["availability"] == "available"

        refreshed = service.list_models({"connection_id": record["connection_id"], "refresh": True})
        assert len(metadata.calls) == 2
        if provider_id == "openai-compatible-api":
            assert refreshed["models"][0]["availability"] == "unverified"
            assert refreshed["models"][0]["reason"] == ("adapter_capability_verification_required")
            with pytest.raises(ModelUnavailableError):
                MinutesResolver(service).validate(config)
        else:
            assert refreshed["models"][0]["availability"] == "available"
            assert refreshed["models"][0]["reason"] is None
            MinutesResolver(service).validate(config)
        assert len(backend.verify_calls) == 1
    finally:
        service.close()


def test_raw_compatible_models_refresh_drops_unversioned_verification_proof(tmp_path: Path):
    root = tmp_path / "raw-compatible-models"
    model_id = MODEL_IDS["openai-compatible-api"]
    endpoint = "http://127.0.0.1:9443/v1"
    http = RawModelsHTTP(model_id)
    secrets = MemorySecretStore()
    metadata = MetadataClient(
        http=http,
        now=lambda: datetime(2026, 9, 2, tzinfo=UTC),
        monotonic=lambda: 0.0,
    )
    runtime = VerificationRuntimeInspector()
    backend = RawCompatibleProbeBackend()
    service = ProviderService(
        root,
        secret_store=secrets,
        metadata_client=metadata,
        runtime_inspector=runtime,
        codex_backend=FakeCodexBackend(),
        openai_compatible_backend=backend,
    )
    try:
        record = service.set_connection(
            {
                "provider_id": "openai-compatible-api",
                "display_name": "Raw compatible fixture",
                "endpoint": endpoint,
                "auth_method": "none",
                "api_surface": "responses",
                "request_id": "create-raw-compatible",
            }
        )["connection"]
        with service.store.transaction() as document:
            current = service.runtime._current("openai-compatible-api", document)
            current["state"] = "ready"
            document["runtimes"]["openai-compatible-api"] = current
        checked = service.test_connection(
            {
                "connection_id": record["connection_id"],
                "expected_revision": record["revision"],
            }
        )["connection"]
        discovered = service.list_models({"connection_id": record["connection_id"]})
        assert discovered["models"][0]["roles"] == []
        assert discovered["models"][0]["resolved_revision"] is None

        probe_args = verify_args(checked, "openai-compatible-api", "raw-models")
        service.verify_model(probe_args)
        receipt = service.store.read()["requests"][probe_args["request_id"]]
        assert receipt["credential_fingerprint_scheme"] == "sha256"
        assert service.store.read()["request_hmac_generation"] is None
        cached = service.list_models({"connection_id": record["connection_id"]})
        assert cached["models"][0]["availability"] == "available"
        assert cached["models"][0]["roles"] == ["llm"]

        refreshed = service.list_models({"connection_id": record["connection_id"], "refresh": True})
        assert refreshed["models"][0]["availability"] == "unverified"
        assert refreshed["models"][0]["roles"] == []
        assert refreshed["models"][0]["resolved_revision"] is None
        assert service.store.read()["catalogs"][record["connection_id"]]["verified_models"] == {}
        assert len(backend.verify_calls) == 1
        assert [call["url"] for call in http.calls] == [
            endpoint + "/models",
            endpoint + "/models",
        ]
        assert all(call["resolved_addresses"] == ("127.0.0.1",) for call in http.calls)

        service.close()
        reopened = ProviderService(
            root,
            secret_store=secrets,
            metadata_client=metadata,
            runtime_inspector=runtime,
            codex_backend=FakeCodexBackend(),
            openai_compatible_backend=backend,
        )
        assert reopened.list_connections()["connections"][0]["auth_method"] == "none"
        reopened.close()
    finally:
        service.close()


def test_compatible_refresh_preserves_proof_with_immutable_model_revision(tmp_path: Path):
    revision = "sha256:" + "7" * 64
    service, _, metadata, _, backend, record = prepared_service(
        tmp_path / "compatible-immutable-revision",
        "openai-compatible-api",
        compatible_resolved_revision=revision,
    )
    try:
        service.verify_model(verify_args(record, "openai-compatible-api", "immutable"))
        refreshed = service.list_models({"connection_id": record["connection_id"], "refresh": True})

        assert refreshed["models"][0]["availability"] == "available"
        assert refreshed["models"][0]["resolved_revision"] == revision
        assert len(metadata.calls) == 2
        assert len(backend.verify_calls) == 1
        assert service.store.read()["catalogs"][record["connection_id"]]["verified_models"]
    finally:
        service.close()


def test_claude_probe_rejects_mismatched_returned_runtime_as_unknown(tmp_path: Path):
    service, _, _, runtime, backend, record = prepared_service(
        tmp_path / "probe-runtime-mismatch", "claude-agent-sdk"
    )
    mismatched = copy.deepcopy(runtime.claude_evidence)
    mismatched["cli_sha256"] = "9" * 64
    backend.verify_runtime_evidence = mismatched
    args = verify_args(record, "claude-agent-sdk", "runtime-mismatch")
    try:
        with pytest.raises(EngineUnavailableError) as failure:
            service.verify_model(args)
        assert failure.value.details == {
            "reason": "provider_generation_outcome_unknown",
            "outcome_unknown": True,
        }
        persisted = service.store.read()
        assert persisted["requests"][args["request_id"]]["state"] == "unknown"
        assert (
            persisted["catalogs"][record["connection_id"]]["models"][0]["availability"]
            == "unverified"
        )
        assert backend.verify_calls[0][1]["expected_runtime"] == expected_claude_runtime(runtime)
    finally:
        service.close()


def test_claude_generation_rejects_mismatched_returned_runtime_as_unknown(tmp_path: Path):
    service, _, _, runtime, backend, record = prepared_service(
        tmp_path / "generation-runtime-mismatch", "claude-agent-sdk"
    )
    try:
        service.verify_model(verify_args(record, "claude-agent-sdk"))
        provider = MinutesResolver(service).resolve(selection(record, "claude-agent-sdk"))
        mismatched = copy.deepcopy(runtime.claude_evidence)
        mismatched["sdk_source_sha256"] = "8" * 64
        backend.complete_runtime_evidence = mismatched

        with pytest.raises(NarumiError) as failure:
            provider.complete("fixture meeting transcript")
        assert failure.value.details["reason"] == "provider_generation_outcome_unknown"
        assert failure.value.details["outcome_unknown"] is True
        assert backend.complete_calls[0][1]["expected_runtime"] == expected_claude_runtime(runtime)
        persisted = service.store.read()
        assert (
            persisted["connections"][record["connection_id"]]["last_generation_state"] == "unknown"
        )
    finally:
        service.close()


def test_runtime_change_invalidates_verification(tmp_path: Path):
    service, _, _, runtime, _, record = prepared_service(tmp_path / "runtime", "claude-agent-sdk")
    config = selection(record, "claude-agent-sdk")
    try:
        service.verify_model(verify_args(record, "claude-agent-sdk"))
        MinutesResolver(service).validate(config)
        runtime.claude_evidence["cli_sha256"] = "4" * 64
        with pytest.raises(EngineUnavailableError):
            MinutesResolver(service).validate(config)
        assert (
            service.list_models({"connection_id": record["connection_id"]})["catalog_state"]
            == "stale"
        )
    finally:
        service.close()


def test_compatible_discovery_identity_drift_invalidates_the_promoted_descriptor(
    tmp_path: Path,
):
    service, _, metadata, _, backend, record = prepared_service(
        tmp_path / "compatible-catalog-drift", "openai-compatible-api"
    )
    config = selection(record, "openai-compatible-api")
    try:
        service.verify_model(verify_args(record, "openai-compatible-api"))
        MinutesResolver(service).validate(config)
        assert len(backend.verify_calls) == 1

        metadata.compatible_display_name = "replacement-compatible-model"
        refreshed = service.list_models({"connection_id": record["connection_id"], "refresh": True})

        assert refreshed["models"][0]["availability"] == "unverified"
        assert refreshed["models"][0]["roles"] == []
        assert service.store.read()["catalogs"][record["connection_id"]]["verified_models"] == {}
        with pytest.raises(ModelUnavailableError):
            MinutesResolver(service).validate(config)
        assert len(backend.verify_calls) == 1
    finally:
        service.close()


def test_compatible_configuration_change_invalidates_verification(tmp_path: Path):
    service, _, _, _, _, record = prepared_service(
        tmp_path / "configuration", "openai-compatible-api"
    )
    try:
        service.verify_model(verify_args(record, "openai-compatible-api"))
        changed = service.set_connection(
            {
                "connection_id": record["connection_id"],
                "expected_revision": record["revision"],
                "api_surface": "chat_completions",
                "chat_max_tokens_field": "max_tokens",
                "request_id": "compatible-config-change-001",
            }
        )["connection"]
        assert changed["revision"] == record["revision"] + 1
        assert service.store.read()["catalogs"].get(record["connection_id"]) is None
        with pytest.raises(ConfigurationConflictError):
            MinutesResolver(service).validate(selection(record, "openai-compatible-api"))
        checked = service.test_connection(
            {
                "connection_id": changed["connection_id"],
                "expected_revision": changed["revision"],
            }
        )["connection"]
        with pytest.raises(ModelUnavailableError):
            MinutesResolver(service).validate(selection(checked, "openai-compatible-api"))
    finally:
        service.close()


@pytest.mark.parametrize("provider_id", list(MODEL_IDS))
@pytest.mark.parametrize("unknown", [False, True])
def test_failed_or_unknown_probe_is_not_resent_and_never_persists_secret(
    tmp_path: Path, provider_id: str, unknown: bool
):
    root = tmp_path / provider_id / ("unknown" if unknown else "failed")
    service, _, _, _, backend, record = prepared_service(root, provider_id)
    backend.verify_error = EngineUnavailableError(
        "upstream fixture must be redacted",
        details=(
            {
                "reason": "provider_generation_outcome_unknown",
                "outcome_unknown": True,
            }
            if unknown
            else {"reason": "fixture_known_failure", "outcome_unknown": False}
        ),
    )
    args = verify_args(record, provider_id, "failed-probe-001")
    try:
        with pytest.raises(EngineUnavailableError) as first:
            service.verify_model(args)
        assert bool(first.value.details.get("outcome_unknown")) is unknown
        assert len(backend.verify_calls) == 1

        with pytest.raises(NarumiError):
            service.verify_model(args)
        assert len(backend.verify_calls) == 1
        with pytest.raises(ModelUnavailableError):
            MinutesResolver(service).validate(selection(record, provider_id))

        for path in root.rglob("*"):
            if path.is_file():
                assert SECRET.encode() not in path.read_bytes(), path
    finally:
        service.close()


def _block_next_probe(backend: ProbeBackend):
    import threading

    started = threading.Event()
    release = threading.Event()
    original = backend.verify_model

    def blocked(*args, **kwargs):
        started.set()
        if not release.wait(10):
            raise AssertionError("fixture model probe was not released")
        return original(*args, **kwargs)

    backend.verify_model = blocked
    return started, release


def _start_probe(service: ProviderService, args: dict):
    import threading

    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["result"] = service.verify_model(args)
        except Exception as error:  # noqa: BLE001 - the thread must report its public error
            outcome["error"] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


def _finish_probe_thread(thread, outcome: dict[str, object]) -> Exception:
    thread.join(10)
    assert not thread.is_alive()
    assert "result" not in outcome
    error = outcome.get("error")
    assert isinstance(error, Exception)
    return error


def test_restarted_service_blocks_new_request_for_unknown_probe(tmp_path: Path):
    root = tmp_path / "restart-ownership"
    service, secrets, metadata, runtime, backend, record = prepared_service(
        root, "claude-agent-sdk"
    )
    args = verify_args(record, "claude-agent-sdk", "old-owner")
    old_started, old_release = _block_next_probe(backend)
    old_thread, old_outcome = _start_probe(service, args)
    replacement = None
    try:
        assert old_started.wait(10)
        accepted = service.store.read()
        assert accepted["requests"][args["request_id"]]["state"] == "pending"
        assert (
            accepted["checks"]["claude-agent-sdk"]["server_instance_id"]
            == service.server_instance_id
        )

        replacement_backend = ProbeBackend("claude-agent-sdk")
        replacement = ProviderService(
            root,
            secret_store=secrets,
            metadata_client=metadata,
            runtime_inspector=runtime,
            codex_backend=FakeCodexBackend(),
            claude_backend=replacement_backend,
            server_instance_id="replacement-provider-service",
        )
        recovered = replacement.store.read()
        assert recovered["requests"][args["request_id"]]["state"] == "unknown"
        assert "claude-agent-sdk" not in recovered["checks"]

        replacement.test_connection(
            {
                "connection_id": record["connection_id"],
                "expected_revision": record["revision"],
            }
        )
        replacement_args = verify_args(record, "claude-agent-sdk", "new-owner")

        with pytest.raises(EngineUnavailableError) as duplicate:
            replacement.verify_model(replacement_args)
        assert duplicate.value.details == {
            "reason": "provider_generation_outcome_unknown",
            "outcome_unknown": True,
        }
        after_rejection = replacement.store.read()
        assert replacement_args["request_id"] not in after_rejection["requests"]
        assert "claude-agent-sdk" not in after_rejection["checks"]
        assert replacement_backend.verify_calls == []

        with pytest.raises(NarumiError):
            replacement.verify_model(args)
        assert replacement_backend.verify_calls == []

        old_release.set()
        assert isinstance(_finish_probe_thread(old_thread, old_outcome), ConfigurationConflictError)
        persisted = replacement.store.read()
        assert "claude-agent-sdk" not in persisted["checks"]
        assert persisted["requests"][args["request_id"]]["state"] == "unknown"
        assert replacement_args["request_id"] not in persisted["requests"]
        assert (
            persisted["catalogs"][record["connection_id"]]["models"][0]["availability"]
            == "unverified"
        )
        assert replacement_backend.verify_calls == []
    finally:
        old_release.set()
        old_thread.join(10)
        if replacement is not None:
            replacement.close()
        service.close()


@pytest.mark.parametrize("changed", ["connection", "runtime", "catalog"])
def test_probe_change_conflict_durably_releases_its_check_and_fails_receipt(
    tmp_path: Path,
    changed: str,
):
    service, _, _, runtime, backend, record = prepared_service(
        tmp_path / changed, "claude-agent-sdk"
    )
    args = verify_args(record, "claude-agent-sdk", f"changed-{changed}")
    started, release = _block_next_probe(backend)
    thread, outcome = _start_probe(service, args)
    try:
        assert started.wait(10)
        if changed == "runtime":
            runtime.claude_evidence["cli_sha256"] = "4" * 64
        else:
            with service.store.transaction() as document:
                if changed == "connection":
                    document["connections"][record["connection_id"]]["revision"] += 1
                else:
                    document["catalogs"][record["connection_id"]]["catalog_id"] = (
                        "replacement-catalog"
                    )
        release.set()
        assert isinstance(_finish_probe_thread(thread, outcome), ConfigurationConflictError)

        persisted = service.store.read()
        assert "claude-agent-sdk" not in persisted["checks"]
        assert persisted["requests"][args["request_id"]]["state"] == "failed"
        assert persisted["catalogs"][record["connection_id"]]["models"][0]["availability"] == (
            "unverified"
        )

        current = persisted["connections"][record["connection_id"]]
        result = service.test_connection(
            {
                "connection_id": current["connection_id"],
                "expected_revision": current["revision"],
            }
        )
        assert "connected" in result
    finally:
        release.set()
        thread.join(10)
        service.close()


def test_closed_service_marks_completed_probe_unknown_and_releases_check(tmp_path: Path):
    service, _, _, _, backend, record = prepared_service(tmp_path / "closed", "claude-agent-sdk")
    args = verify_args(record, "claude-agent-sdk", "closed-service")
    started, release = _block_next_probe(backend)
    thread, outcome = _start_probe(service, args)
    try:
        assert started.wait(10)
        service.closed.set()
        release.set()
        assert isinstance(_finish_probe_thread(thread, outcome), ConfigurationConflictError)
        persisted = service.store.read()
        assert "claude-agent-sdk" not in persisted["checks"]
        assert persisted["requests"][args["request_id"]]["state"] == "unknown"
        assert persisted["catalogs"][record["connection_id"]]["models"][0]["availability"] == (
            "unverified"
        )
        assert len(backend.verify_calls) == 1
    finally:
        release.set()
        thread.join(10)
        service.close()


@pytest.mark.parametrize("provider_id", list(MODEL_IDS))
def test_unknown_probe_outcome_is_durably_unknown_and_never_resent(
    tmp_path: Path, provider_id: str
):
    service, _, _, _, backend, record = prepared_service(
        tmp_path / "unknown-receipt" / provider_id, provider_id
    )
    backend.verify_error = EngineUnavailableError(
        "untrusted upstream failure",
        details={
            "reason": "provider_generation_outcome_unknown",
            "outcome_unknown": True,
        },
    )
    args = verify_args(record, provider_id, "unknown-receipt")
    try:
        with pytest.raises(EngineUnavailableError) as failure:
            service.verify_model(args)
        assert failure.value.details["outcome_unknown"] is True
        persisted = service.store.read()
        assert provider_id not in persisted["checks"]
        assert persisted["requests"][args["request_id"]]["state"] == "unknown"

        with pytest.raises(NarumiError):
            service.verify_model(args)
        replacement_args = verify_args(record, provider_id, "unknown-replacement")
        with pytest.raises(EngineUnavailableError) as duplicate:
            service.verify_model(replacement_args)
        assert duplicate.value.details == {
            "reason": "provider_generation_outcome_unknown",
            "outcome_unknown": True,
        }
        assert replacement_args["request_id"] not in service.store.read()["requests"]
        assert len(backend.verify_calls) == 1
    finally:
        service.close()


@pytest.mark.parametrize("provider_id", list(MODEL_IDS))
@pytest.mark.parametrize("tamper", ["delete", "replace"])
def test_lost_or_replaced_request_hmac_key_blocks_unknown_probe_resend(
    tmp_path: Path, provider_id: str, tamper: str
):
    service, secrets, _, _, backend, record = prepared_service(
        tmp_path / "unknown-after-hmac-loss" / provider_id,
        provider_id,
    )
    backend.verify_error = EngineUnavailableError(
        "untrusted upstream failure",
        details={
            "reason": "provider_generation_outcome_unknown",
            "outcome_unknown": True,
        },
    )
    original = verify_args(record, provider_id, "unknown-before-hmac-loss")
    replacement = verify_args(record, provider_id, "unknown-after-hmac-loss")
    try:
        with pytest.raises(EngineUnavailableError):
            service.verify_model(original)
        generation = service.store.read()["request_hmac_generation"]
        assert generation["scheme"] == "sha256"
        account = f"providers:{service.namespace}:request-hmac"
        if tamper == "delete":
            secrets.delete(account)
        else:
            secrets.set(account, "substituted-request-hmac-key-4821")

        with pytest.raises(BusyError) as blocked:
            service.verify_model(replacement)
        assert blocked.value.details == {"reason": "credential_unavailable"}
        assert replacement["request_id"] not in service.store.read()["requests"]
        assert len(backend.verify_calls) == 1
    finally:
        service.close()


@pytest.mark.parametrize("provider_id", list(MODEL_IDS))
@pytest.mark.parametrize("scheme_marker", ["hmac", "missing"])
def test_legacy_unhashed_marker_cannot_adopt_a_replaced_key_after_unknown_probe(
    tmp_path: Path, provider_id: str, scheme_marker: str
):
    root = tmp_path / "legacy-marker-after-unknown" / provider_id
    service, secrets, metadata, runtime, backend, record = prepared_service(root, provider_id)
    backend.verify_error = EngineUnavailableError(
        "untrusted upstream failure",
        details={
            "reason": "provider_generation_outcome_unknown",
            "outcome_unknown": True,
        },
    )
    replacement_backend = ProbeBackend(provider_id)
    try:
        with pytest.raises(EngineUnavailableError):
            service.verify_model(verify_args(record, provider_id, "legacy-marker-unknown"))
        assert len(backend.verify_calls) == 1
        service.close()
        with service.store.transaction() as document:
            document["request_hmac_generation"] = 1
            if scheme_marker == "missing":
                request_id = verify_args(record, provider_id, "legacy-marker-unknown")["request_id"]
                document["requests"][request_id].pop("credential_fingerprint_scheme")
        secrets.set(
            f"providers:{service.namespace}:request-hmac",
            "substituted-before-marker-upgrade-4182",
        )
        before = service.store.path.read_bytes()

        with pytest.raises(BusyError) as blocked:
            ProviderService(
                root,
                secret_store=secrets,
                metadata_client=metadata,
                runtime_inspector=runtime,
                codex_backend=FakeCodexBackend(),
                claude_backend=(replacement_backend if provider_id == "claude-agent-sdk" else None),
                openai_compatible_backend=(
                    replacement_backend if provider_id == "openai-compatible-api" else None
                ),
            )
        assert blocked.value.details == {"reason": "credential_unavailable"}
        assert service.store.path.read_bytes() == before
        assert replacement_backend.verify_calls == []
    finally:
        service.close()


@pytest.mark.parametrize("provider_id", list(MODEL_IDS))
def test_unknown_probe_cannot_be_resent_after_connection_display_name_only_changes(
    tmp_path: Path, provider_id: str
):
    service, _, _, _, backend, record = prepared_service(
        tmp_path / "unknown-after-rename" / provider_id,
        provider_id,
    )
    backend.verify_error = EngineUnavailableError(
        "untrusted upstream failure",
        details={
            "reason": "provider_generation_outcome_unknown",
            "outcome_unknown": True,
        },
    )
    original_args = verify_args(record, provider_id, "unknown-before-rename")
    try:
        with pytest.raises(EngineUnavailableError):
            service.verify_model(original_args)
        original_receipt = service.store.read()["requests"][original_args["request_id"]]
        assert original_receipt["state"] == "unknown"

        renamed = service.set_connection(
            {
                "connection_id": record["connection_id"],
                "expected_revision": record["revision"],
                "display_name": "Renamed without execution changes",
                "request_id": f"rename-after-unknown-{provider_id}",
            }
        )["connection"]
        assert renamed["catalog_state"] == "stale"
        refreshed = service.test_connection(
            {
                "connection_id": renamed["connection_id"],
                "expected_revision": renamed["revision"],
            }
        )["connection"]
        replacement_args = verify_args(refreshed, provider_id, "unknown-after-rename")
        with pytest.raises(EngineUnavailableError) as duplicate:
            service.verify_model(replacement_args)
        assert duplicate.value.details == {
            "reason": "provider_generation_outcome_unknown",
            "outcome_unknown": True,
        }
        assert replacement_args["request_id"] not in service.store.read()["requests"]
        assert len(backend.verify_calls) == 1
    finally:
        service.close()


@pytest.mark.parametrize("provider_id", list(MODEL_IDS))
def test_replacing_the_same_credential_cannot_bypass_an_unknown_probe(
    tmp_path: Path, provider_id: str
):
    service, _, _, _, backend, record = prepared_service(
        tmp_path / "unknown-after-credential-change" / provider_id,
        provider_id,
    )
    backend.verify_error = EngineUnavailableError(
        "untrusted upstream failure",
        details={
            "reason": "provider_generation_outcome_unknown",
            "outcome_unknown": True,
        },
    )
    try:
        with pytest.raises(EngineUnavailableError):
            service.verify_model(verify_args(record, provider_id, "before-new-credential"))
        assert len(backend.verify_calls) == 1

        changed = service.set_connection(
            {
                "connection_id": record["connection_id"],
                "expected_revision": record["revision"],
                "api_key": SECRET,
                "request_id": f"replace-probe-credential-{provider_id}",
            }
        )["connection"]
        checked = service.test_connection(
            {
                "connection_id": changed["connection_id"],
                "expected_revision": changed["revision"],
            }
        )["connection"]
        backend.verify_error = None
        with pytest.raises(EngineUnavailableError) as duplicate:
            service.verify_model(verify_args(checked, provider_id, "after-same-credential"))
        assert duplicate.value.details["outcome_unknown"] is True
        assert len(backend.verify_calls) == 1
    finally:
        service.close()


@pytest.mark.parametrize("provider_id", list(MODEL_IDS))
def test_a_different_credential_allows_an_explicit_new_probe_after_unknown(
    tmp_path: Path, provider_id: str
):
    service, _, metadata, _, backend, record = prepared_service(
        tmp_path / "unknown-after-different-credential" / provider_id,
        provider_id,
    )
    backend.verify_error = EngineUnavailableError(
        "untrusted upstream failure",
        details={
            "reason": "provider_generation_outcome_unknown",
            "outcome_unknown": True,
        },
    )
    replacement_key = "different-fixture-model-probe-key-9914"
    try:
        with pytest.raises(EngineUnavailableError):
            service.verify_model(verify_args(record, provider_id, "before-different-credential"))

        changed = service.set_connection(
            {
                "connection_id": record["connection_id"],
                "expected_revision": record["revision"],
                "api_key": replacement_key,
                "request_id": f"replace-with-different-credential-{provider_id}",
            }
        )["connection"]
        metadata.expected_api_key = replacement_key
        checked = service.test_connection(
            {
                "connection_id": changed["connection_id"],
                "expected_revision": changed["revision"],
            }
        )["connection"]
        backend.verify_error = None
        verified = service.verify_model(
            verify_args(checked, provider_id, "after-different-credential")
        )
        assert verified["model"]["availability"] == "available"
        assert len(backend.verify_calls) == 2
    finally:
        service.close()


@pytest.mark.parametrize("provider_id", list(MODEL_IDS))
def test_duplicate_connection_with_the_same_credential_cannot_bypass_unknown_probe(
    tmp_path: Path, provider_id: str
):
    service, _, _, _, backend, first = prepared_service(
        tmp_path / "unknown-via-duplicate-connection" / provider_id,
        provider_id,
    )
    backend.verify_error = EngineUnavailableError(
        "untrusted upstream failure",
        details={
            "reason": "provider_generation_outcome_unknown",
            "outcome_unknown": True,
        },
    )
    try:
        with pytest.raises(EngineUnavailableError):
            service.verify_model(verify_args(first, provider_id, "first-connection-unknown"))

        connection_args = {
            "provider_id": provider_id,
            "display_name": "Duplicate execution identity",
            "auth_method": "api_key",
            "api_key": SECRET,
            "request_id": f"create-duplicate-probe-connection-{provider_id}",
        }
        if provider_id == "openai-compatible-api":
            connection_args.update(
                endpoint="https://127.0.0.1:9443/v1",
                api_surface="responses",
            )
        second = service.set_connection(connection_args)["connection"]
        checked = service.test_connection(
            {
                "connection_id": second["connection_id"],
                "expected_revision": second["revision"],
            }
        )["connection"]
        with pytest.raises(EngineUnavailableError) as duplicate:
            service.verify_model(verify_args(checked, provider_id, "duplicate-connection"))
        assert duplicate.value.details["outcome_unknown"] is True
        assert len(backend.verify_calls) == 1
    finally:
        service.close()


@pytest.mark.parametrize("provider_id", list(MODEL_IDS))
def test_legacy_unknown_probe_without_semantic_identity_blocks_new_paid_probe(
    tmp_path: Path, provider_id: str
):
    service, _, _, _, backend, record = prepared_service(
        tmp_path / "legacy-unknown-probe" / provider_id,
        provider_id,
    )
    backend.verify_error = EngineUnavailableError(
        "untrusted upstream failure",
        details={
            "reason": "provider_generation_outcome_unknown",
            "outcome_unknown": True,
        },
    )
    args = verify_args(record, provider_id, "legacy-unknown")
    try:
        with pytest.raises(EngineUnavailableError):
            service.verify_model(args)
        with service.store.transaction() as document:
            document["requests"][args["request_id"]].pop("semantic_fingerprint")
        replacement = verify_args(record, provider_id, "after-legacy-unknown")
        with pytest.raises(EngineUnavailableError) as blocked:
            service.verify_model(replacement)
        assert blocked.value.details["outcome_unknown"] is True
        assert replacement["request_id"] not in service.store.read()["requests"]
        assert len(backend.verify_calls) == 1
    finally:
        service.close()
