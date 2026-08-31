"""Public transcription selections and recovery using real stages and a fake audio boundary."""

from __future__ import annotations

import copy
import json
import threading
from uuid import uuid4

import pytest
from api_transcription_fixtures import (
    MODELS,
    SECRET,
    FakeAudioBackend,
    assert_no_secret,
    audio_models,
    context,
    current_config,
    import_audio,
    regenerate,
    rejected,
    result,
    wait_job,
)
from api_transcription_fixtures import asr_context as asr_context
from conftest import make_recorded_bundle
from narumi.bundle import Bundle
from narumi.errors import EngineUnavailableError
from narumi.models import MeetingConfig
from narumi.pipeline import ProcessResult
from narumi.transcription_selection import TranscriptionRetry
from narumi_server.handlers import processing

from pipeline.tests.provider_fakes import FakeMetadata, MemorySecretStore


def recorded_meeting(state):
    bundle = make_recorded_bundle(state.ctx, meeting_id="20260829T000000Z-0000a5a0")
    result(
        state.ctx,
        "set_meeting_config",
        {
            "meeting_id": bundle.meeting_id,
            **state.config,
            "request_id": str(uuid4()),
        },
    )
    return bundle.meeting_id


def transcript(bundle, track):
    return json.loads(bundle.artifact_path(f"transcripts/own-{track}").read_text())


def save_epoch(state, meeting_id, epoch):
    expected = current_config(state, meeting_id)
    selected = copy.deepcopy(expected["transcription_model"])
    selected["cache_epoch"] = epoch
    return result(
        state.ctx,
        "set_meeting_config",
        {
            "meeting_id": meeting_id,
            "expected_config": expected,
            "transcription_model": selected,
            "request_id": str(uuid4()),
        },
    )["config"]


def unknown(job):
    assert job["status"] == "failed", job
    assert job["error"]["code"] == "engine_unavailable"
    details = job["error"]["details"]
    assert details["reason"] == "transcription_outcome_unknown"
    assert details["outcome_unknown"] is True
    return details


@pytest.mark.parametrize("asr_context", MODELS, indirect=True)
@pytest.mark.parametrize("language", ["ja", "auto"])
def test_asr_profile_survives_restart_and_transcribes_both_tracks(asr_context, language, caplog):
    state = asr_context
    state.config["language"] = language
    saved = result(
        state.ctx,
        "set_profile",
        {
            "name": "api-transcription",
            "config": state.config,
            "expected_config": MeetingConfig().model_dump(mode="json"),
            "request_id": str(uuid4()),
        },
    )["profile"]
    imported = import_audio(state, profile="api-transcription")
    meeting_id = imported["meeting_id"]
    assert current_config(state, meeting_id) == state.config
    assert state.audio.calls == []
    state.restart()
    assert result(state.ctx, "get_profile", {"name": "api-transcription"})["profile"] == saved
    completed = regenerate(state, meeting_id)
    assert completed["status"] == "succeeded", completed
    assert len(state.audio.calls) == 2
    for call in state.audio.calls:
        assert call["endpoint"] == "https://api.openai.com"
        assert call["api_key"] == SECRET
        assert call["model_id"] == state.config["transcription_model"]["model_id"]
        assert call["language"] == language
        assert not call["parameters"]
    assert [call["chunk_duration"] for call in state.audio.calls] == [1.0, 1.25]
    assert state.audio.calls[0]["audio"] != state.audio.calls[1]["audio"]
    bundle = Bundle.find(state.ctx.meetings_root, meeting_id)
    mic, system = transcript(bundle, "mic"), transcript(bundle, "system")
    for track, document in (("mic", mic), ("system", system)):
        assert document["source_id"] == f"own-{track}"
        assert document["engine"]["name"] == "openai-api"
        assert (
            document["engine"]["params"]["model"] == state.config["transcription_model"]["model_id"]
        )
        assert document["time_offset"] == 0.0
        assert document["segments"][0]["start"] == 0.125
        assert document["segments"][0]["end"] == 0.75
        assert document["segments"][0]["text"] == "合成音声の発話"
    if state.config["transcription_model"]["model_id"] == "whisper-1":
        assert mic["segments"][0]["words"][0]["start"] == 0.125
    else:
        assert mic["segments"][0]["speaker"] != system["segments"][0]["speaker"]
        assert system["segments"][0]["speaker"] not in {None, "A"}
    assert bundle.manifest.config.transcription_engine == "fake"
    assert bundle.manifest.config.llm_provider == "none"
    assert state.ctx.providers.http_backend.calls == []
    first_version = bundle.manifest.latest_minutes_version
    save_epoch(state, meeting_id, 1)
    again = regenerate(state, meeting_id)
    assert again["status"] == "succeeded", again
    assert len(state.audio.calls) == 2 and len(state.metadata.calls) == 1
    assert (
        Bundle.find(state.ctx.meetings_root, meeting_id).manifest.latest_minutes_version
        == first_version
    )
    assert_no_secret(state, caplog)


