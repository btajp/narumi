from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import replace
from pathlib import Path

import pytest
from narumi.errors import CancelledError, EngineUnavailableError, ModelUnavailableError
from narumi.providers.claude import backend as backend_module
from narumi.providers.claude.backend import ClaudeSDKBackend
from narumi.providers.claude.protocol import (
    PROBE_PROMPT,
    PROBE_SENTINEL,
    PROBE_SYSTEM,
    WorkerResponse,
)

CONNECTION = "conn-0123456789abcdef"
KEY = "synthetic-claude-sdk-backend-key-73841"
MODEL = "claude-fixture-1-20260901"
PROMPT = "Synthetic meeting transcript whose contents must stay in the pipe"
RUNTIME = {
    "resource_id": "claude-agent-sdk-0-2-144",
    "sdk_version": "0.2.144",
    "cli_version": "2.1.239",
    "cli_sha256": "a" * 64,
    "sdk_source_sha256": "b" * 64,
    "isolation_profile_sha256": "c" * 64,
}


@pytest.fixture(autouse=True)
def fixed_runtime(monkeypatch):
    monkeypatch.setattr(
        backend_module,
        "inspect_runtime",
        lambda: type("Evidence", (), {"public": lambda self: dict(RUNTIME)})(),
    )


class FakeRunner:
    def __init__(self, response=None, error=None, runtime_override=None):
        self.response = response or WorkerResponse(
            "Fixture minutes", MODEL, {"input_tokens": 14, "output_tokens": 5}
        )
        self.error = error
        self.runtime_override = runtime_override
        self.calls = []

    def __call__(self, request, *, env, cwd, should_cancel, timeout):
        call = {
            "request": request,
            "env": dict(env),
            "cwd": Path(cwd),
            "cancelled": should_cancel(),
            "timeout": timeout,
        }
        self.calls.append(call)
        workspace = Path(env["HOME"]).parent
        assert Path(cwd).parent == workspace
        assert all(
            stat.S_IMODE(path.stat(follow_symlinks=False).st_mode) == 0o700
            for path in [
                workspace,
                *(workspace / name for name in ("home", "tmp", "config", "cwd")),
            ]
        )
        assert KEY not in repr(env) and PROMPT not in repr(env)
        assert KEY not in str(cwd) and PROMPT not in str(cwd)
        if self.error:
            raise self.error
        runtime = (
            request.expected_runtime if self.runtime_override is None else self.runtime_override
        )
        return replace(self.response, runtime_evidence=dict(runtime))


def test_backend_passes_secrets_only_in_memory_and_removes_private_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-secret")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/ambient/claude")
    runner = FakeRunner()
    backend = ClaudeSDKBackend(tmp_path / "data", runner=runner)
    result = backend.complete(CONNECTION, KEY, MODEL, PROMPT, system="Synthetic instructions")
    assert result.text == "Fixture minutes" and result.returned_model == MODEL
    call = runner.calls[0]
    assert call["request"].api_key == KEY and call["request"].prompt == PROMPT
    assert set(call["env"]) == {
        "HOME",
        "TMPDIR",
        "TMP",
        "TEMP",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_SECURESTORAGE_CONFIG_DIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "PATH",
        "LANG",
        "LC_ALL",
        "PYTHONNOUSERSITE",
        "PYTHONDONTWRITEBYTECODE",
        "CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK",
        "NO_COLOR",
    }
    assert not call["cwd"].exists()
    for path in (tmp_path / "data").rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert KEY.encode() not in data and PROMPT.encode() not in data


def test_fixed_probe_is_the_only_verification_prompt(tmp_path, monkeypatch):
    evidence = dict(RUNTIME)
    monkeypatch.setattr(
        backend_module,
        "inspect_runtime",
        lambda: type("Evidence", (), {"public": lambda self: evidence})(),
    )
    runner = FakeRunner(
        WorkerResponse(PROBE_SENTINEL, MODEL, {"input_tokens": 9, "output_tokens": 2})
    )
    backend = ClaudeSDKBackend(tmp_path, runner=runner)
    verified = backend.verify_model(CONNECTION, KEY, MODEL)
    request = runner.calls[0]["request"]
    assert request.prompt == PROBE_PROMPT and request.system == PROBE_SYSTEM
    assert verified.model_id == MODEL and verified.runtime_evidence == evidence


