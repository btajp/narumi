from __future__ import annotations

import asyncio
import contextvars
import fcntl
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from narumi.bundle import Bundle, manifest_writer_lock
from narumi.bundle import manifest_writer as writer
from narumi.errors import (
    BusyError,
    ConfigurationConflictError,
    ErrorCode,
    InvalidArgumentError,
    NarumiError,
)

MEETING_A = "20260829T010203Z-00000001"
MEETING_B = "20260829T010203Z-00000002"


def test_lock_is_outside_bundle_private_stable_and_same_thread_reentrant(tmp_path: Path):
    with manifest_writer_lock(tmp_path, MEETING_A):
        with manifest_writer_lock(tmp_path, MEETING_A, timeout=0):
            lock_path = tmp_path / ".manifest-locks" / f"{MEETING_A}.lock"
            first_inode = lock_path.stat().st_ino
            assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
            assert stat.S_IMODE(lock_path.parent.stat().st_mode) == 0o700
    with manifest_writer_lock(tmp_path, MEETING_A):
        assert lock_path.stat().st_ino == first_inode


def test_copied_lease_is_not_inherited_by_another_thread(tmp_path: Path):
    result: list[str] = []
    with manifest_writer_lock(tmp_path, MEETING_A):
        context = contextvars.copy_context()

        def contend() -> None:
            try:
                with manifest_writer_lock(tmp_path, MEETING_A, timeout=0):
                    result.append("acquired")
            except BusyError:
                result.append("busy")

        thread = threading.Thread(target=lambda: context.run(contend))
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert result == ["busy"]


def test_lease_is_not_inherited_by_an_async_child_task(tmp_path: Path):
    async def scenario() -> str:
        with manifest_writer_lock(tmp_path, MEETING_A):

            async def contend() -> str:
                try:
                    with manifest_writer_lock(tmp_path, MEETING_A, timeout=0):
                        return "acquired"
                except BusyError:
                    return "busy"

            return await asyncio.create_task(contend())

    assert asyncio.run(scenario()) == "busy"


def test_different_meetings_do_not_share_process_mutex(tmp_path: Path):
    acquired = threading.Event()

    def acquire_other() -> None:
        with manifest_writer_lock(tmp_path, MEETING_B, timeout=0.2):
            acquired.set()

    with manifest_writer_lock(tmp_path, MEETING_A):
        thread = threading.Thread(target=acquire_other)
        thread.start()
        thread.join(timeout=2)
        assert acquired.is_set()


