"""One short-lived private App Server session for a saved connection."""

from __future__ import annotations

import os
import re
import shutil
import stat
import uuid
from pathlib import Path
from typing import Any

from narumi import __version__
from narumi.errors import (
    AuthenticationRequiredError,
    CancelledError,
    EngineUnavailableError,
    InvalidArgumentError,
)
from narumi.providers._io import _open_directory, _open_regular
from narumi.providers.codex import _policy
from narumi.providers.codex._rpc import PROCESS_CLEANUP_REASON, StdioRPC, unavailable
from narumi.providers.codex._runtime import CodexRuntime, private_environment, write_private_json

_AUTH_TEMPORARY_NAME = re.compile(r"\.auth\.[0-9a-f]{32}\.tmp")


def connection_directory(root: Path, connection_id: str) -> Path:
    if not isinstance(connection_id, str) or not re.fullmatch(
        r"conn-[0-9a-f]{12,32}", connection_id
    ):
        raise InvalidArgumentError("Codex connection identifier is invalid")
    return root / "providers/codex-connections" / connection_id


class CodexSession:
    def __init__(
        self,
        runtime: CodexRuntime,
        connection_id: str,
        operation: Any,
        *,
        model: dict[str, Any] | None = None,
    ) -> None:
        self.runtime = runtime
        self.operation = operation
        self.connection = connection_directory(runtime.root, connection_id)
        self.credential_directory = self.connection / "state"
        self.run_directory = self.connection / "runs" / uuid.uuid4().hex
        self.codex_home = self.run_directory / "state"
        self.home = self.run_directory / "home"
        self.cwd = self.run_directory / "work"
        self.catalog: Path | None = None
        self.rpc: StdioRPC | None = None
        self._model = model
        self._authenticated = False
        self.generation_attempted = False

    def __enter__(self) -> CodexSession:
        try:
            _policy.host_preflight()
            executable = self.runtime.require_prepared()
            # The backend admits only one operation per connection. At this boundary,
            # any temporary or run belongs to a crashed process. Reclaim only exact,
            # verified names while the connection operation lease is held.
            _clear_credential_temporaries(self.credential_directory, self.runtime.root)
            _clear_orphan_runs(self.connection / "runs", self.runtime.root)
            for path in (
                self.home,
                self.codex_home,
                self.cwd,
                self.run_directory / "tmp",
                self.home / "config",
                self.home / "cache",
                self.home / "data",
            ):
                directory = _open_directory(path, trusted_root=self.runtime.root)
                os.close(directory)
            _copy_credentials(self.credential_directory, self.codex_home, self.runtime.root)
            if self._model is not None:
                self.catalog = self.run_directory / "models.json"
                write_private_json(
                    self.run_directory,
                    self.runtime.root,
                    self.catalog.name,
                    _policy.static_catalog(self._model),
                )
            env = private_environment(self.home, self.codex_home, self.run_directory / "tmp")
            self.rpc = StdioRPC(
                _policy.command(executable, catalog=self.catalog),
                env=env,
                cwd=self.cwd,
                should_cancel=self.operation.should_cancel,
            )
            self.operation.attach(self.rpc)
            initialized = self.rpc.call(
                "initialize",
                {
                    "clientInfo": {"name": "narumi", "title": "narumi", "version": __version__},
                    "capabilities": {"experimentalApi": True},
                },
            )
            if initialized.get("codexHome") != str(self.codex_home):
                raise unavailable("codex_home_mismatch")
            self.rpc.notify("initialized")
            self.verify_configuration()
            return self
        except BaseException as error:
            if (
                isinstance(error, EngineUnavailableError)
                and error.details.get("reason") == PROCESS_CLEANUP_REASON
            ):
                self.operation.mark_cleanup_unverified()
            try:
                self.close()
            except BaseException:
                pass
            raise

    def __exit__(self, exception_type: Any, *_: Any) -> None:
        try:
            self.close(persist=exception_type is None)
        except BaseException as cleanup_error:
            if exception_type is not None:
                # Preserve the primary failure/cancellation and its outcome flag.
                return
            if self.generation_attempted and isinstance(cleanup_error, Exception):
                raise unavailable("codex_generation_outcome_unknown") from None
            raise

    def call(self, method: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        if self.rpc is None:
            raise unavailable("codex_process_closed")
        return self.rpc.call(method, params, **kwargs)

    def verify_configuration(self) -> None:
        _policy.host_preflight()
        config = self.call("config/read", {"includeLayers": True, "cwd": str(self.cwd)})
        _policy.verify_configuration(config, self.codex_home, catalog=self.catalog)
        requirements = self.call("configRequirements/read", {})
        if "requirements" not in requirements or requirements["requirements"] is not None:
            raise unavailable("codex_managed_requirements_present")

    def require_chatgpt(self) -> None:
        result = self.call("account/read", {"refreshToken": True})
        account = result.get("account")
        if (
            result.get("requiresOpenaiAuth") is not True
            or not isinstance(account, dict)
            or account.get("type") != "chatgpt"
        ):
            raise AuthenticationRequiredError(
                "Sign in to ChatGPT for this Codex connection",
                details={"reason": "codex_chatgpt_authentication_required"},
            )
        self._authenticated = True

    def verify_empty_capabilities(self, thread_id: str) -> None:
        mcp = self.call("mcpServerStatus/list", {"threadId": thread_id, "limit": 100})
        if mcp.get("data") != [] or mcp.get("nextCursor") is not None:
            raise unavailable("codex_mcp_isolation_unverified")
        skills = self.call("skills/list", {"cwds": [str(self.cwd)], "forceReload": True})
        entries = skills.get("data")
        if not isinstance(entries, list) or len(entries) != 1:
            raise unavailable("codex_skill_isolation_unverified")
        entry = entries[0]
        if not isinstance(entry, dict) or (
            entry.get("cwd") != str(self.cwd)
            or entry.get("skills") != []
            or entry.get("errors") != []
        ):
            raise unavailable("codex_skill_isolation_unverified")

    def close(self, *, persist: bool = False) -> None:
        failure: BaseException | None = None
        cleanup_unverified = False
        credential_contents: bytes | None = None
        if self.rpc is not None:
            rpc, self.rpc = self.rpc, None
            try:
                rpc.close()
            except BaseException as error:
                failure = error
                cleanup_unverified = True
            finally:
                try:
                    self.operation.detach(rpc)
                except BaseException as detach_error:
                    if failure is None:
                        failure = detach_error
        try:
            if persist and self._authenticated and not self.operation.should_cancel():
                if failure is None:
                    credential_contents = _read_credentials(self.codex_home, self.runtime.root)
                    if credential_contents is None:
                        raise unavailable("codex_credentials_missing")
        except BaseException as error:
            if failure is None:
                failure = error
        try:
            _remove_session_run(self.run_directory)
        except BaseException as cleanup_error:
            cleanup_unverified = True
            if failure is None:
                failure = cleanup_error
        if (
            failure is None
            and credential_contents is not None
            and not self.operation.should_cancel()
        ):
            committed: bool | None = None
            try:
                committed = self.operation.install_credentials(
                    lambda: _install_credentials(
                        credential_contents,
                        self.credential_directory,
                        self.runtime.root,
                        report_unknown_install=True,
                    )
                )
                if not committed:
                    if self.operation.kind == "auth":
                        clear_credentials(self.runtime.root, self.operation.connection_id)
                    failure = CancelledError("Codex operation was cancelled")
            except BaseException as install_error:
                failure = install_error
                cleanup_unverified = (
                    isinstance(install_error, EngineUnavailableError)
                    and install_error.details.get("reason")
                    in {
                        "codex_credential_cleanup_unverified",
                        "codex_credential_install_outcome_unknown",
                    }
                ) or committed is False
        credential_contents = None
        if failure is not None:
            if cleanup_unverified:
                self.operation.mark_cleanup_unverified()
            raise failure


def _remove_session_run(run_directory: Path) -> None:
    try:
        metadata = os.lstat(run_directory)
    except FileNotFoundError:
        return
    except OSError:
        raise unavailable("codex_session_cleanup_unverified") from None
    if not stat.S_ISDIR(metadata.st_mode):
        raise unavailable("codex_session_cleanup_unverified")
    try:
        shutil.rmtree(run_directory)
    except FileNotFoundError:
        pass
    except Exception:
        raise unavailable("codex_session_cleanup_unverified") from None
    try:
        os.lstat(run_directory)
    except FileNotFoundError:
        return
    except OSError:
        raise unavailable("codex_session_cleanup_unverified") from None
    raise unavailable("codex_session_cleanup_unverified")


def _copy_credentials(
    source: Path,
    destination: Path,
    root: Path,
    *,
    report_unknown_install: bool = False,
) -> bool:
    """Copy only the official file-store credential as opaque private bytes.

    The persistent connection state is never CODEX_HOME for a child. Installation
    IDs, databases, environment definitions, skills and cached configuration are
    therefore neither read nor copied into a new runtime session.
    """
    contents = _read_credentials(source, root)
    if contents is None:
        return False
    try:
        _install_credentials(
            contents,
            destination,
            root,
            report_unknown_install=report_unknown_install,
        )
    finally:
        contents = None
    return True


def _read_credentials(source: Path, root: Path) -> bytes | None:
    try:
        os.lstat(source)
    except FileNotFoundError:
        return None
    source_directory = _open_directory(source, trusted_root=root)
    try:
        try:
            descriptor = _open_regular(source_directory, "auth.json", os.O_RDONLY)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "rb") as stream:
            contents = stream.read(128 * 1024 + 1)
        if not contents or len(contents) > 128 * 1024:
            raise unavailable("codex_credential_file_rejected")
    finally:
        os.close(source_directory)
    return contents