@pytest.mark.parametrize("text", ["almost", f" {PROBE_SENTINEL}", f"{PROBE_SENTINEL}\n"])
def test_probe_requires_the_exact_sentinel_without_retry(tmp_path, text):
    runner = FakeRunner(WorkerResponse(text, MODEL, {"input_tokens": 3, "output_tokens": 1}))
    backend = ClaudeSDKBackend(tmp_path, runner=runner)
    with pytest.raises(ModelUnavailableError) as failure:
        backend.verify_model(CONNECTION, KEY, MODEL)
    assert failure.value.details["reason"] == "claude_sdk_model_probe_failed"
    assert len(runner.calls) == 1


def test_invalid_worker_success_is_outcome_unknown_and_never_retried(tmp_path):
    runner = FakeRunner(
        WorkerResponse("Fixture minutes", "other-model", {"input_tokens": 3, "output_tokens": 1})
    )
    backend = ClaudeSDKBackend(tmp_path, runner=runner)
    with pytest.raises(EngineUnavailableError) as failure:
        backend.complete(CONNECTION, KEY, MODEL, PROMPT)
    assert failure.value.details == {
        "reason": "provider_generation_outcome_unknown",
        "outcome_unknown": True,
    }
    assert len(runner.calls) == 1


def test_cleanup_failure_after_success_is_outcome_unknown(tmp_path, monkeypatch):
    runner = FakeRunner()
    backend = ClaudeSDKBackend(tmp_path, runner=runner)
    monkeypatch.setattr(
        backend_module,
        "_remove_workspace",
        lambda _: (_ for _ in ()).throw(OSError("fixture cleanup failure")),
    )
    with pytest.raises(EngineUnavailableError) as failure:
        backend.complete(CONNECTION, KEY, MODEL, PROMPT)
    assert failure.value.details["outcome_unknown"] is True
    with pytest.raises(EngineUnavailableError) as poisoned:
        backend.complete(CONNECTION, KEY, MODEL, PROMPT)
    assert poisoned.value.details == {
        "reason": "claude_sdk_workspace_cleanup_failed",
        "outcome_unknown": False,
    }
    assert len(runner.calls) == 1


@pytest.mark.parametrize("operation", ["complete", "verify_model"])
def test_non_os_cleanup_failure_after_submission_is_redacted_unknown(
    tmp_path, monkeypatch, operation
):
    response = WorkerResponse(
        PROBE_SENTINEL if operation == "verify_model" else "Fixture minutes",
        MODEL,
        {"input_tokens": 3, "output_tokens": 1},
    )
    runner = FakeRunner(response)
    backend = ClaudeSDKBackend(tmp_path, runner=runner)
    private_detail = f"{KEY} {PROMPT} private cleanup detail"

    def fail_cleanup(_):
        raise RuntimeError(private_detail)

    monkeypatch.setattr(backend_module, "_remove_workspace", fail_cleanup)
    with pytest.raises(EngineUnavailableError) as failure:
        if operation == "verify_model":
            backend.verify_model(CONNECTION, KEY, MODEL)
        else:
            backend.complete(CONNECTION, KEY, MODEL, PROMPT)

    assert failure.value.details == {
        "reason": "provider_generation_outcome_unknown",
        "outcome_unknown": True,
    }
    assert len(runner.calls) == 1
    rendered = "".join(traceback.format_exception(failure.value))
    assert all(value not in rendered for value in (KEY, PROMPT, private_detail))