def test_other_process_waits_for_same_meeting_lock(tmp_path: Path):
    program = """
import sys
from pathlib import Path
from narumi.bundle import manifest_writer_lock
print("ready", flush=True)
with manifest_writer_lock(Path(sys.argv[1]), sys.argv[2], timeout=5):
    print("acquired", flush=True)
"""
    child = None
    with manifest_writer_lock(tmp_path, MEETING_A):
        child = subprocess.Popen(
            [sys.executable, "-c", program, str(tmp_path), MEETING_A],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"
        with pytest.raises(subprocess.TimeoutExpired):
            child.wait(timeout=0.2)
    assert child is not None
    stdout, stderr = child.communicate(timeout=10)
    assert child.returncode == 0, stderr
    assert stdout.strip() == "acquired"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_forked_child_closes_inherited_fd_and_does_not_reenter(tmp_path: Path):
    read_fd, write_fd = os.pipe()
    with manifest_writer_lock(tmp_path, MEETING_A):
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            try:
                try:
                    with manifest_writer_lock(tmp_path, MEETING_A, timeout=0.1):
                        outcome = b"acquired"
                except BusyError:
                    outcome = b"busy"
                os.write(write_fd, outcome)
            finally:
                os.close(write_fd)
                os._exit(0)
        os.close(write_fd)
        _, status = os.waitpid(pid, 0)
        outcome = os.read(read_fd, 32)
        os.close(read_fd)
        assert os.waitstatus_to_exitcode(status) == 0
        assert outcome == b"busy"
    with manifest_writer_lock(tmp_path, MEETING_A, timeout=0):
        pass


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_lock_fd_is_registered_before_open_returns_to_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    read_fd, write_fd = os.pipe()
    original_open = writer._open_regular
    triggered = False

    def open_and_fork(directory: int, name: str, flags: int, **kwargs: object) -> int:
        nonlocal triggered
        descriptor = original_open(directory, name, flags, **kwargs)
        if name.endswith(".lock") and not triggered:
            triggered = True
            pid = os.fork()
            if pid == 0:
                os.close(read_fd)
                try:
                    try:
                        os.fstat(descriptor)
                        outcome = b"inherited-open"
                    except OSError:
                        outcome = b"closed"
                    os.write(write_fd, outcome)
                finally:
                    os.close(write_fd)
                    os._exit(0)
            _, status = os.waitpid(pid, 0)
            assert os.waitstatus_to_exitcode(status) == 0
        return descriptor

    monkeypatch.setattr(writer, "_open_regular", open_and_fork)
    with manifest_writer_lock(tmp_path, MEETING_A):
        pass
    os.close(write_fd)
    assert os.read(read_fd, 64) == b"closed"
    os.close(read_fd)


def test_ambiguous_close_does_not_leak_process_mutex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    original_close = writer.os.close
    original_open = writer._open_regular
    lock_descriptor: int | None = None
    fail_once = True

    def remember_lock(directory: int, name: str, flags: int, **kwargs: object) -> int:
        nonlocal lock_descriptor
        descriptor = original_open(directory, name, flags, **kwargs)
        if name.endswith(".lock"):
            lock_descriptor = descriptor
        return descriptor

    def close_then_fail(descriptor: int) -> None:
        nonlocal fail_once
        if descriptor == lock_descriptor and fail_once:
            fail_once = False
            original_close(descriptor)
            raise OSError("synthetic ambiguous close")
        original_close(descriptor)

    monkeypatch.setattr(writer, "_open_regular", remember_lock)
    monkeypatch.setattr(writer.os, "close", close_then_fail)
    with pytest.raises(NarumiError, match="saved securely"):
        with manifest_writer_lock(tmp_path, MEETING_A):
            pass
    with manifest_writer_lock(tmp_path, MEETING_A, timeout=0):
        pass


@pytest.mark.parametrize("opener", ["bundle", "lock"])
def test_parent_close_failure_does_not_leak_returned_child_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opener: str
):
    Bundle.create(tmp_path, meeting_name="original", meeting_id=MEETING_A)
    original_root = writer._open_root
    original_open, original_close = writer.os.open, writer.os.close
    root_descriptor: int | None = None
    child_descriptor: int | None = None
    fail_once = True
    child_name = MEETING_A if opener == "bundle" else writer.LOCK_DIRECTORY_NAME

    def remember_root(root: Path, *, create: bool) -> int:
        nonlocal root_descriptor
        root_descriptor = original_root(root, create=create)
        return root_descriptor

    def remember_child(path, *args: object, **kwargs: object) -> int:
        nonlocal child_descriptor
        descriptor = original_open(path, *args, **kwargs)
        if path == child_name:
            child_descriptor = descriptor
        return descriptor

    def close_parent_then_fail(descriptor: int) -> None:
        nonlocal fail_once
        if descriptor == root_descriptor and fail_once:
            fail_once = False
            original_close(descriptor)
            raise OSError("synthetic parent close failure")
        original_close(descriptor)

    monkeypatch.setattr(writer, "_open_root", remember_root)
    monkeypatch.setattr(writer.os, "open", remember_child)
    monkeypatch.setattr(writer.os, "close", close_parent_then_fail)
    with pytest.raises(OSError):
        if opener == "bundle":
            writer._open_bundle_directory(tmp_path, MEETING_A)
        else:
            writer._open_lock_directory(tmp_path)
    assert child_descriptor is not None
    with pytest.raises(OSError):
        os.fstat(child_descriptor)


