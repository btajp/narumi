"""Contract-to-service integration with fake secrets and metadata, never real credentials."""

from __future__ import annotations

import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from conftest import make_recorded_bundle, write_fake_minutes
from narumi.bundle import Bundle
from narumi.contracts import load_contracts
from narumi.errors import CancelledError, EngineUnavailableError
from narumi.generate import run_generate
from narumi.models import ExternalSendPolicy, MeetingConfig
from narumi.pipeline import ProcessResult
from narumi.providers.runtime import RuntimeInspector
from narumi_server.app import dispatch
from narumi_server.context import build_context
from narumi_server.handlers import processing
from narumi_server.provider_tools import PROVIDER_TOOLS
from test_surface_tools import write_silence_wav

from pipeline.tests.provider_fakes import FakeCodexBackend, prepared_codex_connection

SECRET = "fake-provider-integration-secret-739125"


class MemorySecrets:
    def __init__(self):
        self.values: dict[str, str] = {}

    def get(self, account: str) -> str | None:
        return self.values.get(account)

    def set(self, account: str, value: str) -> None:
        self.values[account] = value

    def delete(self, account: str) -> None:
        self.values.pop(account, None)


class Metadata:
    def __init__(self):
        self.calls = 0

    def fetch(self, provider_id: str, endpoint: str, api_key: str | None) -> list[dict]:
        assert provider_id == "anthropic-api"
        assert endpoint == "https://api.anthropic.com"
        assert api_key == SECRET
        self.calls += 1
        return copy.deepcopy(load_contracts()["list_provider_models"].output_examples[0]["models"])


def new_connection() -> dict[str, Any]:
    return {
        "provider_id": "anthropic-api",
        "display_name": "議事録用 API",
        "auth_method": "api_key",
        "api_key": SECRET,
        "request_id": str(uuid4()),
    }


