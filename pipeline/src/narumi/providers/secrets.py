"""Keychain credentials via a dedicated native helper and anonymous pipes only."""

from __future__ import annotations

import json
import os
import selectors
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Protocol, runtime_checkable

from narumi.errors import ErrorCode, NarumiError
from narumi.providers._acl import ensure_no_extended_allow_acl

KEYCHAIN_SERVICE = "jp.btajp.narumi.secrets.v1"
_HELPER_NAME = "narumi-keychain"
_MAX_MESSAGE_BYTES = 128 * 1024
_HELPER_TIMEOUT = 30


class SecretStoreError(NarumiError):
    """A deliberately non-diagnostic error: helper details can contain credentials."""

    def __init__(self) -> None:
        super().__init__(
            "Provider credentials could not be accessed securely", code=ErrorCode.INTERNAL
        )


@runtime_checkable
class SecretStore(Protocol):
    def get(self, account: str) -> str | None: ...

    def set(self, account: str, value: str) -> None: ...

    def delete(self, account: str) -> None: ...


def _trusted_directory_tree(root: Path, leaf: Path) -> bool:
    """Validate a known launch/source root and its descendants, not arbitrary paths."""
    try:
        relative = leaf.relative_to(root)
        current = root
        for component in (None, *relative.parts):
            if component is not None:
                current = current / component
            descriptor = os.open(
                current, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            )
            try:
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISDIR(metadata.st_mode)
                    or metadata.st_uid not in (0, os.geteuid())
                    or metadata.st_mode & 0o022
                ):
                    return False
                ensure_no_extended_allow_acl(descriptor)
            finally:
                os.close(descriptor)
        return True
    except (OSError, ValueError):
        return False


def _bundle_helper(bundle: Path) -> Path | None:
    directory = bundle / "Contents" / "MacOS"
    return directory / _HELPER_NAME if _trusted_directory_tree(bundle, directory) else None


def _helper_candidates() -> list[Path]:
    """Use a loaded checkout or the owned launch's bundle, never PATH or the cwd."""
    source = Path(__file__).resolve()
    candidates: list[Path] = []
    for location in (source, Path(sys.executable).resolve()):
        for ancestor in location.parents:
            if ancestor.name == "Contents" and ancestor.parent.suffix == ".app":
                if (helper := _bundle_helper(ancestor.parent)) is not None:
                    candidates.append(helper)
    # Bundled Python lives in the data-root venv. The owned launcher already pins
    # its contracts to this same bundle; an arbitrary helper hint is not an anchor.
    contracts = Path(os.environ.get("NARUMI_CONTRACTS_DIR", "."))
    if contracts.is_absolute() and len(contracts.parents) >= 4 and ".." not in contracts.parts:
        bundle = contracts.parents[3]
        expected = bundle / "Contents" / "Resources" / "runtime" / "contracts"
        if (
            contracts == expected
            and bundle.suffix == ".app"
            and _trusted_directory_tree(bundle, contracts)
            and (contracts / "manifest.json").is_file()
            and not (contracts / "manifest.json").is_symlink()
            and (helper := _bundle_helper(bundle)) is not None
        ):
            candidates.append(helper)
    if len(source.parents) >= 5:
        repository = source.parents[4]
        build = repository / "app" / ".build"
        if (repository / "app" / "Package.swift").is_file() and (
            repository / "contracts" / "manifest.json"
        ).is_file():
            # A repo-mode app uses its fixed dist bundle without switching the
            # server's contracts to bundled copies. Do not infer arbitrary .apps.
            bundle = repository / "dist" / "narumi.app"
            if _trusted_directory_tree(repository, bundle):
                if (helper := _bundle_helper(bundle)) is not None:
                    candidates.append(helper)
            if _trusted_directory_tree(repository, build):
                for configuration in ("release", "debug"):
                    variant = build / configuration
                    try:
                        resolved = variant.resolve(strict=True)
                    except (OSError, RuntimeError):
                        continue
                    # Swift's release/debug directory links may stay inside .build.
                    if _trusted_directory_tree(build, resolved):
                        candidates.append(variant / _HELPER_NAME)
    return candidates


