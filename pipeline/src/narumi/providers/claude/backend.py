"""Connection-scoped facade for one isolated Claude SDK worker per request."""

from __future__ import annotations

import fcntl
import os
import re
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from narumi.errors import BusyError, EngineUnavailableError, ModelUnavailableError, NarumiError
from narumi.providers._acl import ensure_no_extended_allow_acl
from narumi.providers.claude.protocol import (
    PROBE_PROMPT,
    PROBE_SENTINEL,
    PROBE_SYSTEM,
    WorkerRequest,
    WorkerResponse,
    valid_runtime,
    validate_request,
    validate_response,
)
from narumi.providers.claude.runtime import inspect_runtime
from narumi.providers.claude.snapshot import (
    RESOURCE_SHA256_FIELD,
    ExecutionImage,
    ExecutionSnapshot,
    create_execution_image,
)
from narumi.providers.claude.transport import OUTCOME_UNKNOWN, SubprocessWorkerRunner

DEFAULT_TIMEOUT = 600.0
CLOSE_TIMEOUT = 5.0
_LEASE_ENV = "NARUMI_CLAUDE_BACKEND_LEASE_FD"
_LEASE_NAME = ".backend.lock"
_RUN_NAME = re.compile(r"(?:execution-image|conn-[a-f0-9]{12,64})-[a-z0-9_]{8}")
_MAX_RECOVERY_ENTRIES = 8192
_MAX_RECOVERY_DEPTH = 32
_IMMUTABLE_FLAG = getattr(stat, "UF_IMMUTABLE", 0)
_POISONED_LEASES: list[int] = []
_POISONED_LEASES_LOCK = threading.Lock()
_WORKER_BOOTSTRAP = """\
import os
import runpy
import sys
archive = os.environ.get("NARUMI_CLAUDE_SNAPSHOT_ARCHIVE")
dependencies = os.environ.get("NARUMI_CLAUDE_DEPENDENCY_ROOT")
if not archive or not dependencies:
    raise SystemExit(1)
stdlib = tuple(sys.path)
if any("site-packages" in item or "dist-packages" in item for item in stdlib):
    raise SystemExit(1)
sys.path[:] = [archive, *stdlib, dependencies]
runpy.run_module("narumi.providers.claude.worker", run_name="__main__")
"""


@dataclass(frozen=True)
class ClaudeSDKCompletion:
    text: str
    returned_model: str
    usage: dict[str, int]
    runtime_evidence: dict[str, str]


@dataclass(frozen=True)
class ClaudeSDKVerification:
    model_id: str
    usage: dict[str, int]
    runtime_evidence: dict[str, str]


class WorkerRunner(Protocol):
    def __call__(
        self,
        request: WorkerRequest,
        *,
        env: Mapping[str, str],
        cwd: Path,
        should_cancel: Callable[[], bool],
        timeout: float | None,
    ) -> WorkerResponse: ...