def test_null_asr_selection_restores_local_engine_and_reselecting_reuses_audio(asr_context):
    state = asr_context
    meeting_id = import_audio(state)["meeting_id"]
    assert regenerate(state, meeting_id)["status"] == "succeeded"
    selected = current_config(state, meeting_id)
    local = result(
        state.ctx,
        "set_meeting_config",
        {
            "meeting_id": meeting_id,
            "transcription_model": None,
            "external_send_policy": "local_only",
            "expected_config": selected,
            "request_id": str(uuid4()),
        },
    )["config"]
    assert local["transcription_engine"] == "fake" and local["transcription_model"] is None
    assert regenerate(state, meeting_id)["status"] == "succeeded"
    bundle = Bundle.find(state.ctx.meetings_root, meeting_id)
    assert transcript(bundle, "mic")["engine"]["name"] == "fake"
    result(
        state.ctx,
        "set_meeting_config",
        {
            "meeting_id": meeting_id,
            **selected,
            "expected_config": local,
            "request_id": str(uuid4()),
        },
    )
    assert regenerate(state, meeting_id)["status"] == "succeeded"
    assert len(state.audio.calls) == 2


def test_old_local_config_does_not_require_asr_snapshot_or_send_audio(asr_context):
    state = asr_context
    local = MeetingConfig(transcription_engine="fake").model_dump(mode="json")
    del local["transcription_model"]
    meeting_id = import_audio(state, config=local)["meeting_id"]
    assert current_config(state, meeting_id)["transcription_model"] is None
    receipt = result(
        state.ctx, "regenerate", {"meeting_id": meeting_id, "request_id": str(uuid4())}
    )
    assert wait_job(state, receipt["job_id"])["status"] == "succeeded"
    assert state.audio.calls == []


@pytest.mark.parametrize("tool", ["set_profile", "set_meeting_config"])
@pytest.mark.parametrize("policy", ["local_only", "subscription_ok"])
def test_asr_api_policy_is_required_before_saving(asr_context, tool, policy):
    state = asr_context
    bundle = make_recorded_bundle(state.ctx, meeting_id="20260829T000000Z-0000a5a0")
    config = {**state.config, "external_send_policy": policy}
    args = (
        {"name": "rejected", "config": config}
        if tool == "set_profile"
        else {"meeting_id": bundle.meeting_id, **config}
    )
    rejected(state.ctx, tool, {**args, "request_id": str(uuid4())}, "policy_violation")
    assert state.ctx.profiles.peek("rejected") is None
    assert (
        Bundle.find(state.ctx.meetings_root, bundle.meeting_id).manifest.config == MeetingConfig()
    )
    assert state.audio.calls == []


@pytest.mark.parametrize("change", ["missing", "snapshot", "force", "revision", "disabled"])
def test_asr_generation_rejects_stale_selection_before_creating_job(asr_context, change):
    state = asr_context
    meeting_id = recorded_meeting(state)
    args = {"meeting_id": meeting_id, "request_id": str(uuid4())}
    if change != "missing":
        args["expected_config"] = copy.deepcopy(state.config)
    if change == "snapshot":
        args["expected_config"]["language"] = "en"
    if change == "force":
        args["force"] = True
    if change in {"revision", "disabled"}:
        selected = state.config["transcription_model"]
        result(
            state.ctx,
            "set_provider_connection",
            {
                "connection_id": selected["connection_id"],
                "expected_revision": selected["connection_revision"],
                **(
                    {"enabled": False} if change == "disabled" else {"display_name": "変更した接続"}
                ),
                "request_id": str(uuid4()),
            },
        )
    rejected(
        state.ctx,
        "regenerate",
        args,
        "invalid_argument" if change == "force" else "configuration_conflict",
    )
    assert not state.ctx.jobs.has_active(meeting_id)
    assert state.audio.calls == []