def _available_helper(candidate: Path) -> Path | None:
    try:
        descriptor = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid not in (0, os.geteuid())
                or metadata.st_mode & 0o022
            ):
                return None
            ensure_no_extended_allow_acl(descriptor)
            path = candidate.resolve(strict=True)
            if _trusted_directory_tree(path.parent, path.parent) and os.access(path, os.X_OK):
                return path
        finally:
            os.close(descriptor)
    except (OSError, RuntimeError):
        pass
    return None


def _run_helper(helper: Path, request: bytes) -> subprocess.CompletedProcess[bytes]:
    """Bound both anonymous-pipe output and elapsed time, including blocked stdin."""
    arguments = [str(helper)]
    process = subprocess.Popen(
        arguments,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={"LANG": "C", "LC_ALL": "C"},
        bufsize=0,
    )
    deadline = time.monotonic() + _HELPER_TIMEOUT
    output = bytearray()
    written = 0
    try:
        if process.stdin is None or process.stdout is None:
            raise ValueError("credential helper has no private pipes")
        with selectors.DefaultSelector() as pending:
            for stream, event in (
                (process.stdin, selectors.EVENT_WRITE),
                (process.stdout, selectors.EVENT_READ),
            ):
                os.set_blocking(stream.fileno(), False)
                pending.register(stream, event)
            while pending.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(arguments, _HELPER_TIMEOUT)
                for key, _ in pending.select(remaining):
                    stream = key.fileobj
                    try:
                        if stream is process.stdin:
                            written += os.write(key.fd, request[written:])
                            if written == len(request):
                                pending.unregister(stream)
                                stream.close()
                        else:
                            chunk = os.read(key.fd, min(8192, _MAX_MESSAGE_BYTES - len(output) + 1))
                            if not chunk:
                                pending.unregister(stream)
                                stream.close()
                            else:
                                output.extend(chunk)
                                if len(output) > _MAX_MESSAGE_BYTES:
                                    raise ValueError("credential helper output is too large")
                    except BlockingIOError:
                        continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(arguments, _HELPER_TIMEOUT)
        returncode = process.wait(timeout=remaining)
        return subprocess.CompletedProcess(arguments, returncode, bytes(output))
    finally:
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def _response_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    response: dict[str, object] = {}
    for key, value in pairs:
        if key in response:
            raise ValueError("duplicate credential helper response field")
        response[key] = value
    return response


class KeychainSecretStore:
    """No initialization I/O and no fallback to plaintext or global installers.

    The bundled helper fixes the Keychain service name; callers can choose only an
    account. Parent environment variables are not forwarded to the native helper.
    The helper executable may be explicitly injected by the owned app runtime.
    """

    def __init__(self, helper_path: Path | None = None) -> None:
        self._helper_path = Path(helper_path) if helper_path is not None else None

    def _helper(self) -> Path:
        candidates = [self._helper_path] if self._helper_path is not None else _helper_candidates()
        hint = os.environ.get("NARUMI_KEYCHAIN_HELPER")
        if self._helper_path is None and hint is not None:
            requested = Path(hint)
            if not requested.is_absolute() or requested not in candidates:
                raise SecretStoreError()
            candidates = [requested]
        for candidate in candidates:
            if (path := _available_helper(candidate)) is not None:
                return path
        raise SecretStoreError()

    def _request(self, operation: str, account: str, value: str | None = None) -> str | None:
        try:
            if not isinstance(account, str) or not account or "\x00" in account:
                raise ValueError("invalid account")
            payload = {"operation": operation, "account": account}
            if operation == "set":
                if not isinstance(value, str):
                    raise ValueError("invalid credential")
                payload["value"] = value
            request = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if len(request) > _MAX_MESSAGE_BYTES:
                raise ValueError("credential request is too large")
            result = _run_helper(self._helper(), request)
            if result.returncode != 0 or len(result.stdout) > _MAX_MESSAGE_BYTES:
                raise ValueError("credential helper failed")
            response = json.loads(result.stdout, object_pairs_hook=_response_object)
            if (
                not isinstance(response, dict)
                or set(response) != ({"ok", "value"} if operation == "get" else {"ok"})
                or response.get("ok") is not True
                or (response.get("value") is not None and not isinstance(response["value"], str))
            ):
                raise ValueError("credential helper returned an invalid response")
            return response.get("value")
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            raise SecretStoreError() from None

    def get(self, account: str) -> str | None:
        return self._request("get", account)

    def set(self, account: str, value: str) -> None:
        self._request("set", account, value)

    def delete(self, account: str) -> None:
        self._request("delete", account)