class ClaudeSDKBackend:
    def __init__(self, root: Path, *, runner: WorkerRunner | None = None) -> None:
        self.root = Path(os.path.abspath(root))
        # A supplied runner is a test seam. Production creates a request-scoped
        # runner only after its verified archive FD and CLI copy exist.
        self.runner = runner
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._active: set[str] = set()
        self._image_lock = threading.Lock()
        self._image: ExecutionImage | None = None
        self._image_workspace: Path | None = None
        self._poison_reason: str | None = None
        self._runtime_root = self.root / "providers" / "runtime" / "claude-agent-sdk"
        self._runs_root = self._runtime_root / "runs"
        self._lease_descriptor: int | None = None
        try:
            self._lease_descriptor = _acquire_workspace_lease(
                self.root,
                self._runtime_root,
                self._runs_root,
            )
        except BaseException:
            self._poison_reason = "claude_sdk_workspace_unavailable"

    def runtime_evidence(self) -> dict[str, str]:
        self._ensure_usable()
        if self._closed.is_set():
            raise _unavailable("claude_sdk_backend_closed")
        try:
            return inspect_runtime().public()
        except Exception:
            raise _unavailable("claude_sdk_runtime_unverified") from None

    def ensure_workspace_ready(self) -> None:
        self._ensure_usable()
        if self._closed.is_set():
            raise _unavailable("claude_sdk_backend_closed")

    def complete(
        self,
        connection_id: str,
        api_key: str,
        model_id: str,
        prompt: str,
        *,
        system: str | None = None,
        expected_runtime: Mapping[str, str] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        timeout: float | None = None,
    ) -> ClaudeSDKCompletion:
        response = self._run(
            WorkerRequest(connection_id, api_key, model_id, prompt, system),
            expected_runtime=expected_runtime,
            should_cancel=should_cancel,
            timeout=timeout,
        )
        return ClaudeSDKCompletion(
            response.text,
            response.returned_model,
            dict(response.usage),
            dict(response.runtime_evidence),
        )

    def verify_model(
        self,
        connection_id: str,
        api_key: str,
        model_id: str,
        *,
        expected_runtime: Mapping[str, str] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        timeout: float | None = None,
    ) -> ClaudeSDKVerification:
        evidence = self.runtime_evidence() if expected_runtime is None else dict(expected_runtime)
        response = self._run(
            WorkerRequest(connection_id, api_key, model_id, PROBE_PROMPT, PROBE_SYSTEM),
            expected_runtime=evidence,
            should_cancel=should_cancel,
            timeout=timeout,
        )
        if response.text != PROBE_SENTINEL or response.returned_model != model_id:
            raise ModelUnavailableError(
                "Claude Agent SDK model verification did not return the fixed sentinel",
                details={"reason": "claude_sdk_model_probe_failed"},
            )
        return ClaudeSDKVerification(
            model_id, dict(response.usage), dict(response.runtime_evidence)
        )

    def close(self) -> None:
        deadline = time.monotonic() + CLOSE_TIMEOUT
        with self._condition:
            self._closed.set()
            while self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _unknown()
                self._condition.wait(remaining)
        self._close_backend_resources()

    def _close_backend_resources(self) -> None:
        try:
            self._close_execution_image()
        except BaseException:
            self._mark_poisoned("claude_sdk_workspace_cleanup_failed")
            self._release_workspace_lease()
            raise
        self._release_workspace_lease()

    def _close_execution_image(self) -> None:
        primary: BaseException | None = None
        with self._image_lock:
            image, workspace = self._image, self._image_workspace
            self._image = None
            self._image_workspace = None
            if image is not None:
                try:
                    image.close()
                except BaseException as error:
                    primary = error
            if workspace is not None:
                try:
                    _remove_workspace(workspace)
                except BaseException as error:
                    primary = primary or error
        if primary is not None:
            self._mark_poisoned("claude_sdk_workspace_cleanup_failed")
            raise _unknown() from None

    def _run(
        self,
        request: WorkerRequest,
        *,
        expected_runtime: Mapping[str, str] | None,
        should_cancel: Callable[[], bool] | None,
        timeout: float | None,
    ) -> WorkerResponse:
        self._ensure_usable()
        try:
            validate_request(request)
        except ValueError:
            raise _unavailable("claude_sdk_request_rejected") from None
        try:
            requested_runtime = None if expected_runtime is None else dict(expected_runtime)
            if (
                self.runner is None
                and requested_runtime is not None
                and RESOURCE_SHA256_FIELD in requested_runtime
                and valid_runtime(requested_runtime)
            ):
                observed_runtime = {
                    key: value
                    for key, value in requested_runtime.items()
                    if key != RESOURCE_SHA256_FIELD
                }
            else:
                observed_runtime = inspect_runtime().public()
                requested_runtime = requested_runtime or dict(observed_runtime)
            requested_sdk_runtime = {
                key: value
                for key, value in requested_runtime.items()
                if key != RESOURCE_SHA256_FIELD
            }
            if not valid_runtime(requested_runtime) or requested_sdk_runtime != observed_runtime:
                raise ValueError
        except (TypeError, ValueError, RuntimeError):
            raise _unavailable("claude_sdk_runtime_changed") from None
        with self._operation(request.connection_id):
            workspace = self._workspace(request.connection_id)
            snapshot: ExecutionSnapshot | None = None
            runner = self.runner
            selected_runtime = requested_runtime
            environment = _private_environment(workspace)
            if runner is None:
                try:
                    expected_resource = requested_runtime.get(RESOURCE_SHA256_FIELD)
                    image = self._execution_image(observed_runtime, expected_resource)
                    snapshot = image.materialize(workspace)
                    selected_runtime = snapshot.runtime_evidence
                    if expected_resource is not None and selected_runtime != requested_runtime:
                        raise RuntimeError("selected Claude runtime changed")
                    request = replace(request, expected_runtime=selected_runtime)
                    environment.update(snapshot.environment())
                    runner = SubprocessWorkerRunner(
                        (sys.executable, "-I", "-S", "-c", _WORKER_BOOTSTRAP),
                        inherited_fds=(
                            *snapshot.inherited_descriptors,
                            self._required_lease_descriptor(),
                        ),
                        watchdog_held_fds=(self._required_lease_descriptor(),),
                    )
                    environment[_LEASE_ENV] = str(self._required_lease_descriptor())
                except BaseException:
                    if snapshot is not None:
                        try:
                            snapshot.close()
                        except BaseException:
                            self._mark_poisoned("claude_sdk_snapshot_cleanup_failed")
                    try:
                        _remove_workspace(workspace)
                    except BaseException:
                        self._mark_poisoned("claude_sdk_workspace_cleanup_failed")
                    raise _unavailable("claude_sdk_snapshot_unavailable") from None
            else:
                request = replace(request, expected_runtime=selected_runtime)
            response: WorkerResponse | None = None
            primary: BaseException | None = None
            try:
                response = runner(
                    request,
                    env=environment,
                    cwd=workspace / "cwd",
                    should_cancel=lambda: self._cancelled(should_cancel),
                    timeout=timeout,
                )
                validate_response(response)
                if (
                    response.returned_model != request.model_id
                    or response.runtime_evidence != selected_runtime
                    or request.api_key in (response.text + response.returned_model)
                ):
                    raise _unknown()
            except BaseException as error:
                primary = error
            if snapshot is not None:
                try:
                    snapshot.verify_after_execution()
                except BaseException:
                    self._mark_poisoned("claude_sdk_snapshot_cleanup_failed")
                    primary = _unknown()
                try:
                    snapshot.close()
                except BaseException:
                    self._mark_poisoned("claude_sdk_snapshot_cleanup_failed")
                    primary = _unknown()
            try:
                _remove_workspace(workspace)
            except BaseException:
                # Cleanup runs after the worker may already have submitted a
                # billable request. Never expose arbitrary cleanup exceptions
                # or let their type downgrade the delivery state to retryable.
                self._mark_poisoned("claude_sdk_workspace_cleanup_failed")
                primary = _unknown()
            if primary is not None:
                if isinstance(primary, NarumiError):
                    raise primary
                raise _unknown() from None
            assert response is not None
            return response

    def _execution_image(
        self,
        sdk_runtime: dict[str, str],
        expected_resource: str | None,
    ) -> ExecutionImage:
        with self._image_lock:
            if self._closed.is_set():
                raise RuntimeError("Claude backend closed during snapshot creation")
            if self._image is not None:
                expected = dict(sdk_runtime)
                expected[RESOURCE_SHA256_FIELD] = (
                    expected_resource or self._image.runtime_evidence[RESOURCE_SHA256_FIELD]
                )
                if self._image.runtime_evidence != expected:
                    raise RuntimeError("Claude execution image does not match selected runtime")
                return self._image
            workspace = self._workspace("execution-image")
            image: ExecutionImage | None = None
            try:
                image = create_execution_image(
                    workspace,
                    sdk_runtime,
                    expected_resource_sha256=expected_resource,
                )
            except BaseException:
                if image is not None:
                    try:
                        image.close()
                    except BaseException:
                        self._mark_poisoned("claude_sdk_snapshot_cleanup_failed")
                try:
                    _remove_workspace(workspace)
                except BaseException:
                    self._mark_poisoned("claude_sdk_workspace_cleanup_failed")
                raise
            self._image = image
            self._image_workspace = workspace
            return image

    @contextmanager
    def _operation(self, connection_id: str):
        with self._lock:
            if self._poison_reason is not None:
                raise _unavailable(self._poison_reason)
            if self._closed.is_set():
                raise _unavailable("claude_sdk_backend_closed")
            if connection_id in self._active:
                raise BusyError("This Claude Agent SDK connection is already in use")
            self._active.add(connection_id)
        try:
            yield
        finally:
            cleanup = False
            with self._condition:
                cleanup = self._closed.is_set() and self._active == {connection_id}
            cleanup_error: BaseException | None = None
            if cleanup:
                try:
                    self._close_backend_resources()
                except BaseException as error:
                    cleanup_error = error
            with self._condition:
                self._active.discard(connection_id)
                self._condition.notify_all()
            if cleanup_error is not None:
                raise _unknown() from None

    def _workspace(self, connection_id: str) -> Path:
        base = self._runs_root
        workspace: Path | None = None
        try:
            _reject_symlink_components(self.root)
            base.mkdir(parents=True, mode=0o700, exist_ok=True)
            _reject_symlink_components(base)
            base_info = base.stat(follow_symlinks=False)
            if (
                not stat.S_ISDIR(base_info.st_mode)
                or base_info.st_uid != os.geteuid()
                or base.is_symlink()
                or not base.resolve().is_relative_to(self.root.resolve())
            ):
                raise OSError("private workspace root is untrusted")
            os.chmod(base, 0o700, follow_symlinks=False)
            workspace = Path(tempfile.mkdtemp(prefix=f"{connection_id}-", dir=base))
            os.chmod(workspace, 0o700, follow_symlinks=False)
            workspace_info = workspace.stat(follow_symlinks=False)
            if (
                not stat.S_ISDIR(workspace_info.st_mode)
                or workspace_info.st_uid != os.geteuid()
                or stat.S_IMODE(workspace_info.st_mode) != 0o700
            ):
                raise OSError("private workspace is untrusted")
            workspace_descriptor = _open_directory(workspace)
            os.close(workspace_descriptor)
            for name in ("home", "tmp", "config", "secure", "cwd", "xdg-config", "xdg-data"):
                path = workspace / name
                path.mkdir(mode=0o700)
                info = path.stat(follow_symlinks=False)
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o700
                ):
                    raise OSError("private workspace permissions are not isolated")
                path_descriptor = _open_directory(path)
                os.close(path_descriptor)
            if not workspace.resolve().is_relative_to(base.resolve()):
                raise OSError("private workspace escaped its root")
            return workspace
        except OSError:
            if workspace is not None:
                try:
                    _remove_workspace(workspace)
                except BaseException:
                    self._mark_poisoned("claude_sdk_workspace_cleanup_failed")
            raise _unavailable("claude_sdk_workspace_unavailable") from None

    def _ensure_usable(self) -> None:
        with self._lock:
            reason = self._poison_reason
        if reason is not None:
            raise _unavailable(reason)

    def _mark_poisoned(self, reason: str) -> None:
        with self._lock:
            if self._poison_reason is None:
                self._poison_reason = reason

    def _required_lease_descriptor(self) -> int:
        descriptor = self._lease_descriptor
        if descriptor is None:
            raise RuntimeError("Claude backend lease is unavailable")
        return descriptor

    def _release_workspace_lease(self) -> None:
        descriptor = self._lease_descriptor
        if descriptor is None:
            return
        self._lease_descriptor = None
        if self._poison_reason is not None:
            _retain_poisoned_lease(descriptor)
            return
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _cancelled(self, callback: Callable[[], bool] | None) -> bool:
        if self._closed.is_set():
            return True
        if callback is None:
            return False
        try:
            return bool(callback())
        except Exception:
            return True