def _install_credentials(
    contents: bytes,
    destination: Path,
    root: Path,
    *,
    report_unknown_install: bool = False,
) -> None:
    directory = _open_directory(destination, trusted_root=root)
    temporary = f".auth.{uuid.uuid4().hex}.tmp"
    installed = False
    try:
        try:
            previous = _open_regular(directory, "auth.json", os.O_RDONLY)
        except FileNotFoundError:
            pass
        else:
            os.close(previous)
        target = _open_regular(directory, temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(target, "wb") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, "auth.json", src_dir_fd=directory, dst_dir_fd=directory)
        installed = True
        os.fsync(directory)
    except Exception:
        if installed and report_unknown_install:
            raise unavailable("codex_credential_install_outcome_unknown") from None
        raise
    finally:
        cleanup_error: BaseException | None = None
        try:
            _remove_credential_temporary(directory, temporary)
        except BaseException as error:
            cleanup_error = error
        try:
            os.close(directory)
        except OSError:
            if cleanup_error is None:
                cleanup_error = unavailable("codex_credential_cleanup_unverified")
        if cleanup_error is not None:
            raise cleanup_error


def _remove_credential_temporary(directory: int, temporary: str) -> None:
    """Remove and verify one private credential temporary before returning."""
    cleanup_failed = False
    removed = False
    try:
        os.unlink(temporary, dir_fd=directory)
        removed = True
    except FileNotFoundError:
        pass
    except OSError:
        cleanup_failed = True
    if removed:
        try:
            os.fsync(directory)
        except OSError:
            cleanup_failed = True
    try:
        os.stat(temporary, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        pass
    except OSError:
        cleanup_failed = True
    else:
        cleanup_failed = True
    if cleanup_failed:
        raise unavailable("codex_credential_cleanup_unverified") from None


def clear_credentials(root: Path, connection_id: str) -> None:
    """Delete only this connection's official file-store credential, without startup."""
    connection = connection_directory(root, connection_id)
    state = connection / "state"
    _clear_credential_temporaries(state, root)
    try:
        os.lstat(state)
    except FileNotFoundError:
        pass
    else:
        directory = _open_directory(state, trusted_root=root)
        try:
            try:
                credential = _open_regular(directory, "auth.json", os.O_RDONLY)
            except FileNotFoundError:
                pass
            else:
                os.close(credential)
                os.unlink("auth.json", dir_fd=directory)
                os.fsync(directory)
        finally:
            os.close(directory)
    _clear_orphan_runs(connection / "runs", root)
    _require_credentials_cleared(connection, root)


def recover_connection_artifacts(root: Path, connection_id: str) -> None:
    """Reclaim crash-only copies for one registered connection under the server lease."""
    connection = connection_directory(root, connection_id)
    _clear_credential_temporaries(connection / "state", root)
    _clear_orphan_runs(connection / "runs", root)


def _require_credentials_cleared(connection: Path, root: Path) -> None:
    """Verify that neither persistent nor short-lived credential copies remain."""
    state = connection / "state"
    try:
        os.lstat(state)
    except FileNotFoundError:
        pass
    else:
        directory = _open_directory(state, trusted_root=root)
        try:
            if any(_AUTH_TEMPORARY_NAME.fullmatch(name) for name in os.listdir(directory)):
                raise unavailable("codex_credential_cleanup_unverified")
            try:
                credential = _open_regular(directory, "auth.json", os.O_RDONLY)
            except FileNotFoundError:
                pass
            else:
                os.close(credential)
                raise unavailable("codex_credential_cleanup_unverified")
        finally:
            os.close(directory)
    runs = connection / "runs"
    try:
        os.lstat(runs)
    except FileNotFoundError:
        return
    directory = _open_directory(runs, trusted_root=root)
    try:
        if os.listdir(directory):
            raise unavailable("codex_session_cleanup_unverified")
    finally:
        os.close(directory)


def _clear_credential_temporaries(state: Path, root: Path) -> None:
    """Reclaim exact interrupted credential writes under the connection lease."""
    try:
        os.lstat(state)
    except FileNotFoundError:
        return
    directory = _open_directory(state, trusted_root=root)
    removed = False
    try:
        try:
            names = os.listdir(directory)
        except OSError:
            raise unavailable("codex_credential_cleanup_unverified") from None
        for name in names:
            if _AUTH_TEMPORARY_NAME.fullmatch(name) is None:
                continue
            try:
                descriptor = _open_regular(directory, name, os.O_RDONLY)
            except OSError:
                raise unavailable("codex_credential_cleanup_rejected") from None
            try:
                opened = os.fstat(descriptor)
                current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if not os.path.samestat(opened, current):
                    raise unavailable("codex_credential_cleanup_rejected")
                os.unlink(name, dir_fd=directory)
                removed = True
            except EngineUnavailableError:
                raise
            except OSError:
                raise unavailable("codex_credential_cleanup_unverified") from None
            finally:
                os.close(descriptor)
        if removed:
            try:
                os.fsync(directory)
            except OSError:
                raise unavailable("codex_credential_cleanup_unverified") from None
        try:
            remaining = os.listdir(directory)
        except OSError:
            raise unavailable("codex_credential_cleanup_unverified") from None
        if any(_AUTH_TEMPORARY_NAME.fullmatch(name) for name in remaining):
            raise unavailable("codex_credential_cleanup_unverified")
    finally:
        os.close(directory)


def _clear_orphan_runs(runs: Path, root: Path) -> None:
    """Remove this idle connection's crash remnants without following child links."""
    try:
        os.lstat(runs)
    except FileNotFoundError:
        return
    directory = _open_directory(runs, trusted_root=root)
    try:
        for name in os.listdir(directory):
            metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if metadata.st_uid != os.geteuid():
                raise unavailable("codex_session_cleanup_rejected")
            if stat.S_ISDIR(metadata.st_mode):
                shutil.rmtree(name, dir_fd=directory)
            else:
                os.unlink(name, dir_fd=directory)
        os.fsync(directory)
        if os.listdir(directory):
            raise unavailable("codex_session_cleanup_unverified")
    finally:
        os.close(directory)