def test_invalid_response_plus_cleanup_failure_stays_outcome_unknown(tmp_path, monkeypatch):
    runner = FakeRunner(WorkerResponse("Fixture minutes", MODEL, {"input_tokens": -1}))
    backend = ClaudeSDKBackend(tmp_path, runner=runner)
    monkeypatch.setattr(
        backend_module,
        "_remove_workspace",
        lambda _: (_ for _ in ()).throw(OSError("fixture cleanup failure")),
    )
    with pytest.raises(EngineUnavailableError) as failure:
        backend.complete(CONNECTION, KEY, MODEL, PROMPT)
    assert failure.value.details["outcome_unknown"] is True


def test_close_and_cancellation_are_observed_without_exposing_credentials(tmp_path):
    def cancelled_runner(request, *, should_cancel, **kwargs):
        assert should_cancel()
        raise CancelledError("fixture", details={"outcome_unknown": False})

    backend = ClaudeSDKBackend(tmp_path, runner=cancelled_runner)
    with pytest.raises(CancelledError) as failure:
        backend.complete(CONNECTION, KEY, MODEL, PROMPT, should_cancel=lambda: True)
    assert failure.value.details == {"outcome_unknown": False}
    backend.close()
    with pytest.raises(EngineUnavailableError) as closed:
        backend.complete(CONNECTION, KEY, MODEL, PROMPT)
    assert closed.value.details["reason"] == "claude_sdk_backend_closed"
    assert KEY not in str(failure.value) and KEY not in str(closed.value)


def test_runtime_evidence_is_fixed_and_contains_no_path(tmp_path):
    backend = ClaudeSDKBackend(tmp_path, runner=FakeRunner())
    evidence = backend.runtime_evidence()
    assert evidence["sdk_version"] == "0.2.144"
    assert evidence["cli_version"] == "2.1.239"
    assert len(evidence["cli_sha256"]) == 64
    assert "path" not in evidence and all(os.path.sep not in value for value in evidence.values())


def test_selected_runtime_must_match_before_send_and_worker_evidence_after_send(tmp_path):
    changed = {**RUNTIME, "cli_sha256": "d" * 64}
    runner = FakeRunner()
    backend = ClaudeSDKBackend(tmp_path, runner=runner)
    with pytest.raises(EngineUnavailableError) as before_send:
        backend.complete(CONNECTION, KEY, MODEL, PROMPT, expected_runtime=changed)
    assert before_send.value.details == {
        "reason": "claude_sdk_runtime_changed",
        "outcome_unknown": False,
    }
    assert runner.calls == []
    backend.close()

    runner = FakeRunner(runtime_override=changed)
    backend = ClaudeSDKBackend(tmp_path, runner=runner)
    with pytest.raises(EngineUnavailableError) as after_send:
        backend.complete(CONNECTION, KEY, MODEL, PROMPT, expected_runtime=RUNTIME)
    assert after_send.value.details["outcome_unknown"] is True
    assert len(runner.calls) == 1


def test_probe_binds_the_catalog_selected_runtime_before_send(tmp_path):
    changed = {**RUNTIME, "sdk_source_sha256": "d" * 64}
    runner = FakeRunner(
        WorkerResponse(PROBE_SENTINEL, MODEL, {"input_tokens": 3, "output_tokens": 1})
    )
    backend = ClaudeSDKBackend(tmp_path, runner=runner)
    with pytest.raises(EngineUnavailableError) as failure:
        backend.verify_model(CONNECTION, KEY, MODEL, expected_runtime=changed)
    assert failure.value.details == {
        "reason": "claude_sdk_runtime_changed",
        "outcome_unknown": False,
    }
    assert runner.calls == []


def test_workspace_root_symlink_is_rejected_without_touching_target(tmp_path):
    root = tmp_path / "data"
    parent = root / "providers" / "runtime" / "claude-agent-sdk"
    parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    (parent / "runs").symlink_to(outside, target_is_directory=True)
    backend = ClaudeSDKBackend(root, runner=FakeRunner())
    with pytest.raises(EngineUnavailableError) as failure:
        backend.complete(CONNECTION, KEY, MODEL, PROMPT)
    assert failure.value.details["reason"] == "claude_sdk_workspace_unavailable"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755


