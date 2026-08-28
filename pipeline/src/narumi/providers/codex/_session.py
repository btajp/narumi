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
from narumi.errors import AuthenticationRequiredError, InvalidArgumentError
from narumi.providers._io import _open_directory, _open_regular
from narumi.providers.codex import _policy
from narumi.providers.codex._rpc import StdioRPC, unavailable
from narumi.providers.codex._runtime import CodexRuntime, private_environment, write_private_json


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
        except BaseException:
            try:
                self.close()
            except Exception:
                pass
            raise

    def __exit__(self, exception_type: Any, *_: Any) -> None:
        try:
            self.close(persist=exception_type is None)
        except Exception:
            if exception_type is not None:
                # Preserve the primary failure/cancellation and its outcome flag.
                return
            if self.generation_attempted:
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
        try:
            if self.rpc is not None:
                rpc, self.rpc = self.rpc, None
                try:
                    rpc.close()
                finally:
                    self.operation.detach(rpc)
            if persist and self._authenticated and not self.operation.should_cancel():
                if not _copy_credentials(
                    self.codex_home, self.credential_directory, self.runtime.root
                ):
                    raise unavailable("codex_credentials_missing")
        finally:
            if self.run_directory.is_dir() and not self.run_directory.is_symlink():
                shutil.rmtree(self.run_directory)


def _copy_credentials(source: Path, destination: Path, root: Path) -> bool:
    """Copy only the official file-store credential as opaque private bytes.

    The persistent connection state is never CODEX_HOME for a child. Installation
    IDs, databases, environment definitions, skills and cached configuration are
    therefore neither read nor copied into a new runtime session.
    """
    try:
        os.lstat(source)
    except FileNotFoundError:
        return False
    source_directory = _open_directory(source, trusted_root=root)
    try:
        try:
            descriptor = _open_regular(source_directory, "auth.json", os.O_RDONLY)
        except FileNotFoundError:
            return False
        with os.fdopen(descriptor, "rb") as stream:
            contents = stream.read(128 * 1024 + 1)
        if not contents or len(contents) > 128 * 1024:
            raise unavailable("codex_credential_file_rejected")
    finally:
        os.close(source_directory)
    directory = _open_directory(destination, trusted_root=root)
    temporary = f".auth.{uuid.uuid4().hex}.tmp"
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
        del contents
        os.replace(temporary, "auth.json", src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
        return True
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def clear_credentials(root: Path, connection_id: str) -> None:
    """Delete only this connection's official file-store credential, without startup."""
    connection = connection_directory(root, connection_id)
    state = connection / "state"
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
    finally:
        os.close(directory)
