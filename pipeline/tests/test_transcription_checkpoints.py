"""Audio checkpoint state transitions, explicit retry proofs and execution leases."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import wave
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path
from typing import Any

import pytest
from narumi.bundle import Bundle
from narumi.errors import (
    BusyError,
    CancelledError,
    ConfigurationConflictError,
    EngineUnavailableError,
    InvalidArgumentError,
)
from narumi.transcribe.checkpoints import (
    TranscriptionCheckpoints,
    transcription_execution_lock,
)
from narumi.transcribe.chunks import TranscriptionPlan, build_transcription_plan
from narumi.transcription_selection import TranscriptionRetry


def plan_params() -> dict[str, Any]:
    return {
        "provider": "openai-api",
        "connection_id": "conn-0123456789ab",
        "connection_revision": 1,
        "model_id": "whisper-1",
        "language": "ja",
        "effective_parameters": {
            "response_format": "verbose_json",
            "timestamp_granularities": ["segment", "word"],
            "language": "ja",
        },
        "adapter_version": "1",
        "capability_table_version": "1",
        "runtime_version": "0.5.0",
        "runtime_sha256": "a" * 64,
        "runtime_catalog_revision": "synthetic-runtime-catalog",
        "model_capabilities_sha256": "b" * 64,
        "endpoint": "https://api.openai.com",
    }


def make_audio(bundle: Bundle, track: str) -> Path:
    path = bundle.path / "preprocess" / f"{track}.wav"
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16_000)
        stream.writeframes((b"\x01\x00" if track == "mic" else b"\x02\x00") * 16_000)
    return path


def make_case(
    tmp_path: Path, tracks: tuple[str, ...] = ("mic", "system")
) -> tuple[Bundle, TranscriptionPlan]:
    bundle = Bundle.create(tmp_path / "meetings", meeting_name="合成音声の再開確認")
    sources = {track: make_audio(bundle, track) for track in tracks}
    with transcription_execution_lock(bundle):
        plan = build_transcription_plan(bundle, sources=sources, params=plan_params())
    return bundle, plan


def result_payload(text: str = "合成された発話") -> dict[str, Any]:
    return {
        "text": text,
        "duration": 1.0,
        "segments": [{"native_id": 0, "start": 0.0, "end": 0.5, "text": text, "speaker": None}],
        "words": None,
        "language": "japanese",
        "usage": None,
    }


def checkpoint_root(bundle: Bundle) -> Path:
    return bundle.path / "preprocess" / "transcription"


def read_ledger(bundle: Bundle) -> dict[str, Any]:
    return json.loads((checkpoint_root(bundle) / "ledger.json").read_bytes())


def write_ledger(bundle: Bundle, document: dict[str, Any]) -> None:
    (checkpoint_root(bundle) / "ledger.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )


def retry_proof(plan: TranscriptionPlan, *, index: int = 0, epoch: int = 0) -> TranscriptionRetry:
    return TranscriptionRetry(
        input_fingerprint=plan.input_fingerprint,
        chunk_fingerprint=plan.chunks[index].fingerprint,
        blocked_epoch=epoch,
    )


def assert_unknown(
    error: EngineUnavailableError,
    plan: TranscriptionPlan,
    *,
    index: int = 0,
    epoch: int = 0,
    completed: int = 0,
) -> None:
    chunk = plan.chunks[index]
    assert error.details == {
        "stage": "transcribe",
        "reason": "transcription_outcome_unknown",
        "outcome_unknown": True,
        "input_fingerprint": plan.input_fingerprint,
        "chunk_fingerprint": chunk.fingerprint,
        "blocked_epoch": epoch,
        "track": chunk.track,
        "chunk_index": chunk.index,
        "chunk_count": len(plan.chunks),
        "completed_chunks": completed,
        "start_sample": chunk.start_sample,
        "end_sample": chunk.end_sample,
        "sample_rate": 16_000,
    }


def leave_pending(bundle: Bundle, plan: TranscriptionPlan, *, epoch: int = 0) -> None:
    with transcription_execution_lock(bundle):
        checkpoints = TranscriptionCheckpoints(bundle, plan, cache_epoch=epoch)
        checkpoints.preflight()
        checkpoints.begin_attempt(plan.chunks[0])


@pytest.mark.parametrize("restarted_epoch", [0, 9])
def test_successes_survive_restart_and_epoch_change_without_another_attempt(
    tmp_path: Path, restarted_epoch: int
):
    bundle, plan = make_case(tmp_path)
    with transcription_execution_lock(bundle):
        checkpoints = TranscriptionCheckpoints(bundle, plan, cache_epoch=0)
        checkpoints.preflight()
        for chunk in plan.chunks:
            assert checkpoints.get_success(chunk) is None
            attempt = checkpoints.begin_attempt(chunk)
            checkpoints.succeed(attempt, result_payload(chunk.track))
    ledger_before = (checkpoint_root(bundle) / "ledger.json").read_bytes()
    reopened = Bundle.open(bundle.path)
    with transcription_execution_lock(reopened):
        restored = TranscriptionCheckpoints(reopened, plan, cache_epoch=restarted_epoch)
        restored.preflight()
        assert [restored.get_success(chunk) for chunk in plan.chunks] == [
            result_payload(chunk.track) for chunk in plan.chunks
        ]
        with pytest.raises(ConfigurationConflictError):
            restored.begin_attempt(plan.chunks[0])
    assert (checkpoint_root(bundle) / "ledger.json").read_bytes() == ledger_before
    assert len(list((checkpoint_root(bundle) / "results").iterdir())) == 2
    assert not bundle.manifest.artifacts


@pytest.mark.parametrize("restarted_epoch", [0, 1, 100])
def test_pending_restart_and_epoch_change_alone_never_authorize_resend(
    tmp_path: Path, restarted_epoch: int
):
    bundle, plan = make_case(tmp_path)
    leave_pending(bundle, plan)
    before = read_ledger(bundle)
    with transcription_execution_lock(Bundle.open(bundle.path)):
        restored = TranscriptionCheckpoints(bundle, plan, cache_epoch=restarted_epoch)
        with pytest.raises(EngineUnavailableError) as caught:
            restored.preflight()
        assert_unknown(caught.value, plan)
        with pytest.raises(ConfigurationConflictError):
            restored.begin_attempt(plan.chunks[0])
    assert read_ledger(bundle) == before


@pytest.mark.parametrize(
    "failure",
    [
        EngineUnavailableError(
            "Synthetic HTTP 500 after dispatch",
            details={"reason": "provider_transcription_outcome_unknown", "outcome_unknown": True},
        ),
        TimeoutError("Synthetic response timeout"),
        OSError("Synthetic connection loss"),
        CancelledError("Synthetic post-dispatch cancellation", details={"outcome_unknown": True}),
    ],
)
def test_dispatched_failure_becomes_unknown_without_persisting_provider_diagnostics(
    tmp_path: Path, failure: BaseException
):
    bundle, plan = make_case(tmp_path)
    with transcription_execution_lock(bundle):
        checkpoints = TranscriptionCheckpoints(bundle, plan, cache_epoch=0)
        checkpoints.preflight()
        attempt = checkpoints.begin_attempt(plan.chunks[0])
        with pytest.raises(EngineUnavailableError) as caught:
            checkpoints.fail(attempt, failure)
        assert_unknown(caught.value, plan)
        assert str(failure) not in json.dumps(caught.value.to_payload())
        assert str(failure) not in (checkpoint_root(bundle) / "ledger.json").read_text()
    with transcription_execution_lock(bundle):
        restored = TranscriptionCheckpoints(bundle, plan, cache_epoch=0)
        with pytest.raises(EngineUnavailableError) as caught:
            restored.preflight()
        assert_unknown(caught.value, plan)


def test_initial_known_failure_can_be_retried_without_unknown_confirmation(tmp_path: Path):
    bundle, plan = make_case(tmp_path)
    with transcription_execution_lock(bundle):
        checkpoints = TranscriptionCheckpoints(bundle, plan, cache_epoch=0)
        checkpoints.preflight()
        first = checkpoints.begin_attempt(plan.chunks[0])
        assert (
            checkpoints.fail(first, InvalidArgumentError("Synthetic pre-dispatch failure")) is None
        )
        second = checkpoints.begin_attempt(plan.chunks[0])
        assert second.attempt_id != first.attempt_id
        checkpoints.succeed(second, result_payload())
        assert checkpoints.get_success(plan.chunks[0]) == result_payload()


def test_exact_unknown_proof_reuses_prior_success_and_retries_only_the_target(tmp_path: Path):
    bundle, plan = make_case(tmp_path)
    with transcription_execution_lock(bundle):
        original = TranscriptionCheckpoints(bundle, plan, cache_epoch=0)
        original.preflight()
        original.succeed(original.begin_attempt(plan.chunks[0]), result_payload("マイク"))
        original.begin_attempt(plan.chunks[1])
    prior_success = copy.deepcopy(read_ledger(bundle)["entries"][plan.chunks[0].fingerprint])
    with transcription_execution_lock(bundle):
        resumed = TranscriptionCheckpoints(
            bundle, plan, cache_epoch=1, retry=retry_proof(plan, index=1)
        )
        resumed.preflight()
        assert resumed.get_success(plan.chunks[0]) == result_payload("マイク")
        retry = resumed.begin_attempt(plan.chunks[1])
        assert retry.was_unknown and retry.epoch == 1
        assert read_ledger(bundle)["entries"][plan.chunks[1].fingerprint]["epoch"] == 1
        resumed.succeed(retry, result_payload("システム音声"))
        with pytest.raises(ConfigurationConflictError):
            resumed.begin_attempt(plan.chunks[1])
    assert read_ledger(bundle)["entries"][plan.chunks[0].fingerprint] == prior_success


def test_confirmed_first_unknown_can_continue_into_not_yet_attempted_chunks(tmp_path: Path):
    bundle, plan = make_case(tmp_path)
    leave_pending(bundle, plan)
    with transcription_execution_lock(bundle):
        resumed = TranscriptionCheckpoints(bundle, plan, cache_epoch=1, retry=retry_proof(plan))
        resumed.preflight()
        attempts = []
        for chunk in plan.chunks:
            attempt = resumed.begin_attempt(chunk)
            attempts.append(attempt)
            resumed.succeed(attempt, result_payload(chunk.track))
        assert [attempt.was_unknown for attempt in attempts] == [True, False]
        assert [resumed.get_success(chunk) for chunk in plan.chunks] == [
            result_payload(chunk.track) for chunk in plan.chunks
        ]


def test_retry_epoch_older_than_blocked_epoch_is_rejected(tmp_path: Path):
    bundle, plan = make_case(tmp_path)
    leave_pending(bundle, plan, epoch=3)
    with transcription_execution_lock(bundle):
        checkpoints = TranscriptionCheckpoints(
            bundle, plan, cache_epoch=2, retry=retry_proof(plan, epoch=3)
        )
        with pytest.raises(ConfigurationConflictError):
            checkpoints.preflight()
    assert read_ledger(bundle)["entries"][plan.chunks[0].fingerprint]["epoch"] == 3


@pytest.mark.parametrize(
    ("overrides", "epoch"),
    [
        ({"input_fingerprint": "f" * 64}, 1),
        ({"chunk_fingerprint": "f" * 64}, 1),
        ({"blocked_epoch": 1}, 2),
        ({}, 0),
    ],
)
def test_mismatched_or_not_newer_retry_proof_fails_before_attempt(
    tmp_path: Path, overrides: dict[str, Any], epoch: int
):
    bundle, plan = make_case(tmp_path)
    leave_pending(bundle, plan)
    proof = TranscriptionRetry.model_validate({**retry_proof(plan).model_dump(), **overrides})
    before = read_ledger(bundle)
    with transcription_execution_lock(bundle):
        checkpoints = TranscriptionCheckpoints(bundle, plan, cache_epoch=epoch, retry=proof)
        with pytest.raises(ConfigurationConflictError):
            checkpoints.preflight()
        with pytest.raises(ConfigurationConflictError):
            checkpoints.begin_attempt(plan.chunks[0])
    assert read_ledger(bundle) == before


@pytest.mark.parametrize("state", ["unattempted", "succeeded"])
def test_retry_proof_cannot_authorize_fresh_or_already_successful_input(tmp_path: Path, state: str):
    bundle, plan = make_case(tmp_path, ("mic",))
    with transcription_execution_lock(bundle):
        original = TranscriptionCheckpoints(bundle, plan, cache_epoch=0)
        original.preflight()
        if state == "succeeded":
            original.succeed(original.begin_attempt(plan.chunks[0]), result_payload())
        resumed = TranscriptionCheckpoints(bundle, plan, cache_epoch=1, retry=retry_proof(plan))
        with pytest.raises(ConfigurationConflictError):
            resumed.preflight()


def test_retry_known_failure_consumes_proof_and_remains_unknown_at_new_epoch(tmp_path: Path):
    bundle, plan = make_case(tmp_path)
    leave_pending(bundle, plan)
    with transcription_execution_lock(bundle):
        resumed = TranscriptionCheckpoints(bundle, plan, cache_epoch=1, retry=retry_proof(plan))
        resumed.preflight()
        attempt = resumed.begin_attempt(plan.chunks[0])
        with pytest.raises(EngineUnavailableError) as caught:
            resumed.fail(attempt, InvalidArgumentError("Synthetic retry pre-dispatch failure"))
        assert_unknown(caught.value, plan, epoch=1)
        with pytest.raises(EngineUnavailableError) as caught:
            resumed.begin_attempt(plan.chunks[0])
        assert_unknown(caught.value, plan, epoch=1)
    with transcription_execution_lock(bundle):
        stale = TranscriptionCheckpoints(bundle, plan, cache_epoch=2, retry=retry_proof(plan))
        with pytest.raises(ConfigurationConflictError):
            stale.preflight()
        valid = TranscriptionCheckpoints(
            bundle, plan, cache_epoch=2, retry=retry_proof(plan, epoch=1)
        )
        valid.preflight()
        valid.succeed(valid.begin_attempt(plan.chunks[0]), result_payload())


def test_confirmation_can_only_target_first_unknown_and_never_retries_the_next(tmp_path: Path):
    bundle, plan = make_case(tmp_path)
    leave_pending(bundle, plan)
    ledger = read_ledger(bundle)
    second = copy.deepcopy(ledger["entries"][plan.chunks[0].fingerprint])
    second.update(state="unknown", attempt_id="d" * 32)
    ledger["entries"][plan.chunks[1].fingerprint] = second
    write_ledger(bundle, ledger)
    with transcription_execution_lock(bundle):
        wrong = TranscriptionCheckpoints(
            bundle, plan, cache_epoch=1, retry=retry_proof(plan, index=1)
        )
        with pytest.raises(ConfigurationConflictError):
            wrong.preflight()
        resumed = TranscriptionCheckpoints(bundle, plan, cache_epoch=1, retry=retry_proof(plan))
        resumed.preflight()
        resumed.succeed(resumed.begin_attempt(plan.chunks[0]), result_payload())
        with pytest.raises(EngineUnavailableError) as caught:
            resumed.begin_attempt(plan.chunks[1])
        assert_unknown(caught.value, plan, index=1, completed=1)
    assert read_ledger(bundle)["entries"][plan.chunks[1].fingerprint] == second


@pytest.mark.parametrize("track", ["mic", "system"])
@pytest.mark.parametrize("saved_state", ["succeeded", "pending"])
def test_global_chunk_history_survives_a_track_being_added(
    tmp_path: Path, track: str, saved_state: str
):
    bundle, original_plan = make_case(tmp_path, (track,))
    with transcription_execution_lock(bundle):
        original = TranscriptionCheckpoints(bundle, original_plan, cache_epoch=0)
        original.preflight()
        attempt = original.begin_attempt(original_plan.chunks[0])
        if saved_state == "succeeded":
            original.succeed(attempt, result_payload(track))
    missing = "system" if track == "mic" else "mic"
    make_audio(bundle, missing)
    with transcription_execution_lock(bundle):
        expanded = build_transcription_plan(
            bundle,
            sources={
                item: bundle.path / "preprocess" / f"{item}.wav" for item in ("mic", "system")
            },
            params=plan_params(),
        )
        matching = next(chunk for chunk in expanded.chunks if chunk.track == track)
        assert expanded.input_fingerprint != original_plan.input_fingerprint
        assert matching.fingerprint == original_plan.chunks[0].fingerprint
        restored = TranscriptionCheckpoints(bundle, expanded, cache_epoch=2)
        if saved_state == "succeeded":
            restored.preflight()
            assert restored.get_success(matching) == result_payload(track)
        else:
            with pytest.raises(EngineUnavailableError) as caught:
                restored.preflight()
            assert_unknown(caught.value, expanded, index=matching.index)
            stale_proof = TranscriptionCheckpoints(
                bundle, expanded, cache_epoch=2, retry=retry_proof(original_plan)
            )
            with pytest.raises(ConfigurationConflictError):
                stale_proof.preflight()


def test_stale_attempt_cannot_replace_or_fail_the_confirmed_new_attempt(tmp_path: Path):
    bundle, plan = make_case(tmp_path)
    with transcription_execution_lock(bundle):
        original = TranscriptionCheckpoints(bundle, plan, cache_epoch=0)
        original.preflight()
        old_attempt = original.begin_attempt(plan.chunks[0])
        resumed = TranscriptionCheckpoints(bundle, plan, cache_epoch=1, retry=retry_proof(plan))
        resumed.preflight()
        current = resumed.begin_attempt(plan.chunks[0])
        before = read_ledger(bundle)
        with pytest.raises(ConfigurationConflictError):
            original.succeed(old_attempt, result_payload("古い結果"))
        with pytest.raises(ConfigurationConflictError):
            original.fail(old_attempt, TimeoutError("Synthetic late timeout"))
        assert read_ledger(bundle) == before
        resumed.succeed(current, result_payload("新しい結果"))
        assert resumed.get_success(plan.chunks[0]) == result_payload("新しい結果")


def test_preflight_and_strict_track_order_are_required(tmp_path: Path):
    bundle, plan = make_case(tmp_path)
    with transcription_execution_lock(bundle):
        checkpoints = TranscriptionCheckpoints(bundle, plan, cache_epoch=0)
        with pytest.raises(ConfigurationConflictError):
            checkpoints.get_success(plan.chunks[0])
        with pytest.raises(ConfigurationConflictError):
            checkpoints.begin_attempt(plan.chunks[0])
        checkpoints.preflight()
        with pytest.raises(ConfigurationConflictError):
            checkpoints.begin_attempt(plan.chunks[1])
        first = checkpoints.begin_attempt(plan.chunks[0])
        with pytest.raises(EngineUnavailableError) as caught:
            checkpoints.begin_attempt(plan.chunks[1])
        assert_unknown(caught.value, plan)
        checkpoints.succeed(first, result_payload())
        assert (
            checkpoints.begin_attempt(plan.chunks[1]).chunk_fingerprint
            == plan.chunks[1].fingerprint
        )


def test_constructor_and_operations_require_active_exclusive_lease(tmp_path: Path):
    bundle, plan = make_case(tmp_path)
    with pytest.raises(BusyError):
        TranscriptionCheckpoints(bundle, plan, cache_epoch=0)
    with transcription_execution_lock(bundle):
        checkpoints = TranscriptionCheckpoints(bundle, plan, cache_epoch=0)
        checkpoints.preflight()
        attempt = checkpoints.begin_attempt(plan.chunks[0])
    operations = [
        checkpoints.preflight,
        lambda: checkpoints.get_success(plan.chunks[0]),
        lambda: checkpoints.begin_attempt(plan.chunks[0]),
        lambda: checkpoints.succeed(attempt, result_payload()),
        lambda: checkpoints.fail(attempt, TimeoutError("Synthetic timeout")),
    ]
    for operation in operations:
        with pytest.raises(BusyError):
            operation()


def test_nested_and_cross_thread_attempts_do_not_steal_the_owner_lease(tmp_path: Path):
    bundle, plan = make_case(tmp_path)
    with transcription_execution_lock(bundle):
        checkpoints = TranscriptionCheckpoints(bundle, plan, cache_epoch=0)
        checkpoints.preflight()
        with pytest.raises(BusyError), transcription_execution_lock(bundle):
            pytest.fail("A second lease was granted")
        context = copy_context()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(context.run, checkpoints.begin_attempt, plan.chunks[0])
            with pytest.raises(BusyError):
                future.result(timeout=5)
        checkpoints.succeed(checkpoints.begin_attempt(plan.chunks[0]), result_payload())


def test_other_process_is_busy_while_fake_http_is_pending_and_can_lock_after_release(
    tmp_path: Path,
):
    bundle, plan = make_case(tmp_path)
    child = """
import sys
from pathlib import Path
from narumi.bundle import Bundle
from narumi.errors import BusyError
from narumi.transcribe.checkpoints import transcription_execution_lock
try:
    with transcription_execution_lock(Bundle.open(Path(sys.argv[1]))):
        print("acquired")
except BusyError:
    print("busy")
    sys.exit(42)
"""

    def contender():
        return subprocess.run(
            [sys.executable, "-I", "-c", child, str(bundle.path)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    with transcription_execution_lock(bundle):
        checkpoints = TranscriptionCheckpoints(bundle, plan, cache_epoch=0)
        checkpoints.preflight()
        checkpoints.begin_attempt(plan.chunks[0])
        pending = contender()
        assert (pending.returncode, pending.stdout, pending.stderr) == (42, "busy\n", "")
    released = contender()
    assert (released.returncode, released.stdout, released.stderr) == (0, "acquired\n", "")
    with transcription_execution_lock(bundle):
        restored = TranscriptionCheckpoints(bundle, plan, cache_epoch=0)
        with pytest.raises(EngineUnavailableError) as caught:
            restored.preflight()
        assert_unknown(caught.value, plan)