@pytest.mark.parametrize("tool", ["set_profile", "set_meeting_config"])
def test_stale_epoch_save_cannot_overwrite_a_concurrent_config_change(asr_context, tool):
    state = asr_context
    if tool == "set_profile":
        result(state.ctx, tool, {"name": "cas", "config": state.config, "request_id": str(uuid4())})
        changed = result(
            state.ctx,
            tool,
            {
                "name": "cas",
                "config": {"language": "en"},
                "request_id": str(uuid4()),
            },
        )["profile"]["config"]
        args = {"name": "cas", "config": copy.deepcopy(state.config)}
        args["config"]["transcription_model"]["cache_epoch"] = 1
    else:
        meeting_id = recorded_meeting(state)
        changed = result(
            state.ctx,
            tool,
            {
                "meeting_id": meeting_id,
                "language": "en",
                "request_id": str(uuid4()),
            },
        )["config"]
        selected = {**state.config["transcription_model"], "cache_epoch": 1}
        args = {"meeting_id": meeting_id, "transcription_model": selected}
    rejected(
        state.ctx,
        tool,
        {**args, "expected_config": state.config, "request_id": str(uuid4())},
        "configuration_conflict",
    )
    stored = (
        result(state.ctx, "get_profile", {"name": "cas"})["profile"]["config"]
        if tool == "set_profile"
        else current_config(state, meeting_id)
    )
    assert stored == changed and stored["transcription_model"]["cache_epoch"] == 0
    assert state.audio.calls == []


@pytest.mark.parametrize("reference", ["profile", "unindexed_meeting"])
def test_asr_connection_references_are_protected_until_explicitly_cleared(asr_context, reference):
    state = asr_context
    if reference == "profile":
        result(
            state.ctx,
            "set_profile",
            {"name": "referenced", "config": state.config, "request_id": str(uuid4())},
        )
    else:
        meeting_id = recorded_meeting(state)
        state.ctx.catalog.delete_meeting(meeting_id)
    selected = state.config["transcription_model"]
    delete_args = {
        "connection_id": selected["connection_id"],
        "expected_revision": selected["connection_revision"],
        "confirm": True,
        "request_id": str(uuid4()),
    }
    rejected(state.ctx, "delete_provider_connection", delete_args, "busy")
    if reference == "profile":
        result(
            state.ctx,
            "set_profile",
            {
                "name": "referenced",
                "config": {"transcription_model": None},
                "expected_config": state.config,
                "request_id": str(uuid4()),
            },
        )
    else:
        result(
            state.ctx,
            "set_meeting_config",
            {
                "meeting_id": meeting_id,
                "transcription_model": None,
                "expected_config": state.config,
                "request_id": str(uuid4()),
            },
        )
    result(state.ctx, "delete_provider_connection", {**delete_args, "request_id": str(uuid4())})
    assert state.audio.calls == []


@pytest.mark.parametrize("surface", ["profile", "meeting"])
def test_one_connection_can_be_saved_for_minutes_and_asr_without_sending(asr_context, surface):
    state = asr_context
    model = copy.deepcopy(
        state.ctx.contracts["list_provider_models"].output_examples[0]["models"][0]
    )
    model.update(model_id="gpt-4.1", availability="available", reason=None)
    state.metadata.models.append(model)
    selected = state.config["transcription_model"]
    listed = result(
        state.ctx,
        "list_provider_models",
        {
            "connection_id": selected["connection_id"],
            "role": "llm",
            "refresh": True,
        },
    )
    assert [item["model_id"] for item in listed["models"]] == ["gpt-4.1"]
    config = copy.deepcopy(state.config)
    config["minutes_model"] = {
        **selected,
        "model_id": "gpt-4.1",
        "parameters": {"max_tokens": 512},
    }
    metadata_calls = list(state.metadata.calls)
    if surface == "profile":
        saved = result(
            state.ctx,
            "set_profile",
            {
                "name": "shared-openai",
                "config": config,
                "expected_config": MeetingConfig().model_dump(mode="json"),
                "request_id": str(uuid4()),
            },
        )["profile"]["config"]
        assert (
            result(state.ctx, "get_profile", {"name": "shared-openai"})["profile"]["config"]
            == config
        )
    else:
        bundle = make_recorded_bundle(state.ctx, meeting_id="20260829T000000Z-0000a5a0")
        saved = result(
            state.ctx,
            "set_meeting_config",
            {
                "meeting_id": bundle.meeting_id,
                **config,
                "expected_config": MeetingConfig().model_dump(mode="json"),
                "request_id": str(uuid4()),
            },
        )["config"]
        assert current_config(state, bundle.meeting_id) == config
    assert saved == config
    rejected(
        state.ctx,
        "delete_provider_connection",
        {
            "connection_id": selected["connection_id"],
            "expected_revision": selected["connection_revision"],
            "confirm": True,
            "request_id": str(uuid4()),
        },
        "busy",
    )
    assert state.metadata.calls == metadata_calls
    assert state.audio.calls == state.ctx.providers.http_backend.calls == []