def result(ctx, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    outcome = dispatch(ctx, tool, args)
    assert not outcome.is_error, outcome.payload
    ctx.contracts.validate_output(tool, outcome.payload)
    assert SECRET not in json.dumps(outcome.payload)
    return outcome.payload


@pytest.mark.parametrize("transport", ["streamable-http", "stdio", "in-process"])
def test_server_reports_minutes_selection_on_resident_transport(home, transport):
    ctx = build_context(
        home,
        transports=[transport],
        validate_output=True,
        provider_secret_store=MemorySecrets(),
        provider_codex_backend=FakeCodexBackend(),
    )
    try:
        info = result(ctx, "get_server_info", {})
        assert info["capabilities"]["workflow"]["stage_model_selection"] is (
            transport == "streamable-http"
        )
    finally:
        ctx.close()


def test_server_reports_resident_capabilities_for_mixed_transports(home):
    ctx = build_context(
        home,
        transports=["streamable-http", "stdio"],
        validate_output=True,
        provider_secret_store=MemorySecrets(),
        provider_codex_backend=FakeCodexBackend(),
    )
    try:
        info = result(ctx, "get_server_info", {})
        assert info["capabilities"]["workflow"] == {
            "provider_connections": True,
            "provider_models": True,
            "stage_model_selection": True,
            "ensemble_generation": False,
        }
        assert info["capabilities"]["minutes_model_providers"] == [
            "codex-app-server",
            "anthropic-api",
            "ollama",
            "openai-api",
        ]
        assert info["capabilities"]["transcription_model_providers"] == ["openai-api"]
        assert info["secure_transport"] == {
            "mode": "pinned_tls",
            "tls_required": True,
            "client_auth_required": True,
        }
    finally:
        ctx.close()


def test_connection_round_trip_metadata_cas_and_key_removal(tmp_path: Path, caplog):
    secrets, metadata = MemorySecrets(), Metadata()
    ctx = build_context(
        tmp_path,
        transports=["streamable-http"],
        validate_output=True,
        provider_secret_store=secrets,
        provider_metadata_client=metadata,
    )
    try:
        created = result(ctx, "set_provider_connection", new_connection())["connection"]
        assert created["credential_present"] is True
        assert created["auth_state"] == "unverified"
        listed = result(ctx, "list_provider_connections", {})["connections"]
        assert listed == [created]
        cid = created["connection_id"]
        checked = result(
            ctx, "test_provider_connection", {"connection_id": cid, "expected_revision": 1}
        )
        assert checked["connected"] is True
        assert checked["connection"]["revision"] == 1
        assert checked["connection"]["last_generation_state"] == "never"
        models = result(ctx, "list_provider_models", {"connection_id": cid})
        assert models["models"][0]["model_id"] == "fixture-text-model"
        assert metadata.calls == 1
        changed = result(
            ctx,
            "set_provider_connection",
            {
                "connection_id": cid,
                "expected_revision": 1,
                "enabled": False,
                "request_id": str(uuid4()),
            },
        )["connection"]
        assert changed["revision"] == 2 and changed["credential_present"]
        conflict = dispatch(
            ctx,
            "set_provider_connection",
            {
                "connection_id": cid,
                "expected_revision": 1,
                "enabled": True,
                "request_id": str(uuid4()),
            },
        )
        assert conflict.is_error
        assert conflict.payload["error"]["code"] == "configuration_conflict"
        result(
            ctx,
            "delete_provider_connection",
            {
                "connection_id": cid,
                "expected_revision": 2,
                "confirm": True,
                "request_id": str(uuid4()),
            },
        )
        assert result(ctx, "list_provider_connections", {})["connections"] == []
        assert SECRET not in secrets.values.values()
        assert SECRET not in caplog.text
        for file in tmp_path.rglob("*"):
            if file.is_file():
                assert SECRET.encode() not in file.read_bytes(), file.name
    finally:
        ctx.close()


def test_secret_replay_survives_restart_and_does_not_bypass_argument_checks(tmp_path: Path):
    secrets = MemorySecrets()
    args = new_connection()
    ctx = build_context(tmp_path, transports=["streamable-http"], provider_secret_store=secrets)
    original = result(ctx, "set_provider_connection", args)
    ctx.close()
    restarted = build_context(
        tmp_path, transports=["streamable-http"], provider_secret_store=secrets
    )
    try:
        assert result(restarted, "set_provider_connection", args) == original
        changed = dispatch(
            restarted, "set_provider_connection", {**args, "api_key": "different-fake-secret"}
        )
        assert changed.is_error
        assert SECRET not in json.dumps(changed.payload)
        assert "different-fake-secret" not in json.dumps(changed.payload)
        assert len(result(restarted, "list_provider_connections", {})["connections"]) == 1
    finally:
        restarted.close()


@pytest.mark.parametrize("queued", [True, False])
def test_runtime_cancel_keeps_exclusion_until_worker_stops(tmp_path: Path, queued: bool):
    started, release = threading.Event(), threading.Event()

    class Inspector(RuntimeInspector):
        def resource(self, provider_id):
            return {
                "resource_id": "anthropic-client",
                "display_name": "Fixture installed client",
                "kind": "runtime",
                "version": "1.0.0",
                "source": "installed",
                "download_host": None,
                "sha256": "a" * 64,
                "license": "Fixture",
            }

        def prepare(self, root, provider_id, resource, progress):
            started.set()
            assert release.wait(5)
            progress("fixture_inspection", 0.5)

    instance_id = str(uuid4())
    ctx = build_context(
        tmp_path,
        transports=["streamable-http"],
        provider_secret_store=MemorySecrets(),
        server_instance_id=instance_id,
        provider_codex_backend=FakeCodexBackend(),
    )
    ctx.providers.runtime.inspector = Inspector()

    def runtime():
        providers = result(ctx, "list_providers", {})["providers"]
        return next(p["runtime"] for p in providers if p["provider_id"] == "anthropic-api")

    def prepare_args():
        current = runtime()
        return {
            "provider_id": "anthropic-api",
            "resource_id": current["resources"][0]["resource_id"],
            "expected_catalog_revision": current["catalog_revision"],
            "action": "prepare",
            "request_id": str(uuid4()),
        }

    def occupy_worker(_progress):
        started.set()
        assert release.wait(5)
        return {}

    try:
        assert ctx.server_instance_id == ctx.providers.server_instance_id == instance_id
        if queued:
            ctx.jobs.submit("process", None, occupy_worker)
            assert started.wait(5)
        job_id = result(ctx, "prepare_provider_runtime", prepare_args())["job_id"]
        assert started.wait(5)
        cancelled = result(ctx, "cancel_job", {"job_id": job_id, "request_id": str(uuid4())})["job"]
        assert cancelled["status"] == ("cancelled" if queued else "running")
        if not queued:
            assert runtime()["active_setup"]["job_id"] == job_id
            refused = dispatch(ctx, "prepare_provider_runtime", prepare_args())
            assert refused.is_error and refused.payload["error"]["code"] == "busy"
        release.set()
        assert ctx.jobs.wait(job_id, timeout=5)["status"] == "cancelled"
        assert result(ctx, "get_job_status", {"job_id": job_id})["job"]["status"] == "cancelled"
        assert runtime()["active_setup"] is None
        next_job_id = result(ctx, "prepare_provider_runtime", prepare_args())["job_id"]
        assert ctx.jobs.wait(next_job_id, timeout=5)["status"] == "succeeded"
        assert runtime()["last_setup"]["job_id"] == next_job_id
    finally:
        release.set()
        ctx.close()


class ExplodingService:
    def __getattr__(self, name: str):
        def fail(*args, **kwargs):
            raise EngineUnavailableError(SECRET, details={"upstream": SECRET})

        return fail

    def close(self):
        pass


@pytest.mark.parametrize("tool", sorted(PROVIDER_TOOLS))
def test_all_provider_failures_are_redacted_before_response_audit_or_replay(
    tmp_path: Path, caplog, tool: str
):
    ctx = build_context(
        tmp_path, transports=["streamable-http"], provider_service=ExplodingService()
    )
    try:
        outcome = dispatch(ctx, tool, ctx.contracts[tool].input_examples[0])
        assert outcome.is_error
        assert SECRET not in json.dumps(outcome.payload)
        assert SECRET not in caplog.text
        for file in tmp_path.rglob("*"):
            if file.is_file():
                assert SECRET.encode() not in file.read_bytes(), file.name
    finally:
        ctx.close()


@pytest.mark.parametrize("tool", [*sorted(PROVIDER_TOOLS), "set_gaia_connection"])
def test_private_operations_cannot_bypass_resident_authentication_via_stdio(tmp_path: Path, tool):
    ctx = build_context(tmp_path, transports=["stdio"], provider_service=ExplodingService())
    try:
        outcome = dispatch(ctx, tool, ctx.contracts[tool].input_examples[0])
        assert outcome.is_error
        assert outcome.payload["error"]["code"] == "authentication_required"
    finally:
        ctx.close()


@pytest.fixture
def codex_context(home):
    backend = FakeCodexBackend()
    backend.response = (
        "## アジェンダ\n確認\n## 議論サマリ\n合成テスト\n"
        "## 決定事項\n継続する\n## TODO・宿題\nなし\n"
    )
    ctx = build_context(
        home,
        transports=["streamable-http"],
        validate_output=True,
        provider_secret_store=MemorySecrets(),
        provider_codex_backend=backend,
    )
    connection = prepared_codex_connection(ctx.providers)
    config = MeetingConfig.model_validate(
        {
            "transcription_engine": "fake",
            "external_send_policy": "subscription_ok",
            "minutes_model": {
                "provider": "codex-app-server",
                "connection_id": connection["connection_id"],
                "connection_revision": connection["revision"],
                "model_id": backend.models[0]["model_id"],
                "parameters": {"reasoning_effort": "high"},
            },
        }
    )
    try:
        yield ctx, backend, config
    finally:
        ctx.close()


def selected_bundle(ctx, config):
    bundle = make_recorded_bundle(ctx, meeting_id="20260829T000000Z-0000c0de")
    bundle.manifest.config = config.model_copy(deep=True)
    bundle.save()
    return bundle


def selected_generation(bundle, *, minutes_resolver, should_cancel, **kwargs):
    """Only upstream media work is fake; generation and provenance use the real stage."""
    if bundle.artifact("merged/merged") is None:
        write_fake_minutes(bundle)
    stage = run_generate(
        bundle,
        force=kwargs.get("force", False),
        minutes_resolver=minutes_resolver,
        should_cancel=should_cancel,
    )
    return ProcessResult(
        meeting_id=bundle.meeting_id,
        minutes_version=bundle.manifest.latest_minutes_version,
        stages=[] if stage.skipped else [stage.key],
        skipped=[stage.key] if stage.skipped else [],
    )


def delete_codex(ctx, config):
    return dispatch(
        ctx,
        "delete_provider_connection",
        {
            "connection_id": config.minutes_model.connection_id,
            "expected_revision": config.minutes_model.connection_revision,
            "confirm": True,
            "request_id": str(uuid4()),
        },
    )


def test_saved_codex_profile_survives_restart_and_generates_selected_model(
    codex_context, monkeypatch
):
    ctx, backend, config = codex_context
    saved = result(
        ctx,
        "set_profile",
        {
            "name": "codex-minutes",
            "config": config.model_dump(mode="json"),
            "request_id": str(uuid4()),
        },
    )["profile"]
    started = result(
        ctx, "start_recording", {"profile": "codex-minutes", "request_id": str(uuid4())}
    )
    result(ctx, "stop_recording", {"auto_process": False, "request_id": str(uuid4())})
    ctx.close()
    resumed_backend = FakeCodexBackend()
    resumed_backend.response = backend.response
    resumed = build_context(
        ctx.data_root,
        transports=["streamable-http"],
        validate_output=True,
        provider_secret_store=MemorySecrets(),
        provider_codex_backend=resumed_backend,
    )
    monkeypatch.setattr(processing.narumi_pipeline, "refresh_meeting", selected_generation)
    try:
        assert result(resumed, "get_profile", {"name": "codex-minutes"})["profile"] == saved
        meeting = result(resumed, "get_meeting", {"meeting_id": started["meeting_id"]})
        assert meeting["config"] == config.model_dump(mode="json")
        receipt = result(
            resumed,
            "regenerate",
            {
                "meeting_id": started["meeting_id"],
                "expected_config": meeting["config"],
                "request_id": str(uuid4()),
            },
        )
        job = resumed.jobs.wait(receipt["job_id"], timeout=5)
        assert job["status"] == "succeeded", job
        completions = [call for call in resumed_backend.calls if call[0] == "complete"]
        assert len(completions) == 2
        assert all(
            call[2:4] == (config.minutes_model.model_id, {"reasoning_effort": "high"})
            for call in completions
        )
        bundle = Bundle.find(resumed.meetings_root, started["meeting_id"])
        assert bundle.manifest.minutes_versions[-1].provider == "codex-app-server"
        assert bundle.artifact("minutes/v2").params[
            "minutes_model"
        ] == config.minutes_model.model_dump(mode="json")
    finally:
        resumed.close()


@pytest.mark.parametrize("transport", ["stdio", "in-process"])
@pytest.mark.parametrize(
    "tool", ["set_profile", "set_meeting_config", "regenerate", "register_context"]
)
def test_codex_selection_requires_resident_transport(codex_context, transport, tool):
    ctx, backend, config = codex_context
    bundle = selected_bundle(ctx, config)
    payload = config.model_dump(mode="json")
    args = {
        "set_profile": {"name": "rejected", "config": payload},
        "set_meeting_config": {
            "meeting_id": bundle.meeting_id,
            "minutes_model": payload["minutes_model"],
        },
        "regenerate": {"meeting_id": bundle.meeting_id, "expected_config": payload},
        "register_context": {
            "meeting_id": bundle.meeting_id,
            "source_type": "document",
            "content": "synthetic",
            "auto_regenerate": True,
            "expected_config": payload,
        },
    }[tool]
    ctx.transports = [transport]
    rejected = dispatch(ctx, tool, {**args, "request_id": str(uuid4())})
    assert rejected.is_error and rejected.payload["error"]["code"] == "authentication_required"
    assert backend.calls == []
    assert not ctx.jobs.has_active(bundle.meeting_id)
    assert Bundle.find(ctx.meetings_root, bundle.meeting_id).manifest.contexts == []


@pytest.mark.parametrize(
    "tool",
    [
        "set_profile",
        "set_meeting_config",
        "start_recording",
        "import_recording",
        "regenerate",
        "register_context",
    ],
)
def test_codex_local_policy_is_rejected_before_side_effects(codex_context, tool, tmp_path):
    ctx, backend, config = codex_context
    forbidden = config.model_copy(update={"external_send_policy": ExternalSendPolicy.LOCAL_ONLY})
    payload = forbidden.model_dump(mode="json")
    bundle = selected_bundle(
        ctx, forbidden if tool in {"regenerate", "register_context"} else config
    )
    source = tmp_path / "synthetic.wav"
    source.write_bytes(b"fixture media")
    args = {
        "set_profile": {"name": "rejected", "config": payload},
        "set_meeting_config": {
            "meeting_id": bundle.meeting_id,
            "external_send_policy": "local_only",
        },
        "start_recording": {"config": payload},
        "import_recording": {
            "meeting_name": "synthetic",
            "mic_path": str(source),
            "config": payload,
        },
        "regenerate": {"meeting_id": bundle.meeting_id, "expected_config": payload},
        "register_context": {
            "meeting_id": bundle.meeting_id,
            "source_type": "document",
            "content": "synthetic",
            "auto_regenerate": True,
            "expected_config": payload,
        },
    }[tool]
    before = sorted(ctx.meetings_root.iterdir())
    rejected = dispatch(ctx, tool, {**args, "request_id": str(uuid4())})
    assert rejected.is_error and rejected.payload["error"]["code"] == "policy_violation", (
        rejected.payload
    )
    assert backend.calls == []
    assert sorted(ctx.meetings_root.iterdir()) == before
    assert not ctx.recorder.is_active
    assert not ctx.jobs.has_active(bundle.meeting_id)
    assert ctx.profiles.peek("rejected") is None
    assert Bundle.find(ctx.meetings_root, bundle.meeting_id).manifest.contexts == []


@pytest.mark.parametrize("tool", ["regenerate", "register_context"])
@pytest.mark.parametrize("change", ["missing", "language", "model", "policy", "actual_model_clear"])
def test_codex_generation_requires_the_current_full_config(codex_context, tool, change):
    ctx, backend, config = codex_context
    bundle = selected_bundle(ctx, config)
    expected = config.model_dump(mode="json")
    if change == "language":
        expected["language"] = "en"
    elif change == "model":
        expected["minutes_model"] = None
    elif change == "policy":
        expected["external_send_policy"] = "api_ok"
    elif change == "actual_model_clear":
        bundle.manifest.config.minutes_model = None
        bundle.save()
    args = {"meeting_id": bundle.meeting_id, "request_id": str(uuid4())}
    if change != "missing":
        args["expected_config"] = expected
    if tool == "register_context":
        args.update(source_type="document", content="synthetic", auto_regenerate=True)
    rejected = dispatch(ctx, tool, args)
    assert rejected.is_error and rejected.payload["error"]["code"] == "configuration_conflict"
    assert backend.calls == []
    assert not ctx.jobs.has_active(bundle.meeting_id)
    assert Bundle.find(ctx.meetings_root, bundle.meeting_id).manifest.contexts == []


@pytest.mark.parametrize("reference", ["profile", "unindexed_meeting"])
def test_codex_connection_cannot_be_deleted_while_files_reference_it(codex_context, reference):
    ctx, _, config = codex_context
    if reference == "profile":
        result(
            ctx,
            "set_profile",
            {"name": "saved", "config": config.model_dump(mode="json"), "request_id": str(uuid4())},
        )
    else:
        bundle = selected_bundle(ctx, config)
        ctx.catalog.delete_meeting(bundle.meeting_id)
    rejected = delete_codex(ctx, config)
    assert rejected.is_error and rejected.payload["error"]["code"] == "busy"
    if reference == "profile":
        result(
            ctx,
            "set_profile",
            {"name": "saved", "config": {"minutes_model": None}, "request_id": str(uuid4())},
        )
    else:
        result(
            ctx,
            "set_meeting_config",
            {"meeting_id": bundle.meeting_id, "minutes_model": None, "request_id": str(uuid4())},
        )
    deleted = delete_codex(ctx, config)
    assert not deleted.is_error, deleted.payload


@pytest.mark.parametrize("source", ["profile", "manifest"])
def test_corrupt_reference_file_blocks_connection_deletion(codex_context, source):
    ctx, _, config = codex_context
    if source == "profile":
        path = ctx.profiles.path
    else:
        path = selected_bundle(ctx, MeetingConfig()).manifest_path
    path.write_text("{invalid", encoding="utf-8")
    rejected = delete_codex(ctx, config)
    assert rejected.is_error and rejected.payload["error"]["code"] == "busy"


def test_profile_save_and_connection_deletion_are_serialized(codex_context, monkeypatch):
    ctx, _, config = codex_context
    saving, release, deleting, deleted = (threading.Event() for _ in range(4))
    original = ctx.profiles.set

    def save(*args, **kwargs):
        saving.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    def delete():
        deleting.set()
        try:
            return delete_codex(ctx, config)
        finally:
            deleted.set()

    monkeypatch.setattr(ctx.profiles, "set", save)
    with ThreadPoolExecutor(max_workers=2) as pool:
        saved = pool.submit(
            dispatch,
            ctx,
            "set_profile",
            {"name": "saved", "config": config.model_dump(mode="json"), "request_id": str(uuid4())},
        )
        try:
            assert saving.wait(5)
            removed = pool.submit(delete)
            assert deleting.wait(5)
            assert not deleted.wait(0.1)
        finally:
            release.set()
        assert not saved.result(timeout=5).is_error
        rejected = removed.result(timeout=5)
        assert rejected.is_error and rejected.payload["error"]["code"] == "busy"


def test_codex_job_cancellation_reaches_inflight_generation(codex_context, monkeypatch):
    ctx, backend, config = codex_context
    bundle = selected_bundle(ctx, config)
    write_fake_minutes(bundle)
    started, release = threading.Event(), threading.Event()

    def complete(*args, should_cancel, **kwargs):
        started.set()
        assert release.wait(5)
        assert should_cancel()
        raise CancelledError("fixture cancelled")

    monkeypatch.setattr(backend, "complete", complete)
    monkeypatch.setattr(processing.narumi_pipeline, "process_meeting", selected_generation)
    job_id = processing.enqueue_process(ctx, bundle.meeting_id)
    try:
        assert started.wait(5)
        result(ctx, "cancel_job", {"job_id": job_id, "request_id": str(uuid4())})
    finally:
        release.set()
    assert ctx.jobs.wait(job_id, timeout=5)["status"] == "cancelled"
    fresh = Bundle.find(ctx.meetings_root, bundle.meeting_id)
    assert fresh.manifest.status == "recorded"
    assert fresh.manifest.latest_minutes_version == 1


def test_recording_finalizes_even_if_selected_connection_becomes_disabled(codex_context):
    ctx, backend, config = codex_context
    started = result(
        ctx,
        "start_recording",
        {"config": config.model_dump(mode="json"), "request_id": str(uuid4())},
    )
    result(
        ctx,
        "set_provider_connection",
        {
            "connection_id": config.minutes_model.connection_id,
            "expected_revision": config.minutes_model.connection_revision,
            "enabled": False,
            "request_id": str(uuid4()),
        },
    )
    stopped = result(ctx, "stop_recording", {"request_id": str(uuid4())})
    assert not ctx.recorder.is_active
    job = ctx.jobs.wait(stopped["job_id"], timeout=5)
    assert job["status"] == "failed" and job["error"]["code"] == "configuration_conflict"
    fresh = Bundle.find(ctx.meetings_root, started["meeting_id"])
    assert fresh.manifest.recording.stopped_at is not None
    assert fresh.manifest.recording.tracks["mic"].sha256
    assert not any(call[0] == "complete" for call in backend.calls)


@pytest.mark.parametrize("source", ["import", "context"])
def test_automatic_generation_uses_the_saved_codex_selection(
    codex_context, monkeypatch, tmp_path, source
):
    ctx, backend, config = codex_context
    monkeypatch.setattr(processing.narumi_pipeline, "process_meeting", selected_generation)
    monkeypatch.setattr(processing.narumi_pipeline, "refresh_meeting", selected_generation)
    if source == "import":
        mic = write_silence_wav(tmp_path / "synthetic.wav")
        receipt = result(
            ctx,
            "import_recording",
            {
                "meeting_name": "synthetic import",
                "mic_path": str(mic),
                "config": config.model_dump(mode="json"),
                "request_id": str(uuid4()),
            },
        )
        meeting_id = receipt["meeting_id"]
    else:
        bundle = selected_bundle(ctx, config)
        meeting_id = bundle.meeting_id
        receipt = result(
            ctx,
            "register_context",
            {
                "meeting_id": meeting_id,
                "source_type": "document",
                "content": "synthetic context",
                "auto_regenerate": True,
                "expected_config": config.model_dump(mode="json"),
                "request_id": str(uuid4()),
            },
        )
    job = ctx.jobs.wait(receipt["job_id"], timeout=5)
    assert job["status"] == "succeeded", job
    fresh = Bundle.find(ctx.meetings_root, meeting_id)
    assert fresh.manifest.config.minutes_model == config.minutes_model
    assert fresh.manifest.minutes_versions[-1].provider == "codex-app-server"
    assert len([call for call in backend.calls if call[0] == "complete"]) == 2


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        ("revision", "configuration_conflict"),
        ("authentication", "authentication_required"),
        ("catalog", "model_unavailable"),
        ("runtime", "engine_unavailable"),
        ("model", "model_unavailable"),
        ("parameters", "invalid_argument"),
    ],
)
def test_meeting_save_validates_codex_observations_without_sending(codex_context, failure, code):
    ctx, backend, config = codex_context
    bundle = selected_bundle(ctx, MeetingConfig())
    with ctx.providers.store.transaction() as document:
        record = document["connections"][config.minutes_model.connection_id]
        if failure == "revision":
            record["revision"] += 1
        elif failure == "authentication":
            record["auth_state"] = "failed"
        elif failure == "catalog":
            record["catalog_state"] = "stale"
        elif failure == "runtime":
            document["runtimes"]["codex-app-server"]["state"] = "not_prepared"
    if failure == "model":
        config.minutes_model.model_id = "missing-fixture-model"
    elif failure == "parameters":
        config.minutes_model.parameters["reasoning_effort"] = "unsupported"
    rejected = dispatch(
        ctx,
        "set_meeting_config",
        {
            "meeting_id": bundle.meeting_id,
            **config.model_dump(mode="json"),
            "request_id": str(uuid4()),
        },
    )
    assert rejected.is_error and rejected.payload["error"]["code"] == code, rejected.payload
    assert Bundle.find(ctx.meetings_root, bundle.meeting_id).manifest.config == MeetingConfig()
    assert backend.calls == []


@pytest.mark.parametrize("expected", [False, True])
def test_codex_force_regeneration_is_rejected_before_job_creation(codex_context, expected):
    ctx, backend, config = codex_context
    bundle = selected_bundle(ctx, config)
    args = {"meeting_id": bundle.meeting_id, "force": True, "request_id": str(uuid4())}
    if expected:
        args["expected_config"] = config.model_dump(mode="json")
    rejected = dispatch(ctx, "regenerate", args)
    assert rejected.is_error and rejected.payload["error"]["code"] == "invalid_argument"
    assert not ctx.jobs.has_active(bundle.meeting_id)
    assert ctx.catalog.list_jobs(meeting_id=bundle.meeting_id) == []
    assert backend.calls == []