@pytest.mark.parametrize("bad_id", ["../manifest", "bad", "20260829T010203Z-ABCDEF00"])
def test_unsafe_meeting_ids_are_rejected_before_io(tmp_path: Path, bad_id: str):
    with pytest.raises(InvalidArgumentError):
        with manifest_writer_lock(tmp_path, bad_id):
            pass
    assert not (tmp_path / ".manifest-locks").exists()


@pytest.mark.parametrize("timeout", [-1, float("nan"), float("inf"), "1"])
def test_invalid_timeouts_are_rejected_before_io(tmp_path: Path, timeout: object):
    with pytest.raises(InvalidArgumentError):
        with manifest_writer_lock(tmp_path, MEETING_A, timeout=timeout):  # type: ignore[arg-type]
            pass
    assert not (tmp_path / ".manifest-locks").exists()


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_lock_links_are_rejected_without_touching_target(tmp_path: Path, link_kind: str):
    lock_directory = tmp_path / ".manifest-locks"
    lock_directory.mkdir(mode=0o700)
    target = tmp_path / "unrelated.txt"
    target.write_text("unchanged", encoding="utf-8")
    target.chmod(0o600)
    lock_path = lock_directory / f"{MEETING_A}.lock"
    if link_kind == "symlink":
        lock_path.symlink_to(target)
    else:
        os.link(target, lock_path)

    with pytest.raises(NarumiError) as failure:
        with manifest_writer_lock(tmp_path, MEETING_A):
            pass
    assert failure.value.code == ErrorCode.INTERNAL
    assert str(tmp_path) not in str(failure.value)
    assert target.read_text(encoding="utf-8") == "unchanged"


def test_unsafe_lock_permissions_are_rejected(tmp_path: Path):
    lock_directory = tmp_path / ".manifest-locks"
    lock_directory.mkdir(mode=0o700)
    lock_path = lock_directory / f"{MEETING_A}.lock"
    lock_path.write_bytes(b"")
    lock_path.chmod(0o644)
    with pytest.raises(NarumiError, match="saved securely"):
        with manifest_writer_lock(tmp_path, MEETING_A):
            pass


def test_stale_bundle_cannot_overwrite_newer_manifest(tmp_path: Path):
    current = Bundle.create(tmp_path, meeting_name="original", meeting_id=MEETING_A)
    stale = Bundle.open(current.path)
    current.manifest.meeting_name = "newer"
    current.save()
    stale.manifest.status = "recorded"

    with pytest.raises(ConfigurationConflictError) as failure:
        stale.save()
    assert failure.value.details == {"reason": "manifest_generation_conflict"}
    persisted = Bundle.open(current.path).manifest
    assert persisted.meeting_name == "newer"
    assert persisted.status == "recording"


def test_bundle_writer_lock_verifies_generation_and_reenters(tmp_path: Path):
    bundle = Bundle.create(tmp_path, meeting_name="original", meeting_id=MEETING_A)
    stale = Bundle.open(bundle.path)
    with bundle.writer_lock(timeout=None):
        with bundle.writer_lock(timeout=0):
            bundle.manifest.meeting_name = "current"
            bundle.save()
    with pytest.raises(ConfigurationConflictError):
        with stale.writer_lock(timeout=0):
            pytest.fail("stale bundle entered its writer lease")


def test_raw_byte_change_conflicts_even_when_json_is_equivalent(tmp_path: Path):
    bundle = Bundle.create(tmp_path, meeting_name="original", meeting_id=MEETING_A)
    before = bundle.manifest_path.read_bytes()
    bundle.manifest_path.write_bytes(before + b" \n")
    bundle.manifest.meeting_name = "stale"
    with pytest.raises(ConfigurationConflictError):
        bundle.save()
    assert bundle.manifest_path.read_bytes() == before + b" \n"