def test_data_root_symlink_is_rejected_without_touching_target(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    root = tmp_path / "data-link"
    root.symlink_to(outside, target_is_directory=True)
    backend = ClaudeSDKBackend(root, runner=FakeRunner())
    with pytest.raises(EngineUnavailableError) as failure:
        backend.complete(CONNECTION, KEY, MODEL, PROMPT)
    assert failure.value.details["reason"] == "claude_sdk_workspace_unavailable"
    assert list(outside.iterdir()) == []


def test_close_waits_for_active_operation_to_observe_cancellation(tmp_path):
    started = threading.Event()

    def runner(request, *, should_cancel, **kwargs):
        started.set()
        while not should_cancel():
            time.sleep(0.01)
        raise CancelledError("fixture", details={"outcome_unknown": True})

    backend = ClaudeSDKBackend(tmp_path, runner=runner)
    failures = []

    def generate():
        try:
            backend.complete(CONNECTION, KEY, MODEL, PROMPT)
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=generate)
    thread.start()
    assert started.wait(1)
    backend.close()
    thread.join(1)
    assert not thread.is_alive()
    assert len(failures) == 1 and isinstance(failures[0], CancelledError)


def test_backend_startup_sweeps_owned_orphan_request_and_immutable_cache(tmp_path):
    runs = tmp_path / "providers" / "runtime" / "claude-agent-sdk" / "runs"
    runs.mkdir(parents=True, mode=0o700)
    os.chmod(runs, 0o700)
    request = runs / "conn-0123456789abcdef-abcdefgh"
    cache = runs / "execution-image-hgfedcba"
    for workspace in (request, cache):
        workspace.mkdir(mode=0o700)
        nested = workspace / "image"
        nested.mkdir(mode=0o700)
        artifact = nested / "artifact"
        artifact.write_bytes(b"orphan")
        os.chmod(artifact, 0o500)
        if workspace == cache and hasattr(os, "chflags"):
            os.chflags(artifact, getattr(stat, "UF_IMMUTABLE", 0), follow_symlinks=False)

    backend = ClaudeSDKBackend(tmp_path, runner=FakeRunner())
    assert backend._poison_reason is None
    assert list(runs.iterdir()) == []
    backend.close()


def test_orphan_symlink_fails_closed_without_touching_target(tmp_path):
    runs = tmp_path / "providers" / "runtime" / "claude-agent-sdk" / "runs"
    runs.mkdir(parents=True, mode=0o700)
    os.chmod(runs, 0o700)
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep"
    marker.write_text("keep")
    (runs / "conn-0123456789abcdef-abcdefgh").symlink_to(outside, target_is_directory=True)

    runner = FakeRunner()
    backend = ClaudeSDKBackend(tmp_path, runner=runner)
    with pytest.raises(EngineUnavailableError) as failure:
        backend.complete(CONNECTION, KEY, MODEL, PROMPT)
    assert failure.value.details == {
        "reason": "claude_sdk_workspace_unavailable",
        "outcome_unknown": False,
    }
    assert marker.read_text() == "keep" and runner.calls == []


def test_orphan_acl_rejection_fails_closed_before_deletion(tmp_path, monkeypatch):
    runs = tmp_path / "providers" / "runtime" / "claude-agent-sdk" / "runs"
    runs.mkdir(parents=True, mode=0o700)
    os.chmod(runs, 0o700)
    orphan = runs / "conn-0123456789abcdef-abcdefgh"
    orphan.mkdir(mode=0o700)
    rejected_inode = orphan.stat().st_ino
    original = backend_module.ensure_no_extended_allow_acl

    def reject_orphan(descriptor):
        if os.fstat(descriptor).st_ino == rejected_inode:
            raise OSError("fixture extended allow ACL")
        original(descriptor)

    monkeypatch.setattr(backend_module, "ensure_no_extended_allow_acl", reject_orphan)
    backend = ClaudeSDKBackend(tmp_path, runner=FakeRunner())
    with pytest.raises(EngineUnavailableError):
        backend.runtime_evidence()
    assert orphan.is_dir()