def test_enqueued_retry_is_a_snapshot_even_if_caller_mutates_it(asr_context, monkeypatch):
    """Queue-boundary probe only; the refresh stand-in does not test audio or retry execution."""
    state = asr_context
    meeting_id = recorded_meeting(state)
    entered, release = threading.Event(), threading.Event()
    observed = []

    def occupy(_progress):
        entered.set()
        assert release.wait(15)
        return {}

    def observe(bundle, *, transcription_retry, **_kwargs):
        observed.append(transcription_retry)
        return ProcessResult(
            meeting_id=bundle.meeting_id, minutes_version=None, stages=[], skipped=[]
        )

    monkeypatch.setattr(processing.narumi_pipeline, "refresh_meeting", observe)
    original = TranscriptionRetry(
        input_fingerprint="a" * 64,
        chunk_fingerprint="b" * 64,
        blocked_epoch=0,
    )
    accepted = original.model_dump(mode="json")
    busy_job = state.ctx.jobs.submit("process", None, occupy)
    try:
        assert entered.wait(5)
        queued = processing.enqueue_regenerate(
            state.ctx,
            meeting_id,
            force=False,
            reason="retry snapshot fixture",
            transcription_retry=original,
        )
        assert result(state.ctx, "get_job_status", {"job_id": queued})["job"]["status"] == "queued"
        original.input_fingerprint = "c" * 64
        original.chunk_fingerprint = "d" * 64
        original.blocked_epoch = 7
    finally:
        release.set()
    assert wait_job(state, busy_job)["status"] == "succeeded"
    completed = wait_job(state, queued)
    assert completed["status"] == "succeeded", completed
    assert len(observed) == 1 and observed[0] is not original
    assert observed[0].model_dump(mode="json") == accepted
    assert state.audio.calls == state.ctx.providers.http_backend.calls == []


def automatic_recording(state):
    result(state.ctx, "start_recording", {"config": state.config, "request_id": str(uuid4())})
    return result(state.ctx, "stop_recording", {"discard_video": True, "request_id": str(uuid4())})


@pytest.mark.parametrize("source", ["import", "recording"])
def test_automatic_processing_uses_asr_without_retry_authorization(
    asr_context, monkeypatch, source
):
    state = asr_context
    original = processing.narumi_pipeline.process_meeting
    observed = []

    def capture(bundle, **kwargs):
        observed.append(kwargs)
        return original(bundle, **kwargs)

    monkeypatch.setattr(processing.narumi_pipeline, "process_meeting", capture)
    receipt = (
        import_audio(state, auto_process=True) if source == "import" else automatic_recording(state)
    )
    completed = wait_job(state, receipt["job_id"])
    assert completed["status"] == "succeeded", completed
    assert len(observed) == 1 and observed[0]["transcription_resolver"] is not None
    assert observed[0].get("transcription_retry") is None
    assert len(state.audio.calls) == 2
    assert not state.ctx.recorder.is_active


@pytest.mark.parametrize("source", ["import", "recording"])
@pytest.mark.parametrize("change", ["config", "disabled"])
def test_queued_auto_asr_checks_snapshot_and_keeps_finalized_audio(asr_context, source, change):
    state = asr_context
    started, release = threading.Event(), threading.Event()

    def occupy(_progress):
        started.set()
        assert release.wait(15)
        return {}

    busy_job = state.ctx.jobs.submit("process", None, occupy)
    try:
        assert started.wait(5)
        receipt = (
            import_audio(state, auto_process=True)
            if source == "import"
            else automatic_recording(state)
        )
        bundle = Bundle.find(state.ctx.meetings_root, receipt["meeting_id"])
        if change == "config":
            # Simulate an external edit of the authoritative file after job acceptance.
            bundle.manifest.config.language = "en"
            bundle.save()
        else:
            selected = state.config["transcription_model"]
            result(
                state.ctx,
                "set_provider_connection",
                {
                    "connection_id": selected["connection_id"],
                    "expected_revision": selected["connection_revision"],
                    "enabled": False,
                    "request_id": str(uuid4()),
                },
            )
    finally:
        release.set()
    assert wait_job(state, busy_job)["status"] == "succeeded"
    failed = wait_job(state, receipt["job_id"])
    assert failed["status"] == "failed" and failed["error"]["code"] == "configuration_conflict", (
        failed
    )
    fresh = Bundle.find(state.ctx.meetings_root, receipt["meeting_id"])
    assert fresh.manifest.recording.stopped_at is not None
    assert all(fresh.manifest.recording.tracks[track].sha256 for track in ("mic", "system"))
    assert not state.ctx.recorder.is_active and state.audio.calls == []


