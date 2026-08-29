"""Interrupted and corrupted audio checkpoint storage must never cause a resend."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from pathlib import Path

import pytest
from narumi.bundle import Bundle
from narumi.errors import CancelledError, EngineUnavailableError
from narumi.transcribe import _storage
from narumi.transcribe import checkpoints as checkpoint_module
from narumi.transcribe.checkpoints import (
    TranscriptionCheckpoints,
    transcription_execution_lock,
)
from narumi.transcribe.chunks import TranscriptionPlan, build_transcription_plan

from .test_transcription_checkpoints import (
    assert_unknown,
    checkpoint_root,
    leave_pending,
    make_audio,
    make_case,
    plan_params,
    read_ledger,
    result_payload,
    write_ledger,
)


def assert_storage_refuses_dispatch(bundle: Bundle, plan: TranscriptionPlan) -> None:
    dispatched = []
    with pytest.raises(EngineUnavailableError) as caught, transcription_execution_lock(bundle):
        checkpoints = TranscriptionCheckpoints(bundle, plan, cache_epoch=10)
        checkpoints.preflight()
        checkpoints.begin_attempt(plan.chunks[0])
        dispatched.append(plan.chunks[0].fingerprint)
    assert caught.value.details == {
        "stage": "transcribe",
        "reason": "transcription_checkpoint_unavailable",
    }
    assert not dispatched


def complete_plan(bundle: Bundle, plan: TranscriptionPlan) -> None:
    with transcription_execution_lock(bundle):
        checkpoints = TranscriptionCheckpoints(bundle, plan, cache_epoch=0)
        checkpoints.preflight()
        for chunk in plan.chunks:
            checkpoints.succeed(checkpoints.begin_attempt(chunk), result_payload(chunk.track))


def result_path(bundle: Bundle, plan: TranscriptionPlan, index: int = 0) -> Path:
    fingerprint = plan.chunks[index].fingerprint
    attempt_id = read_ledger(bundle)["entries"][fingerprint]["attempt_id"]
    return checkpoint_root(bundle) / "results" / f"{fingerprint}-{attempt_id}.json"


def test_pending_receipt_and_fsync_finish_before_caller_can_dispatch(tmp_path: Path, monkeypatch):
    bundle, plan = make_case(tmp_path)
    fsyncs = []
    original = os.fsync

    def record_fsync(descriptor: int):
        fsyncs.append("directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file")
        return original(descriptor)

    with transcription_execution_lock(bundle):
        checkpoints = TranscriptionCheckpoints(bundle, plan, cache_epoch=3)
        checkpoints.preflight()
        monkeypatch.setattr(_storage.os, "fsync", record_fsync)
        attempt = checkpoints.begin_attempt(plan.chunks[0])
        entry = read_ledger(bundle)["entries"][plan.chunks[0].fingerprint]
        assert entry == {
            "state": "pending",
            "attempt_id": attempt.attempt_id,
            "epoch": 3,
            "was_unknown": False,
        }
        assert "file" in fsyncs and "directory" in fsyncs
        assert fsyncs.index("file") < fsyncs.index("directory")


def test_pending_save_failure_prevents_dispatch_and_preserves_unattempted_state(
    tmp_path: Path, monkeypatch
):
    bundle, plan = make_case(tmp_path)
    dispatched = []

    def fail_write(*_args, **_kwargs):
        raise OSError("Synthetic disk full before dispatch")

    with transcription_execution_lock(bundle):
        checkpoints = TranscriptionCheckpoints(bundle, plan, cache_epoch=0)
        checkpoints.preflight()
        monkeypatch.setattr(checkpoint_module, "write_bytes", fail_write)
        with pytest.raises(EngineUnavailableError) as caught:
            checkpoints.begin_attempt(plan.chunks[0])
            dispatched.append("sent")
        assert caught.value.details["reason"] == "transcription_checkpoint_unavailable"
    assert not dispatched
    assert read_ledger(bundle)["entries"][plan.chunks[0].fingerprint] == {"state": "unattempted"}


@pytest.mark.parametrize("failed_write", ["result", "ledger"])
def test_post_dispatch_save_failure_retains_pending_and_blocks_restart(
    tmp_path: Path, monkeypatch, failed_write: str
):
    bundle, plan = make_case(tmp_path)
    original = checkpoint_module.write_bytes

    def fail_selected(directory, name, data, **kwargs):
        is_ledger = name == "ledger.json"
        if is_ledger == (failed_write == "ledger"):
            raise OSError("Synthetic disk full after response")
        return original(directory, name, data, **kwargs)

    with transcription_execution_lock(bundle):
        checkpoints = TranscriptionCheckpoints(bundle, plan, cache_epoch=0)
        checkpoints.preflight()
        attempt = checkpoints.begin_attempt(plan.chunks[0])
        with monkeypatch.context() as patch:
            patch.setattr(checkpoint_module, "write_bytes", fail_selected)
            with pytest.raises(EngineUnavailableError) as caught:
                checkpoints.succeed(attempt, result_payload())
            assert_unknown(caught.value, plan)
    assert read_ledger(bundle)["entries"][plan.chunks[0].fingerprint]["state"] == "pending"
    with transcription_execution_lock(Bundle.open(bundle.path)):
        restored = TranscriptionCheckpoints(bundle, plan, cache_epoch=2)
        with pytest.raises(EngineUnavailableError) as caught:
            restored.preflight()
        assert_unknown(caught.value, plan)


def test_failure_receipt_save_error_does_not_erase_original_pending(tmp_path: Path, monkeypatch):
    bundle, plan = make_case(tmp_path)

    def fail_write(*_args, **_kwargs):
        raise OSError("Synthetic checkpoint I/O failure")

    with transcription_execution_lock(bundle):
        checkpoints = TranscriptionCheckpoints(bundle, plan, cache_epoch=0)
        checkpoints.preflight()
        attempt = checkpoints.begin_attempt(plan.chunks[0])
        before = read_ledger(bundle)
        with monkeypatch.context() as patch:
            patch.setattr(checkpoint_module, "write_bytes", fail_write)
            with pytest.raises(EngineUnavailableError) as caught:
                checkpoints.fail(attempt, TimeoutError("Synthetic response timeout"))
            assert_unknown(caught.value, plan)
        assert read_ledger(bundle) == before


def test_success_is_durable_even_when_cancellation_arrives_before_save(tmp_path: Path):
    bundle, plan = make_case(tmp_path)
    cancelled = False
    with transcription_execution_lock(bundle):
        checkpoints = TranscriptionCheckpoints(
            bundle, plan, cache_epoch=0, should_cancel=lambda: cancelled
        )
        checkpoints.preflight()
        attempt = checkpoints.begin_attempt(plan.chunks[0])
        cancelled = True
        checkpoints.succeed(attempt, result_payload())
        with pytest.raises(CancelledError):
            checkpoints.get_success(plan.chunks[0])
    with transcription_execution_lock(bundle):
        restored = TranscriptionCheckpoints(bundle, plan, cache_epoch=1)
        restored.preflight()
        assert restored.get_success(plan.chunks[0]) == result_payload()


@pytest.mark.parametrize("cancel_at", ["preflight", "begin"])
def test_cancellation_before_dispatch_does_not_create_pending(tmp_path: Path, cancel_at: str):
    bundle, plan = make_case(tmp_path)
    cancelled = cancel_at == "preflight"
    with transcription_execution_lock(bundle):
        checkpoints = TranscriptionCheckpoints(
            bundle, plan, cache_epoch=0, should_cancel=lambda: cancelled
        )
        if cancel_at == "preflight":
            with pytest.raises(CancelledError):
                checkpoints.preflight()
        else:
            checkpoints.preflight()
            cancelled = True
            with pytest.raises(CancelledError):
                checkpoints.begin_attempt(plan.chunks[0])
    assert all(
        entry == {"state": "unattempted"} for entry in read_ledger(bundle)["entries"].values()
    )


@pytest.mark.parametrize(
    "corruption",
    [
        "invalid_json",
        "duplicate_keys",
        "boolean_version",
        "boolean_epoch",
        "negative_epoch",
        "non_boolean_unknown",
        "unknown_field",
        "unknown_state",
        "missing_entry",
        "orphan_entry",
        "duplicate_chunk",
        "empty_plan",
        "known_failed_from_unknown",
        "invalid_attempt_id",
    ],
)
def test_malformed_or_inconsistent_ledger_refuses_all_dispatch(tmp_path: Path, corruption: str):
    bundle, plan = make_case(tmp_path)
    leave_pending(bundle, plan)
    path = checkpoint_root(bundle) / "ledger.json"
    ledger = read_ledger(bundle)
    entry = ledger["entries"][plan.chunks[0].fingerprint]
    if corruption == "invalid_json":
        path.write_bytes(b"{incomplete")
    elif corruption == "duplicate_keys":
        path.write_bytes(b'{"version":1,"version":1,"plans":{},"entries":{}}')
    else:
        if corruption == "boolean_version":
            ledger["version"] = True
        elif corruption == "boolean_epoch":
            entry["epoch"] = True
        elif corruption == "negative_epoch":
            entry["epoch"] = -1
        elif corruption == "non_boolean_unknown":
            entry["was_unknown"] = 1
        elif corruption == "unknown_field":
            entry["provider_diagnostics"] = "synthetic-private-data"
        elif corruption == "unknown_state":
            entry["state"] = "sent"
        elif corruption == "missing_entry":
            del ledger["entries"][plan.chunks[0].fingerprint]
        elif corruption == "orphan_entry":
            ledger["entries"]["f" * 64] = {"state": "unattempted"}
        elif corruption == "duplicate_chunk":
            ledger["plans"][plan.input_fingerprint].append(plan.chunks[0].fingerprint)
        elif corruption == "empty_plan":
            ledger["plans"][plan.input_fingerprint] = []
        elif corruption == "known_failed_from_unknown":
            entry.update(state="known_failed", was_unknown=True)
        elif corruption == "invalid_attempt_id":
            entry["attempt_id"] = "../result"
        write_ledger(bundle, ledger)
    assert_storage_refuses_dispatch(bundle, plan)


def test_oversized_ledger_refuses_dispatch_without_treating_it_as_missing(tmp_path: Path):
    bundle, plan = make_case(tmp_path)
    leave_pending(bundle, plan)
    (checkpoint_root(bundle) / "ledger.json").write_bytes(b" " * (_storage.MAX_JSON_BYTES + 1))
    assert_storage_refuses_dispatch(bundle, plan)


@pytest.mark.parametrize("corruption", ["missing", "modified", "duplicate_keys"])
def test_saved_plan_must_match_before_any_attempt(tmp_path: Path, corruption: str):
    bundle, plan = make_case(tmp_path)
    leave_pending(bundle, plan)
    path = checkpoint_root(bundle) / "plans" / f"{plan.input_fingerprint}.json"
    if corruption == "missing":
        path.unlink()
    elif corruption == "modified":
        payload = json.loads(path.read_bytes())
        payload["params"]["language"] = "en"
        path.write_text(json.dumps(payload))
    else:
        data = path.read_bytes()
        path.write_bytes(
            data.replace(b'{"chunker_version":', b'{"version":1,"chunker_version":', 1)
        )
    assert_storage_refuses_dispatch(bundle, plan)


@pytest.mark.parametrize(
    "corruption", ["missing", "hash", "semantic", "duplicate_keys", "oversize"]
)
def test_success_artifact_corruption_is_never_a_cache_miss(tmp_path: Path, corruption: str):
    bundle, plan = make_case(tmp_path)
    complete_plan(bundle, plan)
    path = result_path(bundle, plan)
    ledger = read_ledger(bundle)
    if corruption == "missing":
        path.unlink()
    else:
        if corruption == "hash":
            data = b'{"changed":true}'
        elif corruption == "semantic":
            payload = result_payload()
            payload["segments"][0]["end"] = 2.0
            data = json.dumps(payload).encode()
        elif corruption == "duplicate_keys":
            data = path.read_bytes().replace(b'{"duration":', b'{"duration":1,"duration":', 1)
        else:
            data = b" " * (_storage.MAX_JSON_BYTES + 1)
        path.write_bytes(data)
        if corruption != "hash":
            ledger["entries"][plan.chunks[0].fingerprint]["result_sha256"] = hashlib.sha256(
                data
            ).hexdigest()
            write_ledger(bundle, ledger)
    assert_storage_refuses_dispatch(bundle, plan)


def test_later_corrupt_success_is_checked_before_a_new_earlier_track_can_dispatch(tmp_path: Path):
    bundle, original_plan = make_case(tmp_path, ("system",))
    complete_plan(bundle, original_plan)
    result_path(bundle, original_plan).unlink()
    make_audio(bundle, "mic")
    with transcription_execution_lock(bundle):
        expanded = build_transcription_plan(
            bundle,
            sources={
                track: bundle.path / "preprocess" / f"{track}.wav" for track in ("mic", "system")
            },
            params=plan_params(),
        )
    assert_storage_refuses_dispatch(bundle, expanded)


@pytest.mark.parametrize("ledger_change", ["missing", "empty"])
@pytest.mark.parametrize("different_plan", [False, True])
def test_existing_plan_evidence_prevents_reinitializing_a_missing_or_emptied_ledger(
    tmp_path: Path, ledger_change: str, different_plan: bool
):
    bundle, plan = make_case(tmp_path, ("mic",))
    leave_pending(bundle, plan)
    if ledger_change == "missing":
        (checkpoint_root(bundle) / "ledger.json").unlink()
    else:
        write_ledger(bundle, {"version": 1, "plans": {}, "entries": {}})
    if different_plan:
        make_audio(bundle, "system")
        with transcription_execution_lock(bundle):
            plan = build_transcription_plan(
                bundle,
                sources={
                    track: bundle.path / "preprocess" / f"{track}.wav"
                    for track in ("mic", "system")
                },
                params=plan_params(),
            )
    assert_storage_refuses_dispatch(bundle, plan)


@pytest.mark.parametrize(
    "entry",
    [
        "ledger.json",
        "ledger.lock",
        "execution.lock",
        "plans",
        "results",
        "plan_file",
        "result_file",
    ],
)
def test_symlinked_checkpoint_entries_are_never_followed(tmp_path: Path, entry: str):
    bundle, plan = make_case(tmp_path)
    complete_plan(bundle, plan)
    root = checkpoint_root(bundle)
    if entry == "plan_file":
        path = root / "plans" / f"{plan.input_fingerprint}.json"
    elif entry == "result_file":
        path = result_path(bundle, plan)
    else:
        path = root / entry
    replacement = tmp_path / "untrusted-checkpoint-target"
    path.rename(replacement)
    path.symlink_to(replacement, target_is_directory=replacement.is_dir())
    assert_storage_refuses_dispatch(bundle, plan)


def test_result_with_multiple_hardlinks_is_rejected_before_resend(tmp_path: Path):
    bundle, plan = make_case(tmp_path)
    complete_plan(bundle, plan)
    os.link(result_path(bundle, plan), tmp_path / "unexpected-result-alias.json")
    assert_storage_refuses_dispatch(bundle, plan)


def test_mutating_returned_success_does_not_change_saved_result(tmp_path: Path):
    bundle, plan = make_case(tmp_path)
    complete_plan(bundle, plan)
    with transcription_execution_lock(bundle):
        checkpoints = TranscriptionCheckpoints(bundle, plan, cache_epoch=0)
        checkpoints.preflight()
        result = checkpoints.get_success(plan.chunks[0])
        before = copy.deepcopy(result)
        result["segments"][0]["text"] = "呼び出し側での変更"
        assert checkpoints.get_success(plan.chunks[0]) == before


def test_incomplete_plan_temporary_is_reclaimed_without_invalidating_existing_success(
    tmp_path: Path,
):
    bundle, plan = make_case(tmp_path)
    complete_plan(bundle, plan)
    temporary = checkpoint_root(bundle) / "plans" / f".{('f' * 64)}.json.{('e' * 32)}.tmp"
    temporary.write_bytes(b"incomplete")
    with transcription_execution_lock(bundle):
        resumed = TranscriptionCheckpoints(bundle, plan, cache_epoch=1)
        resumed.preflight()
        assert not temporary.exists()
        assert resumed.get_success(plan.chunks[0]) == result_payload("mic")


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_unsafe_plan_temporary_is_preserved_and_never_followed_or_deleted(
    tmp_path: Path, link_kind: str
):
    bundle, plan = make_case(tmp_path)
    complete_plan(bundle, plan)
    temporary = checkpoint_root(bundle) / "plans" / f".{('f' * 64)}.json.{('e' * 32)}.tmp"
    target = tmp_path / "unrelated-private-file"
    target.write_bytes(b"unchanged synthetic contents")
    if link_kind == "symlink":
        temporary.symlink_to(target)
    else:
        os.link(target, temporary)
    assert_storage_refuses_dispatch(bundle, plan)
    assert temporary.exists()
    assert target.read_bytes() == b"unchanged synthetic contents"


def test_unreferenced_final_plan_is_not_removed_as_a_temporary(tmp_path: Path):
    bundle, plan = make_case(tmp_path)
    complete_plan(bundle, plan)
    orphan = checkpoint_root(bundle) / "plans" / f"{('f' * 64)}.json"
    orphan.write_bytes(b"unexpected final plan")
    assert_storage_refuses_dispatch(bundle, plan)
    assert orphan.read_bytes() == b"unexpected final plan"


def test_invalid_response_cannot_be_persisted_as_success_or_retried_implicitly(tmp_path: Path):
    bundle, plan = make_case(tmp_path)
    with transcription_execution_lock(bundle):
        checkpoints = TranscriptionCheckpoints(bundle, plan, cache_epoch=0)
        checkpoints.preflight()
        attempt = checkpoints.begin_attempt(plan.chunks[0])
        invalid = result_payload()
        invalid["segments"] = []
        with pytest.raises(EngineUnavailableError) as caught:
            checkpoints.succeed(attempt, invalid)
        assert_unknown(caught.value, plan)
    with transcription_execution_lock(bundle):
        restored = TranscriptionCheckpoints(bundle, plan, cache_epoch=2)
        with pytest.raises(EngineUnavailableError) as caught:
            restored.preflight()
        assert_unknown(caught.value, plan)
