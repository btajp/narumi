"""No real Keychain or external service is touched by these helper-boundary tests."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import traceback
from pathlib import Path

import pytest
from narumi.providers import secrets
from narumi.providers.secrets import KeychainSecretStore, SecretStore, SecretStoreError

SECRET = "fake-keychain-secret-74826"
ACCOUNT = "provider/test-account"


class PipeProcess:
    """A subprocess double with real anonymous pipes, but no spawned executable."""

    def __init__(self, arguments, respond):
        self.arguments = arguments
        self.returncode = None
        self.done = threading.Event()
        self.killed = threading.Event()
        self.failure = None
        input_read, input_write = os.pipe()
        output_read, output_write = os.pipe()
        self.stdin = os.fdopen(input_write, "wb", buffering=0)
        self.stdout = os.fdopen(output_read, "rb", buffering=0)

        def serve():
            try:
                with os.fdopen(input_read, "rb") as source:
                    request = source.read()
                with os.fdopen(output_write, "wb") as sink:
                    response = respond(request)
                    if response is None:
                        self.killed.wait(5)
                    else:
                        sink.write(response)
            except BrokenPipeError:
                pass
            except BaseException as error:
                self.failure = error
            finally:
                self.returncode = -9 if self.killed.is_set() else 0
                self.done.set()

        threading.Thread(target=serve, daemon=True).start()

    def poll(self):
        return self.returncode if self.done.is_set() else None

    def wait(self, timeout):
        if not self.done.wait(timeout):
            raise subprocess.TimeoutExpired(self.arguments, timeout)
        if self.failure is not None:
            raise self.failure
        return self.returncode

    def kill(self):
        self.killed.set()


@pytest.fixture(autouse=True)
def forbid_real_process(monkeypatch):
    monkeypatch.setattr(
        secrets.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("no real Keychain process")
    )


@pytest.fixture
def helper(tmp_path: Path) -> Path:
    path = tmp_path / "narumi-keychain"
    path.write_text("not executed: subprocess is always replaced")
    path.chmod(0o700)
    return path


def test_initialization_does_not_resolve_helpers_or_access_keychain(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("initialization must not resolve or execute a helper")

    monkeypatch.setattr(secrets, "_helper_candidates", forbidden)
    monkeypatch.setattr(secrets.subprocess, "Popen", forbidden)
    assert isinstance(KeychainSecretStore(), SecretStore)


def test_credentials_only_cross_stdin_and_get_stdout(helper: Path, monkeypatch):
    values = {}
    calls = []
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    monkeypatch.setenv("DYLD_INSERT_LIBRARIES", "/untrusted.dylib")

    def popen(arguments, **kwargs):
        calls.append((arguments, kwargs))
        assert arguments == [str(helper)]
        assert SECRET not in repr(arguments)
        assert SECRET not in repr(kwargs["env"])
        assert "DYLD_INSERT_LIBRARIES" not in kwargs["env"]
        assert kwargs["stdin"] == subprocess.PIPE
        assert kwargs["stdout"] == subprocess.PIPE
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert kwargs["bufsize"] == 0
        return PipeProcess(arguments, respond)

    def respond(request_bytes):
        request = json.loads(request_bytes)
        assert "service" not in request
        operation = request.pop("operation")
        account = request.pop("account")
        if operation == "set":
            values[account] = request.pop("value")
        elif operation == "delete":
            values.pop(account, None)
        assert not request
        response = {"ok": True}
        if operation == "get":
            response["value"] = values.get(account)
        return json.dumps(response).encode()

    monkeypatch.setattr(secrets.subprocess, "Popen", popen)
    store = KeychainSecretStore(helper)
    assert store.get(ACCOUNT) is None
    store.set(ACCOUNT, SECRET)
    assert store.get(ACCOUNT) == SECRET
    store.delete(ACCOUNT)
    assert store.get(ACCOUNT) is None
    store.delete(ACCOUNT)
    assert len(calls) == 6
    assert SECRET not in repr(store)


def test_missing_explicit_helper_does_not_fall_back(tmp_path: Path, helper: Path, monkeypatch):
    def forbidden():
        pytest.fail("an explicit unavailable path must not use another helper")

    monkeypatch.setattr(secrets, "_helper_candidates", forbidden)
    with pytest.raises(SecretStoreError):
        KeychainSecretStore(tmp_path / "missing").set(ACCOUNT, SECRET)


def test_path_executables_are_never_discovered(helper: Path, monkeypatch):
    monkeypatch.setenv("PATH", str(helper.parent))
    monkeypatch.setattr(secrets, "_helper_candidates", lambda: [])
    with pytest.raises(SecretStoreError):
        KeychainSecretStore().get(ACCOUNT)


def test_arbitrary_environment_helper_is_not_a_launch_anchor(helper: Path, monkeypatch):
    monkeypatch.setenv("NARUMI_KEYCHAIN_HELPER", str(helper))
    monkeypatch.delenv("NARUMI_CONTRACTS_DIR", raising=False)
    with pytest.raises(SecretStoreError):
        KeychainSecretStore().get(ACCOUNT)


@pytest.fixture
def bundled_helper(tmp_path: Path, monkeypatch) -> Path:
    bundle = tmp_path / "Renamed Application.app"
    contracts = bundle / "Contents" / "Resources" / "runtime" / "contracts"
    contracts.mkdir(parents=True)
    for directory in (bundle, *contracts.parents[:3], contracts):
        directory.chmod(0o700)
    (contracts / "manifest.json").write_text("{}")
    helper = bundle / "Contents" / "MacOS" / "narumi-keychain"
    helper.parent.mkdir()
    helper.write_text("not executed: subprocess is always replaced")
    helper.chmod(0o700)
    monkeypatch.setenv("NARUMI_CONTRACTS_DIR", str(contracts))
    monkeypatch.setenv("NARUMI_KEYCHAIN_HELPER", str(helper))
    return helper


def test_owned_contracts_anchor_allows_same_bundle_after_rename(bundled_helper: Path, monkeypatch):
    calls = []

    def run(path, request):
        calls.append(path)
        return subprocess.CompletedProcess([str(path)], 0, b'{"ok": true, "value": null}')

    monkeypatch.setattr(secrets, "_run_helper", run)
    assert KeychainSecretStore().get(ACCOUNT) is None
    assert calls == [bundled_helper]


@pytest.fixture
def repository_helper(bundled_helper: Path, tmp_path: Path, monkeypatch) -> Path:
    repository = tmp_path / "checkout"
    repository.mkdir(mode=0o700)
    for relative in (
        "pipeline/src/narumi/providers/secrets.py",
        "app/Package.swift",
        "contracts/manifest.json",
    ):
        marker = repository / relative
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("fixture")
    dist = repository / "dist"
    dist.mkdir(mode=0o700)
    bundled_helper.parents[2].rename(dist / "narumi.app")
    helper = dist / "narumi.app" / "Contents" / "MacOS" / "narumi-keychain"
    monkeypatch.setattr(
        secrets, "__file__", str(repository / "pipeline/src/narumi/providers/secrets.py")
    )
    monkeypatch.delenv("NARUMI_CONTRACTS_DIR", raising=False)
    monkeypatch.setenv("NARUMI_KEYCHAIN_HELPER", str(helper))
    return helper


def test_repo_app_helper_is_accepted_without_bundled_contracts_or_build_tree(
    repository_helper: Path, monkeypatch
):
    calls = []

    def run(path, request):
        calls.append(path)
        return subprocess.CompletedProcess([str(path)], 0, b'{"ok": true, "value": null}')

    monkeypatch.setattr(secrets, "_run_helper", run)
    assert KeychainSecretStore().get(ACCOUNT) is None
    assert calls == [repository_helper]


@pytest.mark.parametrize("change", ["other_app", "symlink", "shared_write"])
def test_repo_dist_helper_keeps_fixed_identity_and_private_path_checks(
    repository_helper: Path, change: str, monkeypatch
):
    bundle = repository_helper.parents[2]
    if change == "other_app":
        other = bundle.with_name("other.app")
        bundle.rename(other)
        monkeypatch.setenv("NARUMI_KEYCHAIN_HELPER", str(other / "Contents/MacOS/narumi-keychain"))
    elif change == "symlink":
        original = bundle.parent.with_name("real-dist")
        bundle.parent.rename(original)
        bundle.parent.symlink_to(original, target_is_directory=True)
    else:
        bundle.parent.chmod(0o777)
    with pytest.raises(SecretStoreError):
        KeychainSecretStore().get(ACCOUNT)


@pytest.mark.parametrize("target", ["helper", "parent", "bundle"])
def test_allow_acl_rejection_prevents_known_bundle_helper_execution(
    bundled_helper: Path, target: str, monkeypatch
):
    path = {
        "helper": bundled_helper,
        "parent": bundled_helper.parent,
        "bundle": bundled_helper.parents[2],
    }[target]
    inode = path.stat().st_ino

    def reject_acl(descriptor):
        if os.fstat(descriptor).st_ino == inode:
            raise OSError(SECRET)

    monkeypatch.setattr(secrets, "ensure_no_extended_allow_acl", reject_acl)
    with pytest.raises(SecretStoreError) as error:
        KeychainSecretStore().get(ACCOUNT)
    assert SECRET not in str(error.value)


@pytest.mark.parametrize("target", ["helper", "parent"])
def test_explicit_helper_keeps_file_and_parent_acl_checks(helper: Path, target: str, monkeypatch):
    inode = (helper if target == "helper" else helper.parent).stat().st_ino

    def reject_acl(descriptor):
        if os.fstat(descriptor).st_ino == inode:
            raise OSError(SECRET)

    monkeypatch.setattr(secrets, "ensure_no_extended_allow_acl", reject_acl)
    with pytest.raises(SecretStoreError) as error:
        KeychainSecretStore(helper).get(ACCOUNT)
    assert SECRET not in str(error.value)


def test_hint_must_match_same_bundle_not_only_helper_basename(
    bundled_helper: Path, helper: Path, monkeypatch
):
    monkeypatch.setenv("NARUMI_KEYCHAIN_HELPER", str(helper))
    with pytest.raises(SecretStoreError):
        KeychainSecretStore().get(ACCOUNT)


@pytest.mark.parametrize("target", ["helper", "directory", "contracts"])
def test_bundle_symlinks_and_shared_write_directories_are_rejected(
    bundled_helper: Path, helper: Path, target: str, monkeypatch
):
    if target == "helper":
        bundled_helper.unlink()
        bundled_helper.symlink_to(helper)
    elif target == "directory":
        bundled_helper.parent.chmod(0o777)
    else:
        contracts = bundled_helper.parent.parent / "Resources" / "runtime" / "contracts"
        original = contracts.with_name("real-contracts")
        contracts.rename(original)
        contracts.symlink_to(original, target_is_directory=True)
    with pytest.raises(SecretStoreError):
        KeychainSecretStore().get(ACCOUNT)


@pytest.mark.parametrize("permissions", [0o600, 0o722, 0o770])
def test_helper_must_be_executable_and_not_writable_by_others(
    helper: Path, permissions: int, monkeypatch
):
    helper.chmod(permissions)
    with pytest.raises(SecretStoreError):
        KeychainSecretStore(helper).get(ACCOUNT)


@pytest.mark.parametrize(
    "response",
    [
        SECRET.encode(),
        b"[]",
        b"null",
        b'{"ok": 1}',
        b'{"ok": false}',
        b'{"ok": true}',
        b'{"ok": false, "ok": true, "value": null}',
        b'{"ok": true, "value": 12}',
        json.dumps({"ok": True, "error": SECRET}).encode(),
        b" " * (128 * 1024 + 1),
    ],
)
def test_invalid_helper_responses_never_leak_details(helper: Path, response: bytes, monkeypatch):
    monkeypatch.setattr(
        secrets,
        "_run_helper",
        lambda path, request: subprocess.CompletedProcess([str(path)], 0, response),
    )
    with pytest.raises(SecretStoreError) as error:
        KeychainSecretStore(helper).get(ACCOUNT)
    assert error.value.code == "internal"
    assert error.value.details == {}
    assert SECRET not in "".join(traceback.format_exception(error.value))
    assert ACCOUNT not in str(error.value)
    assert error.value.__suppress_context__ is True


@pytest.mark.parametrize(
    "failure",
    [
        OSError(SECRET),
        subprocess.TimeoutExpired("helper", 30, output=SECRET.encode()),
        subprocess.CalledProcessError(1, "helper", output=SECRET.encode()),
    ],
)
def test_helper_execution_exceptions_are_sanitized(helper: Path, failure: Exception, monkeypatch):
    def run(*args, **kwargs):
        raise failure

    monkeypatch.setattr(secrets, "_run_helper", run)
    with pytest.raises(SecretStoreError) as error:
        KeychainSecretStore(helper).set(ACCOUNT, SECRET)
    assert SECRET not in "".join(traceback.format_exception(error.value))


def test_nonzero_exit_does_not_parse_or_return_helper_output(helper: Path, monkeypatch):
    monkeypatch.setattr(
        secrets,
        "_run_helper",
        lambda path, request: subprocess.CompletedProcess(
            [str(path)], 1, json.dumps({"ok": True, "value": SECRET}).encode()
        ),
    )
    with pytest.raises(SecretStoreError) as error:
        KeychainSecretStore(helper).get(ACCOUNT)
    assert SECRET not in str(error.value)


@pytest.mark.parametrize("account", [None, "", "bad\x00account"])
def test_invalid_account_never_starts_helper(helper: Path, account, monkeypatch):
    with pytest.raises(SecretStoreError):
        KeychainSecretStore(helper).get(account)


def test_oversized_input_never_starts_helper(helper: Path, monkeypatch):
    with pytest.raises(SecretStoreError):
        KeychainSecretStore(helper).set(ACCOUNT, "x" * (128 * 1024))


def test_output_is_bounded_while_reading_and_never_echoed(helper: Path, monkeypatch):
    processes = []
    reads = []
    original_read = os.read

    def popen(arguments, **kwargs):
        process = PipeProcess(arguments, lambda request: SECRET.encode() * (128 * 1024))
        processes.append(process)
        return process

    def bounded_read(descriptor, count):
        chunk = original_read(descriptor, count)
        reads.append(len(chunk))
        return chunk

    monkeypatch.setattr(secrets.subprocess, "Popen", popen)
    monkeypatch.setattr(secrets.os, "read", bounded_read)
    with pytest.raises(SecretStoreError) as error:
        KeychainSecretStore(helper).get(ACCOUNT)
    assert sum(reads) == 128 * 1024 + 1
    assert processes[0].done.is_set()
    assert SECRET not in "".join(traceback.format_exception(error.value))


def test_unresponsive_helper_is_killed_after_bounded_wait(helper: Path, monkeypatch):
    processes = []

    def popen(arguments, **kwargs):
        process = PipeProcess(arguments, lambda request: None)
        processes.append(process)
        return process

    monkeypatch.setattr(secrets.subprocess, "Popen", popen)
    monkeypatch.setattr(secrets, "_HELPER_TIMEOUT", 0.03)
    with pytest.raises(SecretStoreError):
        KeychainSecretStore(helper).get(ACCOUNT)
    assert processes[0].killed.is_set()
    assert processes[0].done.is_set()