@pytest.mark.parametrize("asr_context", MODELS, indirect=True)
def test_unknown_audio_requires_exact_retry_and_reuses_other_track_after_restart(
    asr_context, caplog
):
    state = asr_context
    meeting_id = import_audio(state)["meeting_id"]
    state.audio.failures[2] = EngineUnavailableError(
        SECRET, details={"outcome_unknown": True, "upstream": SECRET}
    )
    details = unknown(regenerate(state, meeting_id))
    assert (
        details["track"],
        details["chunk_index"],
        details["chunk_count"],
        details["completed_chunks"],
    ) == ("system", 1, 2, 1)
    assert (details["start_sample"], details["end_sample"], details["sample_rate"]) == (
        0,
        20000,
        16000,
    )
    proof = {
        key: details[key] for key in ("input_fingerprint", "chunk_fingerprint", "blocked_epoch")
    }
    assert proof["blocked_epoch"] == 0 and len(state.audio.calls) == 2
    before = Bundle.find(state.ctx.meetings_root, meeting_id)
    mic_hash = before.artifact("transcripts/own-mic").sha256
    assert before.artifact("transcripts/own-system") is None
    state.restart()
    rebuilt = result(state.ctx, "rebuild_catalog", {"request_id": str(uuid4())})
    assert rebuilt["errors"] == [] and rebuilt["meetings"] == 1
    assert unknown(regenerate(state, meeting_id))["chunk_fingerprint"] == proof["chunk_fingerprint"]
    assert len(state.audio.calls) == 2
    save_epoch(state, meeting_id, 1)
    assert unknown(regenerate(state, meeting_id))["blocked_epoch"] == 0
    assert len(state.audio.calls) == 2
    for field in proof:
        stale = {**proof, field: 1 if field == "blocked_epoch" else "0" * 64}
        failed = regenerate(state, meeting_id, retry=stale)
        assert (
            failed["status"] == "failed" and failed["error"]["code"] == "configuration_conflict"
        ), failed
        assert len(state.audio.calls) == 2
    state.audio.failures[3] = EngineUnavailableError(SECRET, details={"outcome_unknown": True})
    retry_unknown = unknown(regenerate(state, meeting_id, retry=proof))
    assert len(state.audio.calls) == 3
    assert state.audio.calls[2]["audio"] == state.audio.calls[1]["audio"]
    assert retry_unknown["blocked_epoch"] == 1
    state.restart()
    consumed = regenerate(state, meeting_id, retry=proof)
    assert (
        consumed["status"] == "failed" and consumed["error"]["code"] == "configuration_conflict"
    ), consumed
    assert unknown(regenerate(state, meeting_id))["blocked_epoch"] == 1
    assert len(state.audio.calls) == 3
    save_epoch(state, meeting_id, 2)
    next_proof = {key: retry_unknown[key] for key in proof}
    completed = regenerate(state, meeting_id, retry=next_proof)
    assert completed["status"] == "succeeded", completed
    assert len(state.audio.calls) == 4
    assert state.audio.calls[3]["audio"] == state.audio.calls[1]["audio"]
    after = Bundle.find(state.ctx.meetings_root, meeting_id)
    assert after.artifact("transcripts/own-mic").sha256 == mic_hash
    assert after.artifact("transcripts/own-system") is not None
    reused = regenerate(state, meeting_id)
    assert reused["status"] == "succeeded" and len(state.audio.calls) == 4
    stale = regenerate(state, meeting_id, retry=next_proof)
    assert stale["status"] == "failed" and stale["error"]["code"] == "configuration_conflict", stale
    assert len(state.audio.calls) == 4
    assert_no_secret(state, caplog)


@pytest.mark.parametrize("transport", ["streamable-http", "stdio", "in-process"])
def test_asr_capability_is_advertised_only_on_resident_server(home, transport):
    ctx = context(
        home,
        MemorySecretStore(),
        FakeMetadata(audio_models()),
        FakeAudioBackend(),
        transport=transport,
    )
    try:
        capabilities = result(ctx, "get_server_info", {})["capabilities"]
        assert capabilities["transcription_model_providers"] == (
            ["openai-api"] if transport == "streamable-http" else []
        )
    finally:
        ctx.close()
