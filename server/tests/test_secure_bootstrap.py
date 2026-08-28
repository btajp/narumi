"""Local TLS identity is trusted only after owner, endpoint and certificate checks."""

from __future__ import annotations

import json
import os
import ssl
import stat
import subprocess
import sys
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from narumi.errors import BusyError
from narumi_server import bootstrap, secure_transport, transport_tls
from narumi_server.secure_transport import (
    BootstrapNotFoundError,
    ServerTransport,
    TransportSecurityError,
    load_client_transport,
    prepare_server_transport,
)


class FakeSecretStore:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.calls: list[tuple[str, str]] = []
        self.failures: dict[str, Exception] = {}

    def _record(self, operation: str, account: str) -> None:
        self.calls.append((operation, account))
        if error := self.failures.get(operation):
            raise error

    def get(self, account: str) -> str | None:
        self._record("get", account)
        return self.values.get(account)

    def set(self, account: str, value: str) -> None:
        self._record("set", account)
        self.values[account] = value

    def delete(self, account: str) -> None:
        self._record("delete", account)
        self.values.pop(account, None)


@pytest.fixture(autouse=True)
def forbid_real_keychain(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    attempts: list[None] = []

    def forbidden() -> None:
        attempts.append(None)
        raise AssertionError("Tests must supply a fake secret store")

    monkeypatch.setattr(secure_transport, "_default_secret_store", forbidden)
    yield
    assert not attempts


@pytest.fixture
def identity(tmp_path: Path) -> Iterator[tuple[Path, FakeSecretStore, ServerTransport]]:
    root = tmp_path / "home"
    store = FakeSecretStore()
    with prepare_server_transport(root, str(uuid4()), secret_store=store) as server:
        store.calls.clear()
        yield root, store, server


def update_document(server: ServerTransport, **changes: Any) -> None:
    document = json.loads(server.bootstrap_path.read_text())
    document.update(changes)
    server.bootstrap_path.write_text(json.dumps(document))


def assert_rejected_before_secret(root: Path, store: FakeSecretStore, **kwargs: Any) -> None:
    with pytest.raises(TransportSecurityError):
        load_client_transport(root, secret_store=store, **kwargs)
    assert not store.calls


@contextmanager
def temporary_acl(target: Path, entry: str) -> Iterator[None]:
    subprocess.run(["/bin/chmod", "+a", entry, str(target)], check=True, capture_output=True)
    try:
        yield
    finally:
        subprocess.run(["/bin/chmod", "-a", entry, str(target)], check=True, capture_output=True)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_published_identity_is_private_and_roundtrips_without_secret_repr(
    tmp_path: Path, host: str
) -> None:
    root, store = tmp_path / "data", FakeSecretStore()
    with prepare_server_transport(root, str(uuid4()), host=host, secret_store=store) as server:
        client = load_client_transport(root, expected_url=server.url, secret_store=store)
        assert client.client_token == server.client_token
        assert client.server_instance_id == server.server_instance_id
        assert client.ssl_context.verify_mode == ssl.CERT_REQUIRED
        assert client.ssl_context.check_hostname
        assert client.ssl_context.cert_store_stats()["x509"] == 1
        for directory in (root, root / "runtime", server.bootstrap_path.parent):
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        for file in server.bootstrap_path.parent.iterdir():
            assert stat.S_IMODE(file.stat().st_mode) == 0o600
        for public_value in (server.bootstrap_path.read_text(), repr(server), repr(client)):
            assert server.client_token not in public_value
            assert "PRIVATE KEY" not in public_value
        assert "certificate_pem" not in repr(client)
    assert not server.bootstrap_path.exists()
    assert not server.certificate_path.exists()
    assert not server.private_key_path.exists()
    assert not store.values


def test_bootstrap_replace_exposes_only_complete_owner_only_files(
    identity: tuple[Path, FakeSecretStore, ServerTransport], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, server = identity
    original = server.bootstrap_path.read_bytes()
    replacement = json.loads(original) | {"pid": os.getpid() + 1}
    real_replace, observed = os.replace, []

    def inspect_replace(source: str, target: str, **kwargs: Any) -> None:
        assert server.bootstrap_path.read_bytes() == original
        temporary = server.bootstrap_path.parent / source
        assert stat.S_IMODE(temporary.stat().st_mode) == 0o600
        assert json.loads(temporary.read_bytes()) == replacement
        observed.append(target)
        real_replace(source, target, **kwargs)

    monkeypatch.setattr(bootstrap.os, "replace", inspect_replace)
    with bootstrap.private_server_directory(root) as directory:
        bootstrap.write_bootstrap(directory, replacement)
    assert observed == [bootstrap.BOOTSTRAP_FILE]
    assert json.loads(server.bootstrap_path.read_bytes()) == replacement
    assert not list(server.bootstrap_path.parent.glob(".*.tmp"))


def test_failed_atomic_write_preserves_previous_bootstrap_and_removes_temporary(
    identity: tuple[Path, FakeSecretStore, ServerTransport], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, server = identity
    original = server.bootstrap_path.read_bytes()

    def fail_replace(*args: Any, **kwargs: Any) -> None:
        raise OSError("simulated interrupted publication")

    monkeypatch.setattr(bootstrap.os, "replace", fail_replace)
    with bootstrap.private_server_directory(root) as directory:
        with pytest.raises(TransportSecurityError):
            bootstrap.write_bootstrap(directory, {"incomplete": True})
    assert server.bootstrap_path.read_bytes() == original
    assert not list(server.bootstrap_path.parent.glob(".*.tmp"))


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_atomic_write_refuses_existing_aliased_target(tmp_path: Path, link_kind: str) -> None:
    root, original = tmp_path / "data", tmp_path / "existing-file"
    original.write_bytes(b"must remain unchanged")
    original.chmod(0o600)
    with bootstrap.private_server_directory(root, create=True) as directory:
        target = bootstrap.bootstrap_path(root)
        if link_kind == "symlink":
            target.symlink_to(original)
        else:
            os.link(original, target)
        with pytest.raises(TransportSecurityError):
            bootstrap.write_bootstrap(directory, {"replacement": True})
    assert original.read_bytes() == b"must remain unchanged"
    assert not list(target.parent.glob(".*.tmp"))


@pytest.mark.parametrize("missing", ["root", "runtime", "server", "bootstrap"])
def test_only_absent_bootstrap_allows_not_found(tmp_path: Path, missing: str) -> None:
    root, store = tmp_path / "data", FakeSecretStore()
    for component, directory in (
        ("root", root),
        ("runtime", root / "runtime"),
        ("server", root / "runtime/server"),
    ):
        if component == missing:
            break
        directory.mkdir(mode=0o700)
    with pytest.raises(BootstrapNotFoundError):
        load_client_transport(root, secret_store=store)
    assert not store.calls


@pytest.mark.parametrize("content", [b"{", b"[]", b"null", b"\xff", b"{}"])
def test_malformed_bootstrap_is_not_absence(
    identity: tuple[Path, FakeSecretStore, ServerTransport], content: bytes
) -> None:
    root, store, server = identity
    server.bootstrap_path.write_bytes(content)
    assert_rejected_before_secret(root, store)


def test_duplicate_fields_and_oversized_json_are_rejected_before_secret(
    identity: tuple[Path, FakeSecretStore, ServerTransport],
) -> None:
    root, store, server = identity
    original = server.bootstrap_path.read_bytes()
    duplicate = b'{"version":1,' + original.lstrip()[1:]
    server.bootstrap_path.write_bytes(duplicate)
    assert_rejected_before_secret(root, store)
    padded = original + b" " * (bootstrap.MAX_BOOTSTRAP_BYTES - len(original))
    server.bootstrap_path.write_bytes(padded)
    assert (
        load_client_transport(root, secret_store=store).server_instance_id
        == server.server_instance_id
    )
    store.calls.clear()
    server.bootstrap_path.write_bytes(padded + b" ")
    assert_rejected_before_secret(root, store)


@pytest.mark.parametrize("component", ["root", "runtime", "server", "bootstrap"])
def test_symlinks_at_every_bootstrap_boundary_are_rejected(
    identity: tuple[Path, FakeSecretStore, ServerTransport], component: str
) -> None:
    root, store, server = identity
    target = {
        "root": root,
        "runtime": root / "runtime",
        "server": server.bootstrap_path.parent,
        "bootstrap": server.bootstrap_path,
    }[component]
    original = target.with_name(f"original-{target.name}")
    target.rename(original)
    target.symlink_to(original, target_is_directory=component != "bootstrap")
    assert_rejected_before_secret(root, store)
    assert original.exists()


@pytest.mark.parametrize(
    ("component", "mode"),
    [
        ("root", 0o777),
        ("root", 0o775),
        ("runtime", 0o755),
        ("server", 0o750),
        ("bootstrap", 0o644),
        ("bootstrap", 0o660),
        ("bootstrap", 0o400),
    ],
)
def test_untrusted_permissions_are_rejected_before_secret(
    identity: tuple[Path, FakeSecretStore, ServerTransport], component: str, mode: int
) -> None:
    root, store, server = identity
    target = {
        "root": root,
        "runtime": root / "runtime",
        "server": server.bootstrap_path.parent,
        "bootstrap": server.bootstrap_path,
    }[component]
    target.chmod(mode)
    assert_rejected_before_secret(root, store)


def test_only_startup_tightens_safe_legacy_directories(tmp_path: Path) -> None:
    root, store = tmp_path / "legacy", FakeSecretStore()
    runtime, server_directory = root / "runtime", root / "runtime/server"
    for directory in (root, runtime, server_directory):
        directory.mkdir()
        directory.chmod(0o755)
    assert_rejected_before_secret(root, store)
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o755 for path in (root, runtime, server_directory)
    )
    with prepare_server_transport(root, str(uuid4()), secret_store=store) as server:
        assert stat.S_IMODE(root.stat().st_mode) == 0o755
        assert all(
            stat.S_IMODE(path.stat().st_mode) == 0o700 for path in (runtime, server_directory)
        )
        assert load_client_transport(root, secret_store=store).client_token == server.client_token


def test_startup_does_not_repair_world_writable_legacy_directory(tmp_path: Path) -> None:
    root, store = tmp_path / "legacy", FakeSecretStore()
    runtime = root / "runtime"
    runtime.mkdir(parents=True)
    runtime.chmod(0o777)
    with pytest.raises(TransportSecurityError):
        prepare_server_transport(root, str(uuid4()), secret_store=store)
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o777
    assert not (runtime / "server").exists()
    assert not store.calls


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS extended ACL semantics")
def test_startup_does_not_repair_legacy_directory_with_allow_acl(tmp_path: Path) -> None:
    root, store = tmp_path / "legacy", FakeSecretStore()
    server_directory = root / "runtime/server"
    server_directory.mkdir(parents=True)
    server_directory.chmod(0o755)
    with temporary_acl(server_directory, "everyone allow read,write"):
        with pytest.raises(TransportSecurityError):
            prepare_server_transport(root, str(uuid4()), secret_store=store)
        assert stat.S_IMODE(server_directory.stat().st_mode) == 0o755
        assert not list(server_directory.iterdir())
        assert not store.calls


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS extended ACL semantics")
@pytest.mark.parametrize("component", ["root", "runtime", "server", "bootstrap"])
def test_extended_allow_acl_is_rejected_despite_private_posix_mode(
    identity: tuple[Path, FakeSecretStore, ServerTransport], component: str
) -> None:
    root, store, server = identity
    target = {
        "root": root,
        "runtime": root / "runtime",
        "server": server.bootstrap_path.parent,
        "bootstrap": server.bootstrap_path,
    }[component]
    with temporary_acl(target, "everyone allow read,write"):
        assert stat.S_IMODE(target.stat().st_mode) == (0o600 if component == "bootstrap" else 0o700)
        assert_rejected_before_secret(root, store)
    assert load_client_transport(root, secret_store=store).client_token == server.client_token


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS extended ACL semantics")
def test_deny_only_acl_does_not_reject_an_owner_only_bootstrap(
    identity: tuple[Path, FakeSecretStore, ServerTransport],
) -> None:
    root, store, server = identity
    with temporary_acl(server.bootstrap_path, "everyone deny delete"):
        client = load_client_transport(root, secret_store=store)
        assert client.client_token == server.client_token


@pytest.mark.parametrize("component", ["root", "runtime", "server", "bootstrap"])
def test_other_owner_is_rejected_from_fstat_without_chown(
    identity: tuple[Path, FakeSecretStore, ServerTransport],
    component: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, store, server = identity
    target = {
        "root": root,
        "runtime": root / "runtime",
        "server": server.bootstrap_path.parent,
        "bootstrap": server.bootstrap_path,
    }[component]
    target_inode, real_fstat = target.stat().st_ino, os.fstat

    def other_owner(fd: int) -> os.stat_result:
        info = real_fstat(fd)
        if info.st_ino == target_inode:
            fields = list(info)
            fields[4] = os.getuid() + 1
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(bootstrap.os, "fstat", other_owner)
    assert_rejected_before_secret(root, store)


@pytest.mark.parametrize("kind", ["hardlink", "fifo"])
def test_bootstrap_must_be_a_single_regular_file(
    identity: tuple[Path, FakeSecretStore, ServerTransport], kind: str
) -> None:
    root, store, server = identity
    if kind == "hardlink":
        os.link(server.bootstrap_path, root / "second-link")
    else:
        server.bootstrap_path.unlink()
        os.mkfifo(server.bootstrap_path, mode=0o600)
    assert_rejected_before_secret(root, store)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8765/mcp",
        "https://localhost:8765/mcp",
        "https://example.com:8765/mcp",
        "https://127.0.0.2:8765/mcp",
        "https://127.0.0.1/mcp",
        "https://127.0.0.1:0/mcp",
        "https://127.0.0.1:8765/mcp?secret=x",
        "https://user:secret@127.0.0.1:8765/mcp",
        "https://127.0.0.1:8765//mcp",
        "https://127.0.0.1:8765/%2e",
        "https://127.0.0.1:8765/mcp\n",
    ],
)
def test_untrusted_endpoint_is_rejected_before_secret(
    identity: tuple[Path, FakeSecretStore, ServerTransport], url: str
) -> None:
    root, store, server = identity
    update_document(server, url=url)
    assert_rejected_before_secret(root, store)


@pytest.mark.parametrize(
    "options",
    [
        {"host": "localhost"},
        {"host": "0.0.0.0"},
        {"host": "example.com"},
        {"port": True},
        {"port": 0},
        {"path": "/mcp?secret=x"},
    ],
)
def test_server_refuses_unsafe_bind_before_creating_identity(
    tmp_path: Path, options: dict[str, Any]
) -> None:
    root, store = tmp_path / "data", FakeSecretStore()
    with pytest.raises(TransportSecurityError):
        prepare_server_transport(root, str(uuid4()), secret_store=store, **options)
    assert not root.exists()
    assert not store.calls


@pytest.mark.parametrize(
    "expected_url",
    ["https://127.0.0.1:8766/mcp", "https://[::1]:8765/mcp", "http://127.0.0.1:8765/mcp"],
)
def test_explicit_endpoint_cannot_override_bootstrap(
    identity: tuple[Path, FakeSecretStore, ServerTransport], expected_url: str
) -> None:
    root, store, _ = identity
    assert_rejected_before_secret(root, store, expected_url=expected_url)


@pytest.mark.parametrize(
    "changes",
    [
        {"certificate_sha256": "0" * 64},
        {"certificate_pem": "not a certificate"},
        {"token_account": "another-account"},
        {"pid": True},
        {"version": True},
        {"server_instance_id": "not-an-instance"},
        {"extra": "untrusted"},
    ],
)
def test_identity_or_pin_tampering_is_rejected_before_secret(
    identity: tuple[Path, FakeSecretStore, ServerTransport], changes: dict[str, Any]
) -> None:
    root, store, server = identity
    update_document(server, **changes)
    assert_rejected_before_secret(root, store)


@pytest.mark.parametrize("days", [-2, 31])
def test_not_yet_valid_and_expired_certificates_are_rejected_before_secret(
    identity: tuple[Path, FakeSecretStore, ServerTransport],
    monkeypatch: pytest.MonkeyPatch,
    days: int,
) -> None:
    root, store, _ = identity
    shifted = datetime.now(UTC) + timedelta(days=days)

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return shifted

    monkeypatch.setattr(transport_tls, "datetime", Clock)
    assert_rejected_before_secret(root, store)


@pytest.mark.parametrize("token", [None, "short", "x" * 513, "space " * 10, 123])
def test_missing_or_invalid_keychain_token_never_becomes_a_valid_transport(
    identity: tuple[Path, FakeSecretStore, ServerTransport], token: Any
) -> None:
    root, store, server = identity
    store.values[server.token_account] = token
    with pytest.raises(TransportSecurityError):
        load_client_transport(root, secret_store=store)
    assert store.calls == [("get", server.token_account)]


def test_keychain_errors_are_safe_and_failed_setup_releases_lease(tmp_path: Path) -> None:
    root, store = tmp_path / "home", FakeSecretStore()
    secret = "fake-secret-that-must-not-appear"
    store.failures["set"] = RuntimeError(secret)
    with pytest.raises(TransportSecurityError) as setup:
        prepare_server_transport(root, str(uuid4()), secret_store=store)
    assert secret not in "".join(traceback.format_exception(setup.value))
    assert not bootstrap.bootstrap_path(root).exists()
    store.failures.clear()
    with prepare_server_transport(root, str(uuid4()), secret_store=store) as server:
        store.failures["get"] = RuntimeError(secret)
        with pytest.raises(TransportSecurityError) as read:
            load_client_transport(root, secret_store=store)
        assert secret not in "".join(traceback.format_exception(read.value))
        assert server.bootstrap_path.exists()


def test_same_root_lease_prevents_replacing_live_identity_and_can_be_reacquired(
    identity: tuple[Path, FakeSecretStore, ServerTransport],
) -> None:
    root, store, server = identity
    original = server.bootstrap_path.read_bytes()
    with pytest.raises(BusyError):
        prepare_server_transport(root, str(uuid4()), secret_store=store)
    assert server.bootstrap_path.read_bytes() == original
    assert not store.calls
    server.close()
    with prepare_server_transport(root, str(uuid4()), secret_store=store) as replacement:
        assert replacement.server_instance_id != server.server_instance_id


def test_separate_roots_do_not_share_locks_or_keychain_accounts(tmp_path: Path) -> None:
    store, instance_id = FakeSecretStore(), str(uuid4())
    first = prepare_server_transport(tmp_path / "one", instance_id, secret_store=store)
    with (
        first,
        prepare_server_transport(tmp_path / "two", instance_id, secret_store=store) as second,
    ):
        assert first.token_account != second.token_account
        first.close()
        assert second.bootstrap_path.exists()
        client = load_client_transport(tmp_path / "two", secret_store=store)
        assert client.client_token == second.client_token


def test_close_removes_only_own_identity_and_is_idempotent(
    identity: tuple[Path, FakeSecretStore, ServerTransport],
) -> None:
    _, store, server = identity
    other_id = str(uuid4())
    other_account = f"{server.token_account.rsplit(':', 1)[0]}:{other_id}"
    store.values[other_account] = "other-instance-token"
    other_key = server.private_key_path.with_name(f"{other_id}.key")
    other_key.write_text("other private file")
    update_document(server, server_instance_id=other_id, token_account=other_account)
    replacement = server.bootstrap_path.read_bytes()
    server.close()
    server.close()
    assert server.bootstrap_path.read_bytes() == replacement
    assert other_key.read_text() == "other private file"
    assert store.values == {other_account: "other-instance-token"}
    assert store.calls == [("delete", server.token_account)]
    assert not server.certificate_path.exists()
    assert not server.private_key_path.exists()