def test_backend_lease_excludes_concurrent_sweep_and_releases_on_close(tmp_path):
    first = ClaudeSDKBackend(tmp_path, runner=FakeRunner())
    blocked = ClaudeSDKBackend(tmp_path, runner=FakeRunner())
    with pytest.raises(EngineUnavailableError) as failure:
        blocked.runtime_evidence()
    assert failure.value.details["reason"] == "claude_sdk_workspace_unavailable"
    first.close()

    recovered = ClaudeSDKBackend(tmp_path, runner=FakeRunner())
    assert recovered.runtime_evidence()["sdk_version"] == "0.2.144"
    recovered.close()


def test_close_timeout_keeps_lease_until_last_operation_finishes(tmp_path, monkeypatch):
    monkeypatch.setattr(backend_module, "CLOSE_TIMEOUT", 0.05)
    started = threading.Event()
    release = threading.Event()

    def runner(request, **_):
        started.set()
        assert release.wait(2)
        return WorkerResponse(
            "Fixture minutes",
            request.model_id,
            {"input_tokens": 2, "output_tokens": 1},
            dict(request.expected_runtime),
        )

    backend = ClaudeSDKBackend(tmp_path, runner=runner)
    thread = threading.Thread(
        target=lambda: backend.complete(CONNECTION, KEY, MODEL, PROMPT),
        daemon=True,
    )
    thread.start()
    assert started.wait(1)
    with pytest.raises(EngineUnavailableError):
        backend.close()
    blocked = ClaudeSDKBackend(tmp_path, runner=FakeRunner())
    assert blocked._poison_reason is not None

    release.set()
    thread.join(2)
    assert not thread.is_alive() and backend._lease_descriptor is None
    recovered = ClaudeSDKBackend(tmp_path, runner=FakeRunner())
    assert recovered._poison_reason is None
    recovered.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX inherited lease semantics")
def test_server_sigkill_guardian_holds_lease_then_next_backend_sweeps_orphan(tmp_path):
    root = tmp_path.resolve()
    ready = root / "lease-guardian.ready"
    worker = f"""
import signal, sys, time
sys.stdin.buffer.read()
signal.signal(signal.SIGTERM, signal.SIG_IGN)
open({str(ready)!r}, "w").write("ready")
time.sleep(30)
"""
    wrapper = f"""
import sys
from pathlib import Path
from narumi.providers.claude.backend import ClaudeSDKBackend
from narumi.providers.claude.protocol import WorkerRequest
from narumi.providers.claude.transport import SubprocessWorkerRunner
root = Path({str(root)!r})
backend = ClaudeSDKBackend(root, runner=lambda *args, **kwargs: None)
backend._workspace({CONNECTION!r})
lease = backend._lease_descriptor
request = WorkerRequest(
    {CONNECTION!r}, {KEY!r}, {MODEL!r}, {PROMPT!r}, None, {RUNTIME!r}
)
SubprocessWorkerRunner(
    (sys.executable, "-I", "-c", {worker!r}),
    inherited_fds=(lease,),
    watchdog_held_fds=(lease,),
)(request, env={{}}, cwd=root, should_cancel=lambda: False, timeout=30)
"""
    server = subprocess.Popen(
        (sys.executable, "-I", "-c", wrapper),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    deadline = time.monotonic() + 3
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ready.exists()
    os.kill(server.pid, signal.SIGKILL)
    server.wait(timeout=3)

    blocked = ClaudeSDKBackend(root, runner=FakeRunner())
    assert blocked._poison_reason is not None
    deadline = time.monotonic() + 6
    recovered = None
    while time.monotonic() < deadline:
        candidate = ClaudeSDKBackend(root, runner=FakeRunner())
        if candidate._poison_reason is None:
            recovered = candidate
            break
        time.sleep(0.05)
    assert recovered is not None
    runs = root / "providers" / "runtime" / "claude-agent-sdk" / "runs"
    assert list(runs.iterdir()) == []
    recovered.close()