def test_directory_swap_cannot_redirect_a_verified_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    bundle = Bundle.create(tmp_path, meeting_name="original", meeting_id=MEETING_A)
    replacement = tmp_path / "replacement"
    displaced = tmp_path / "displaced"
    shutil.copytree(bundle.path, replacement)
    replacement_manifest = json.loads((replacement / "manifest.json").read_text(encoding="utf-8"))
    replacement_manifest["meeting_name"] = "replacement"
    (replacement / "manifest.json").write_text(
        json.dumps(replacement_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    bundle.manifest.meeting_name = "writer"
    original_read = writer._read_manifest_at
    calls = 0

    def swap_after_cas(directory: int):
        nonlocal calls
        snapshot = original_read(directory)
        calls += 1
        if calls == 2:
            bundle.path.rename(displaced)
            replacement.rename(bundle.path)
        return snapshot

    monkeypatch.setattr(writer, "_read_manifest_at", swap_after_cas)
    with pytest.raises(ConfigurationConflictError) as failure:
        bundle.save()
    assert failure.value.details["outcome_unknown"] is True
    assert json.loads(bundle.manifest_path.read_text(encoding="utf-8"))["meeting_name"] == (
        "replacement"
    )
    assert (
        json.loads((displaced / "manifest.json").read_text(encoding="utf-8"))["meeting_name"]
        == "writer"
    )


def test_directory_swap_with_same_manifest_hash_is_detected_by_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    bundle = Bundle.create(tmp_path, meeting_name="original", meeting_id=MEETING_A)
    replacement = tmp_path / "replacement"
    displaced = tmp_path / "displaced"
    shutil.copytree(bundle.path, replacement)
    bundle.abspath("tracks/parent-only.raw").write_bytes(b"parent-only")
    fixed_time = "2030-01-02T03:04:05Z"
    bundle.manifest.meeting_name = "writer"
    expected = bundle.manifest.model_copy(deep=True)
    expected.updated_at = fixed_time
    (replacement / "manifest.json").write_text(
        expected.model_dump_json(indent=2, exclude_none=False) + "\n", encoding="utf-8"
    )
    original_read = writer._read_manifest_at
    calls = 0

    def swap_after_cas(directory: int):
        nonlocal calls
        snapshot = original_read(directory)
        calls += 1
        if calls == 2:
            bundle.path.rename(displaced)
            replacement.rename(bundle.path)
        return snapshot

    monkeypatch.setattr("narumi.bundle.session.utc_now_iso", lambda: fixed_time)
    monkeypatch.setattr(writer, "_read_manifest_at", swap_after_cas)
    with pytest.raises(ConfigurationConflictError) as failure:
        bundle.save()
    assert failure.value.details["outcome_unknown"] is True
    assert not bundle.abspath("tracks/parent-only.raw").exists()
    assert (displaced / "tracks/parent-only.raw").read_bytes() == b"parent-only"


def test_run_stage_rejects_stale_generation_before_producer_runs(tmp_path: Path):
    current = Bundle.create(tmp_path, meeting_name="original", meeting_id=MEETING_A)
    stale = Bundle.open(current.path)
    current.manifest.meeting_name = "newer"
    current.save()
    called = False

    def producer(_path: Path) -> None:
        nonlocal called
        called = True

    with pytest.raises(ConfigurationConflictError):
        stale.run_stage(
            "demo/stale",
            inputs={},
            params={},
            producer=("demo", "1"),
            output="preprocess/stale.txt",
            fn=producer,
        )
    assert not called
    assert not stale.abspath("preprocess/stale.txt").exists()


def test_two_processes_cannot_both_create_same_bundle(tmp_path: Path):
    gate = tmp_path / "gate"
    program = """
import sys, time
from pathlib import Path
from narumi.bundle import Bundle
from narumi.errors import InvalidArgumentError
root, meeting_id, gate = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
print("ready", flush=True)
while not gate.exists():
    time.sleep(0.005)
try:
    Bundle.create(root, meeting_name="race", meeting_id=meeting_id)
except InvalidArgumentError:
    print("exists", flush=True)
else:
    print("created", flush=True)
"""
    children = [
        subprocess.Popen(
            [sys.executable, "-c", program, str(tmp_path), MEETING_A, str(gate)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    for child in children:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"
    gate.write_text("go", encoding="utf-8")
    outcomes = []
    for child in children:
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, stderr
        outcomes.append(stdout.strip())
    assert sorted(outcomes) == ["created", "exists"]
    assert Bundle.find(tmp_path, MEETING_A).manifest.meeting_name == "race"


def test_known_create_failure_removes_only_its_empty_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    original_fsync = writer.os.fsync

    def fail_manifest_fsync(descriptor: int) -> None:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("synthetic manifest fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(writer.os, "fsync", fail_manifest_fsync)
    with pytest.raises(NarumiError) as failure:
        Bundle.create(tmp_path, meeting_name="failed", meeting_id=MEETING_A)
    assert failure.value.code == ErrorCode.INTERNAL
    assert not (tmp_path / MEETING_A).exists()
    assert (tmp_path / ".manifest-locks" / f"{MEETING_A}.lock").exists()


def test_parent_fsync_failure_after_create_is_outcome_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    original_fsync = writer.os.fsync
    directory_fsyncs = 0

    def fail_final_parent_fsync(descriptor: int) -> None:
        nonlocal directory_fsyncs
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            directory_fsyncs += 1
            if directory_fsyncs == 3:
                raise OSError("synthetic parent fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(writer.os, "fsync", fail_final_parent_fsync)
    with pytest.raises(ConfigurationConflictError) as failure:
        Bundle.create(tmp_path, meeting_name="unknown", meeting_id=MEETING_A)
    assert failure.value.details == {
        "reason": "bundle_create_outcome_unknown",
        "outcome_unknown": True,
    }
    assert Bundle.find(tmp_path, MEETING_A).manifest.meeting_name == "unknown"


def test_unique_temporary_and_file_replace_directory_fsync_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    bundle = Bundle.create(tmp_path, meeting_name="original", meeting_id=MEETING_A)
    operations: list[str] = []
    temporaries: list[str] = []
    original_fsync, original_replace = writer.os.fsync, writer.os.replace

    def fsync(descriptor: int) -> None:
        operations.append("directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file")
        original_fsync(descriptor)

    def replace(source: str, target: str, **kwargs: object) -> None:
        operations.append("replace")
        temporaries.append(source)
        original_replace(source, target, **kwargs)

    monkeypatch.setattr(writer.os, "fsync", fsync)
    monkeypatch.setattr(writer.os, "replace", replace)
    for name in ("first", "second"):
        operations.clear()
        bundle.manifest.meeting_name = name
        bundle.save()
        assert operations == ["file", "replace", "directory"]
    assert len(set(temporaries)) == 2
    assert all(name.startswith(".manifest.json.") and name.endswith(".tmp") for name in temporaries)
    assert not list(bundle.path.glob(".manifest.json.*.tmp"))


def test_uuid_collision_does_not_delete_another_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    bundle = Bundle.create(tmp_path, meeting_name="original", meeting_id=MEETING_A)
    temporary = bundle.path / f".manifest.json.{'0' * 32}.tmp"
    temporary.write_text("leave intact", encoding="utf-8")
    temporary.chmod(0o600)
    monkeypatch.setattr(writer.uuid, "uuid4", lambda: type("Token", (), {"hex": "0" * 32})())
    bundle.manifest.meeting_name = "not-published"
    with pytest.raises(NarumiError) as failure:
        bundle.save()
    assert failure.value.code == ErrorCode.INTERNAL
    assert temporary.read_text(encoding="utf-8") == "leave intact"
    assert Bundle.open(bundle.path).manifest.meeting_name == "original"


def test_temporary_fsync_failure_preserves_previous_manifest_and_hides_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    bundle = Bundle.create(tmp_path, meeting_name="original", meeting_id=MEETING_A)
    before = bundle.manifest_path.read_bytes()
    bundle.manifest.meeting_name = "not-published"
    original_fsync = writer.os.fsync

    def fail_file_fsync(descriptor: int) -> None:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("secret-path-fragment")
        original_fsync(descriptor)

    monkeypatch.setattr(writer.os, "fsync", fail_file_fsync)
    with pytest.raises(NarumiError) as failure:
        bundle.save()
    assert failure.value.code == ErrorCode.INTERNAL
    assert "secret-path-fragment" not in str(failure.value)
    assert bundle.manifest_path.read_bytes() == before
    assert not list(bundle.path.glob(".manifest.json.*.tmp"))


def test_run_stage_rolls_back_record_after_known_save_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    bundle = Bundle.create(tmp_path, meeting_name="original", meeting_id=MEETING_A)
    calls = 0
    original_fsync = writer.os.fsync
    fail_once = True

    def fsync(descriptor: int) -> None:
        nonlocal fail_once
        if fail_once and stat.S_ISREG(os.fstat(descriptor).st_mode):
            fail_once = False
            raise OSError("synthetic temporary failure")
        original_fsync(descriptor)

    def produce(path: Path) -> None:
        nonlocal calls
        calls += 1
        path.write_text(f"attempt {calls}", encoding="utf-8")

    monkeypatch.setattr(writer.os, "fsync", fsync)
    arguments = {
        "key": "demo/retry",
        "inputs": {},
        "params": {},
        "producer": ("demo", "1"),
        "output": "preprocess/retry.txt",
        "fn": produce,
    }
    with pytest.raises(NarumiError):
        bundle.run_stage(**arguments)
    assert bundle.artifact("demo/retry") is None
    result = bundle.run_stage(**arguments)
    assert not result.skipped and calls == 2
    assert Bundle.open(bundle.path).artifact("demo/retry") is not None


def test_directory_fsync_failure_is_unknown_and_requires_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    bundle = Bundle.create(tmp_path, meeting_name="original", meeting_id=MEETING_A)
    bundle.manifest.meeting_name = "published"
    original_fsync = writer.os.fsync

    def fail_directory_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("secret-path-fragment")
        original_fsync(descriptor)

    monkeypatch.setattr(writer.os, "fsync", fail_directory_fsync)
    with pytest.raises(ConfigurationConflictError) as failure:
        bundle.save()
    assert failure.value.details == {
        "reason": "manifest_save_outcome_unknown",
        "outcome_unknown": True,
    }
    assert "secret-path-fragment" not in str(failure.value)
    assert Bundle.open(bundle.path).manifest.meeting_name == "published"
    with pytest.raises(ConfigurationConflictError) as retry:
        bundle.save()
    assert retry.value.details == {
        "reason": "manifest_save_outcome_unknown",
        "outcome_unknown": True,
    }
    assert not list(bundle.path.glob(".manifest.json.*.tmp"))


def test_replace_failure_blocks_retry_even_when_old_manifest_remains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    bundle = Bundle.create(tmp_path, meeting_name="original", meeting_id=MEETING_A)
    before = bundle.manifest_path.read_bytes()
    bundle.manifest.meeting_name = "unknown"

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("secret-path-fragment")

    monkeypatch.setattr(writer.os, "replace", fail_replace)
    for _ in range(2):
        with pytest.raises(ConfigurationConflictError) as failure:
            bundle.save()
        assert failure.value.details == {
            "reason": "manifest_save_outcome_unknown",
            "outcome_unknown": True,
        }
        assert "secret-path-fragment" not in str(failure.value)
    assert bundle.manifest_path.read_bytes() == before
    assert not list(bundle.path.glob(".manifest.json.*.tmp"))


def test_only_one_thread_can_create_with_expected_absent(tmp_path: Path):
    def create() -> str:
        try:
            Bundle.create(tmp_path, meeting_name="race", meeting_id=MEETING_A)
        except InvalidArgumentError:
            return "exists"
        return "created"

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(lambda _: create(), range(2))) == ["created", "exists"]


def test_external_flock_contention_has_bounded_timeout(tmp_path: Path):
    lock_directory = tmp_path / ".manifest-locks"
    lock_directory.mkdir(mode=0o700)
    lock_path = lock_directory / f"{MEETING_A}.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(BusyError):
            with manifest_writer_lock(tmp_path, MEETING_A, timeout=0):
                pass
    finally:
        os.close(descriptor)
