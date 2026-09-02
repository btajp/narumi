from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from narumi.errors import CancelledError
from narumi.providers.claude import backend as backend_module
from narumi.providers.claude.backend import ClaudeSDKBackend
from narumi.providers.claude.protocol import WorkerResponse
from narumi.providers.claude.runtime import inspect_runtime
from narumi.providers.claude.snapshot import (
    RESOURCE_SHA256_FIELD,
    adapter_source_digest,
    claude_resource_sha256,
)

CONNECTIONS = ("conn-0123456789abcdef", "conn-fedcba9876543210")
KEY = "synthetic-snapshot-key-2917"
MODEL = "claude-snapshot-fixture"


def expected_runtime() -> dict[str, str]:
    evidence = inspect_runtime().public()
    return {
        **evidence,
        RESOURCE_SHA256_FIELD: claude_resource_sha256(evidence, adapter_source_digest()),
    }


def test_real_execution_image_is_created_once_reused_and_released(tmp_path, monkeypatch):
    calls = 0
    original = backend_module.create_execution_image

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(backend_module, "create_execution_image", counted)
    backend = ClaudeSDKBackend(tmp_path.resolve())
    runtime = expected_runtime()
    cli_path = None
    archive_descriptor = None
    for connection in CONNECTIONS:
        with pytest.raises(CancelledError) as failure:
            backend.complete(
                connection,
                KEY,
                MODEL,
                "fixture prompt",
                expected_runtime=runtime,
                should_cancel=lambda: True,
            )
        assert failure.value.details == {"outcome_unknown": False}
        assert backend._image is not None
        cli_path = backend._image.cli_path
        archive_descriptor = backend._image.archive_descriptor
        assert cli_path.is_file()
    assert calls == 1
    assert backend._image is not None
    with zipfile.ZipFile(f"/dev/fd/{archive_descriptor}") as archive:
        members = set(archive.namelist())
    assert "claude_agent_sdk/__init__.py" in members
    assert "narumi/providers/claude/worker.py" in members
    assert not any(member.endswith("/_bundled/claude") for member in members)

    bootstrap = """
import json
import os
import sys
archive = os.environ["NARUMI_CLAUDE_SNAPSHOT_ARCHIVE"]
dependencies = os.environ["NARUMI_CLAUDE_DEPENDENCY_ROOT"]
lease = int(os.environ["NARUMI_CLAUDE_BACKEND_LEASE_FD"])
stdlib = tuple(sys.path)
sys.path[:] = [archive, *stdlib, dependencies]
from pathlib import Path
from narumi.providers.claude.worker import load_sdk
sdk = load_sdk(json.loads(os.environ["EXPECTED_RUNTIME"]), Path.cwd())
import claude_agent_sdk
print(claude_agent_sdk.__file__)
print(sdk.evidence.cli_path)
print(os.get_inheritable(int(archive.rsplit("/", 1)[1])), os.get_inheritable(lease))
"""
    children = []
    snapshots = []
    for index in range(2):
        child_workspace = backend._image_workspace / f"parallel-{index}"
        child_workspace.mkdir(mode=0o700)
        child_tmp = child_workspace / "tmp"
        child_tmp.mkdir(mode=0o700)
        snapshot = backend._image.materialize(child_workspace)
        child_environment = snapshot.environment()
        assert backend._lease_descriptor is not None
        child_environment["NARUMI_CLAUDE_BACKEND_LEASE_FD"] = str(backend._lease_descriptor)
        child_environment["EXPECTED_RUNTIME"] = json.dumps(runtime, sort_keys=True)
        child_environment["TMPDIR"] = str(child_tmp)
        children.append(
            subprocess.Popen(
                [sys.executable, "-I", "-S", "-c", bootstrap],
                cwd=child_workspace,
                env=child_environment,
                pass_fds=(*snapshot.inherited_descriptors, backend._lease_descriptor),
                umask=0o077,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )
        snapshots.append(snapshot)
    for child, snapshot in zip(children, snapshots, strict=True):
        stdout, stderr = child.communicate(timeout=10)
        snapshot.close()
        assert child.returncode == 0, stderr
        assert stdout.splitlines() == [
            f"/dev/fd/{snapshot.archive_descriptor}/claude_agent_sdk/__init__.py",
            str(cli_path),
            "False False",
        ]

    if hasattr(os, "chflags"):
        os.chflags(cli_path, 0, follow_symlinks=False)
    os.chmod(cli_path, 0o700, follow_symlinks=False)
    with cli_path.open("r+b", buffering=0) as stream:
        stream.seek(-1, os.SEEK_END)
        original_byte = stream.read(1)
        stream.seek(-1, os.SEEK_END)
        stream.write(bytes([original_byte[0] ^ 1]))
        os.fsync(stream.fileno())
    os.chmod(cli_path, 0o500, follow_symlinks=False)
    with pytest.raises(RuntimeError, match="cached Claude CLI changed"):
        backend._image.verify_cli()

    backend.close()
    assert cli_path is not None and not cli_path.exists()
    assert archive_descriptor is not None
    with pytest.raises(OSError):
        os.fstat(archive_descriptor)


@dataclass
class _FakeSnapshot:
    runtime_evidence: dict[str, str]
    verify_calls: int = 0
    close_calls: int = 0

    @property
    def inherited_descriptors(self) -> tuple[int, ...]:
        return ()

    def environment(self) -> dict[str, str]:
        return {}

    def verify_after_execution(self) -> None:
        self.verify_calls += 1

    def close(self) -> None:
        self.close_calls += 1


class _FakeImage:
    def __init__(self, runtime_evidence: dict[str, str]):
        self.runtime_evidence = dict(runtime_evidence)
        self.materialize_calls = 0
        self.close_calls = 0

    def materialize(self, workspace: Path) -> _FakeSnapshot:
        assert workspace.is_dir()
        self.materialize_calls += 1
        return _FakeSnapshot(dict(self.runtime_evidence))

    def close(self) -> None:
        self.close_calls += 1


class _FakeSubprocessRunner:
    def __init__(self, command, *, inherited_fds=(), watchdog_held_fds=()):
        assert "-I" in command and "-S" in command
        assert len(inherited_fds) == 1
        assert watchdog_held_fds == inherited_fds

    def __call__(self, request, **kwargs):
        return WorkerResponse(
            "fixture minutes",
            request.model_id,
            {"input_tokens": 2, "output_tokens": 1},
            dict(request.expected_runtime),
        )


def test_parallel_first_use_builds_only_one_execution_image(tmp_path, monkeypatch):
    runtime = {
        "resource_id": "claude-agent-sdk-0-2-144",
        "sdk_version": "0.2.144",
        "cli_version": "2.1.239",
        "cli_sha256": "1" * 64,
        "sdk_source_sha256": "2" * 64,
        "isolation_profile_sha256": "3" * 64,
        RESOURCE_SHA256_FIELD: "4" * 64,
    }
    build_entered = threading.Event()
    release_build = threading.Event()
    calls = 0
    image = _FakeImage(runtime)

    def build(*args, **kwargs):
        nonlocal calls
        calls += 1
        build_entered.set()
        assert release_build.wait(2)
        return image

    monkeypatch.setattr(backend_module, "create_execution_image", build)
    monkeypatch.setattr(backend_module, "SubprocessWorkerRunner", _FakeSubprocessRunner)
    backend = ClaudeSDKBackend(tmp_path.resolve())
    results = []
    failures = []

    def complete(connection: str) -> None:
        try:
            results.append(
                backend.complete(
                    connection,
                    KEY,
                    MODEL,
                    "fixture prompt",
                    expected_runtime=runtime,
                )
            )
        except BaseException as error:
            failures.append(error)

    threads = [threading.Thread(target=complete, args=(connection,)) for connection in CONNECTIONS]
    for thread in threads:
        thread.start()
    assert build_entered.wait(2)
    release_build.set()
    for thread in threads:
        thread.join(3)
    assert not failures and len(results) == 2
    assert calls == 1 and image.materialize_calls == 2
    backend.close()
    assert image.close_calls == 1