def _private_environment(workspace: Path) -> dict[str, str]:
    return {
        "HOME": str(workspace / "home"),
        "TMPDIR": str(workspace / "tmp"),
        "TMP": str(workspace / "tmp"),
        "TEMP": str(workspace / "tmp"),
        "CLAUDE_CONFIG_DIR": str(workspace / "config"),
        "CLAUDE_SECURESTORAGE_CONFIG_DIR": str(workspace / "secure"),
        "XDG_CONFIG_HOME": str(workspace / "xdg-config"),
        "XDG_DATA_HOME": str(workspace / "xdg-data"),
        "PATH": "/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK": "1",
        "NO_COLOR": "1",
    }


def _acquire_workspace_lease(root: Path, runtime_root: Path, runs_root: Path) -> int:
    root_descriptor: int | None = None
    runs_descriptor: int | None = None
    lease_descriptor: int | None = None
    lease_locked = False
    try:
        _reject_symlink_components(root)
        runtime_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        _reject_symlink_components(runtime_root)
        runtime_info = runtime_root.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(runtime_info.st_mode)
            or runtime_info.st_uid != os.geteuid()
            or runtime_root.is_symlink()
            or not runtime_root.resolve().is_relative_to(root.resolve())
        ):
            raise OSError("Claude runtime root is untrusted")
        os.chmod(runtime_root, 0o700, follow_symlinks=False)
        root_descriptor = _open_directory(runtime_root)
        lease_flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            lease_flags |= os.O_NOFOLLOW
        lease_descriptor = os.open(
            _LEASE_NAME,
            lease_flags,
            0o600,
            dir_fd=root_descriptor,
        )
        os.fchmod(lease_descriptor, 0o600)
        lease_info = os.fstat(lease_descriptor)
        ensure_no_extended_allow_acl(lease_descriptor)
        path_info = os.stat(_LEASE_NAME, dir_fd=root_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(lease_info.st_mode)
            or lease_info.st_uid != os.geteuid()
            or stat.S_IMODE(lease_info.st_mode) != 0o600
            or lease_info.st_nlink != 1
            or _entry_identity(lease_info) != _entry_identity(path_info)
        ):
            raise OSError("Claude backend lease is untrusted")
        try:
            fcntl.flock(lease_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise OSError("Claude backend lease is already held") from None
        lease_locked = True
        try:
            os.mkdir("runs", 0o700, dir_fd=root_descriptor)
        except FileExistsError:
            pass
        runs_descriptor = _open_directory_at(root_descriptor, "runs")
        runs_info = os.fstat(runs_descriptor)
        if runs_info.st_uid == os.geteuid():
            os.fchmod(runs_descriptor, 0o700)
            runs_info = os.fstat(runs_descriptor)
        if (
            runs_info.st_uid != os.geteuid()
            or stat.S_IMODE(runs_info.st_mode) != 0o700
            or _entry_identity(runs_info)
            != _entry_identity(os.stat("runs", dir_fd=root_descriptor, follow_symlinks=False))
        ):
            raise OSError("Claude runs root is untrusted")
        if Path(os.path.abspath(runs_root)) != runtime_root / "runs":
            raise OSError("Claude runs root escaped its runtime root")
        _sweep_orphaned_runs(runs_descriptor)
        os.fsync(runs_descriptor)
        return lease_descriptor
    except BaseException:
        if lease_descriptor is not None:
            if lease_locked:
                _retain_poisoned_lease(lease_descriptor)
                lease_descriptor = None
            else:
                os.close(lease_descriptor)
        raise
    finally:
        for descriptor in (runs_descriptor, root_descriptor):
            if descriptor is not None:
                os.close(descriptor)


def _sweep_orphaned_runs(runs_descriptor: int) -> None:
    entries = sorted(os.listdir(runs_descriptor))
    if len(entries) > _MAX_RECOVERY_ENTRIES:
        raise OSError("too many orphaned Claude run entries")
    budget = [_MAX_RECOVERY_ENTRIES]
    for name in entries:
        if _RUN_NAME.fullmatch(name) is None:
            raise OSError("unrecognized Claude run entry")
        _remove_directory_at(runs_descriptor, name, budget=budget, depth=0)
    if os.listdir(runs_descriptor):
        raise OSError("Claude run recovery did not reach an empty root")


def _remove_workspace(workspace: Path) -> None:
    if _RUN_NAME.fullmatch(workspace.name) is None:
        raise OSError("invalid private workspace name")
    parent_descriptor = _open_directory(workspace.parent)
    try:
        _remove_directory_at(
            parent_descriptor,
            workspace.name,
            budget=[_MAX_RECOVERY_ENTRIES],
            depth=0,
        )
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)


def _remove_directory_at(
    parent_descriptor: int,
    name: str,
    *,
    budget: list[int],
    depth: int,
) -> None:
    if depth > _MAX_RECOVERY_DEPTH or budget[0] <= 0 or not _safe_entry_name(name):
        raise OSError("unsafe Claude workspace tree")
    budget[0] -= 1
    descriptor = _open_directory_at(parent_descriptor, name)
    try:
        before = os.fstat(descriptor)
        ensure_no_extended_allow_acl(descriptor)
        path_info = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            before.st_uid != os.geteuid()
            or before.st_mode & 0o022
            or (depth == 0 and stat.S_IMODE(before.st_mode) != 0o700)
            or _entry_identity(before) != _entry_identity(path_info)
        ):
            raise OSError("untrusted Claude workspace directory")
        for child in sorted(os.listdir(descriptor)):
            if budget[0] <= 0 or not _safe_entry_name(child):
                raise OSError("unsafe Claude workspace tree")
            child_info = os.stat(child, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(child_info.st_mode):
                _remove_directory_at(descriptor, child, budget=budget, depth=depth + 1)
            elif stat.S_ISREG(child_info.st_mode):
                _remove_regular_at(descriptor, child, budget=budget)
            else:
                raise OSError("unsupported Claude workspace entry")
        _clear_immutable(descriptor)
        os.fsync(descriptor)
        current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if _entry_identity(before) != _entry_identity(current):
            raise OSError("Claude workspace directory changed during cleanup")
    finally:
        os.close(descriptor)
    os.rmdir(name, dir_fd=parent_descriptor)
    os.fsync(parent_descriptor)


def _remove_regular_at(parent_descriptor: int, name: str, *, budget: list[int]) -> None:
    if budget[0] <= 0 or not _safe_entry_name(name):
        raise OSError("unsafe Claude workspace tree")
    budget[0] -= 1
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    try:
        before = os.fstat(descriptor)
        ensure_no_extended_allow_acl(descriptor)
        path_info = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or before.st_mode & 0o022
            or _entry_identity(before) != _entry_identity(path_info)
        ):
            raise OSError("untrusted Claude workspace file")
        _clear_immutable(descriptor)
        current = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if _entry_identity(before) != _entry_identity(current):
            raise OSError("Claude workspace file changed during cleanup")
        os.unlink(name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    finally:
        os.close(descriptor)


def _clear_immutable(descriptor: int) -> None:
    if not _IMMUTABLE_FLAG:
        return
    info = os.fstat(descriptor)
    if not info.st_flags & _IMMUTABLE_FLAG:
        return
    os.chflags(
        f"/dev/fd/{descriptor}",
        info.st_flags & ~_IMMUTABLE_FLAG,
        follow_symlinks=True,
    )
    if os.fstat(descriptor).st_flags & _IMMUTABLE_FLAG:
        raise OSError("Claude workspace immutable flag could not be cleared")


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        os.close(descriptor)
        raise OSError("untrusted Claude workspace directory")
    ensure_no_extended_allow_acl(descriptor)
    return descriptor


def _open_directory_at(parent_descriptor: int, name: str) -> int:
    if not _safe_entry_name(name):
        raise OSError("invalid Claude workspace entry")
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        os.close(descriptor)
        raise OSError("untrusted Claude workspace directory")
    ensure_no_extended_allow_acl(descriptor)
    return descriptor


def _safe_entry_name(name: str) -> bool:
    return bool(name) and name not in {".", ".."} and "/" not in name and "\0" not in name


def _entry_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_mode, value.st_uid


def _retain_poisoned_lease(descriptor: int) -> None:
    with _POISONED_LEASES_LOCK:
        _POISONED_LEASES.append(descriptor)


def _reject_symlink_components(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise OSError("private workspace path contains a symlink")


def _unavailable(reason: str) -> EngineUnavailableError:
    return EngineUnavailableError(
        "Claude Agent SDK could not complete the operation",
        details={"reason": reason, "outcome_unknown": False},
    )


def _unknown() -> EngineUnavailableError:
    return EngineUnavailableError(
        "The provider generation outcome is unknown; explicitly start a new attempt to resend",
        details={"reason": OUTCOME_UNKNOWN, "outcome_unknown": True},
    )
