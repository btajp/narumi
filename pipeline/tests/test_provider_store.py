"""Private provider metadata, durable transactions, and concurrent update safety."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from narumi.errors import InvalidArgumentError, NarumiError
from narumi.providers import _acl, _io
from narumi.providers.store import ProviderStore

SECRET = "fake-registry-secret-92471"


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def initialized(root: Path) -> ProviderStore:
    store = ProviderStore(root)
    with store.transaction() as document:
        document["connections"]["conn-example"] = {
            "display_name": "Example",
            "secret_account": "provider/opaque-account",
        }
    return store


def test_new_store_has_no_initialization_io_and_returns_independent_defaults(tmp_path: Path):
    root = tmp_path / "new" / "data"
    store = ProviderStore(root)
    assert not root.exists()
    snapshot = store.read()
    assert snapshot == {
        "version": 2,
        "request_hmac_generation": None,
        "connections": {},
        "catalogs": {},
        "auth_operations": {},
        "runtimes": {},
        "requests": {},
        "checks": {},
    }
    snapshot["connections"]["unsaved"] = {}
    assert not store.read()["connections"]
    assert not store.path.exists()
    assert mode(root.parent) == mode(root) == mode(root / "providers") == 0o700
    assert mode(root / "providers" / "registry.json.lock") == 0o600


def test_metadata_survives_reopening_without_mutable_aliases(tmp_path: Path):
    store = initialized(tmp_path)
    with store.transaction() as document:
        document["requests"]["req-1"] = {"fingerprint": {"scheme": "hmac-sha256", "digest": "abc"}}
    reopened = ProviderStore(tmp_path)
    snapshot = reopened.read()
    assert snapshot["connections"]["conn-example"]["secret_account"] == "provider/opaque-account"
    snapshot["connections"]["conn-example"]["display_name"] = "Unsaved"
    assert reopened.read()["connections"]["conn-example"]["display_name"] == "Example"
    assert mode(store.path) == 0o600


def test_pre_marker_registry_is_normalized_without_bootstrapping_hmac(tmp_path: Path):
    store = initialized(tmp_path)
    document = json.loads(store.path.read_text())
    document.pop("request_hmac_generation")
    store.path.write_text(json.dumps(document))

    reopened = ProviderStore(tmp_path)
    assert reopened.read()["request_hmac_generation"] is None
    with reopened.transaction():
        pass
    assert json.loads(store.path.read_text())["request_hmac_generation"] is None


@pytest.mark.parametrize(
    "generation",
    [
        True,
        0,
        2,
        "1",
        {"scheme": "sha256", "digest": "a" * 63},
        {"scheme": "sha512", "digest": "a" * 64},
    ],
)
def test_invalid_request_hmac_generation_is_rejected(tmp_path: Path, generation):
    store = initialized(tmp_path)
    document = json.loads(store.path.read_text())
    document["request_hmac_generation"] = generation
    store.path.write_text(json.dumps(document))

    with pytest.raises(NarumiError):
        ProviderStore(tmp_path).read()


def test_existing_ancestors_are_unchanged_and_private_modes_are_repaired(tmp_path: Path):
    root = tmp_path / "existing"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    store = initialized(root)
    for path in (store.path, store.path.with_name("registry.json.lock")):
        path.chmod(0o644)
    store.path.parent.chmod(0o755)
    store.read()
    assert mode(root) == 0o755
    assert mode(store.path.parent) == 0o700
    assert mode(store.path) == mode(store.path.with_name("registry.json.lock")) == 0o600


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("caller failure"),
        InvalidArgumentError("invalid"),
        ValueError("caller validation failure"),
        TypeError("caller argument failure"),
    ],
)
def test_caller_exceptions_are_preserved_and_do_not_commit(tmp_path: Path, error: Exception):
    store = initialized(tmp_path)
    previous = store.path.read_bytes()
    with pytest.raises(type(error)) as caught, store.transaction() as document:
        document["connections"].clear()
        raise error
    assert caught.value is error
    assert store.path.read_bytes() == previous


def test_explicit_intent_commit_survives_later_caller_failure(tmp_path: Path):
    store = initialized(tmp_path)
    with pytest.raises(RuntimeError), store.transaction() as document:
        document["requests"]["req-1"] = {"state": "prepared"}
        store.commit(document)
        document["requests"]["req-1"]["state"] = "not-saved"
        raise RuntimeError("side effect failed")
    assert ProviderStore(tmp_path).read()["requests"]["req-1"]["state"] == "prepared"


def test_unchanged_commits_do_not_repeat_atomic_writes(tmp_path: Path, monkeypatch):
    store = initialized(tmp_path)
    writes = []
    original = _io.replace_private

    def counted(directory, contents):
        writes.append(contents)
        original(directory, contents)

    monkeypatch.setattr(_io, "replace_private", counted)
    with store.transaction() as document:
        document["checks"]["check-1"] = {"state": "prepared"}
        store.commit(document)
    with store.transaction():
        pass
    assert len(writes) == 1


def test_transaction_reentry_and_unowned_commits_fail_without_deadlocking(tmp_path: Path):
    store = initialized(tmp_path)
    with pytest.raises(RuntimeError):
        store.commit(store.read())
    with store.transaction() as document:
        with pytest.raises(RuntimeError), store.transaction():
            pass
        with pytest.raises(RuntimeError):
            store.read()
        with pytest.raises(RuntimeError):
            store.commit(dict(document))


@pytest.mark.parametrize(
    "malformed",
    [
        SECRET,
        "[]",
        '{"version": 1, "version": 1}',
        json.dumps({"version": 2}),
        json.dumps({"version": True}),
    ],
)
def test_invalid_saved_metadata_is_not_replaced_or_echoed(tmp_path: Path, malformed: str):
    store = initialized(tmp_path)
    store.path.write_text(malformed)
    with pytest.raises(NarumiError) as error, store.transaction():
        pytest.fail("invalid documents must not be yielded")
    assert error.value.code == "internal"
    assert SECRET not in str(error.value)
    assert error.value.details == {}
    assert error.value.__suppress_context__ is True
    assert store.path.read_text() == malformed


@pytest.mark.parametrize("field", ["api_key", "refresh_token", "client_token", "hmac_secret"])
def test_plaintext_secret_fields_are_rejected_at_any_depth(tmp_path: Path, field: str):
    store = initialized(tmp_path)
    previous = store.path.read_bytes()
    with pytest.raises(NarumiError) as error, store.transaction() as document:
        document["requests"]["bad"] = {"nested": [{field: SECRET}]}
    assert SECRET not in str(error.value)
    assert store.path.read_bytes() == previous


@pytest.mark.parametrize("field", ["authorization_url", "user_code"])
def test_authentication_challenges_cannot_enter_operations_or_receipts(tmp_path: Path, field: str):
    store = initialized(tmp_path)
    previous = store.path.read_bytes()
    for section in ("auth_operations", "requests"):
        with pytest.raises(NarumiError) as error, store.transaction() as document:
            document[section]["fixture"] = {"nested": [{field: SECRET}]}
        assert SECRET not in str(error.value)
        assert error.value.details == {}
        assert store.path.read_bytes() == previous


def test_legacy_challenges_are_redacted_on_read_and_removed_only_by_a_transaction(tmp_path: Path):
    store = initialized(tmp_path)
    document = store.read()
    challenge = {
        "authorization_url": "https://auth.openai.com/oauth/authorize",
        "user_code": SECRET,
    }
    document["auth_operations"]["legacy"] = dict(challenge)
    document["requests"]["legacy"] = {"response": {"operation": dict(challenge)}}
    original = json.dumps(document)
    store.path.write_text(original)
    reopened = ProviderStore(tmp_path)
    redacted = {"authorization_url": None, "user_code": None}
    assert reopened.read()["auth_operations"]["legacy"] == redacted
    assert reopened.read()["requests"]["legacy"]["response"]["operation"] == redacted
    assert store.path.read_text() == original
    with reopened.transaction():
        pass
    assert SECRET not in store.path.read_text()
    assert challenge["authorization_url"] not in store.path.read_text()


@pytest.mark.parametrize("value", [b"not-json", float("nan"), {1: "integer-key"}])
def test_non_json_metadata_is_rejected_atomically(tmp_path: Path, value):
    store = initialized(tmp_path)
    previous = store.path.read_bytes()
    with pytest.raises(NarumiError), store.transaction() as document:
        document["checks"]["invalid"] = value
    assert store.path.read_bytes() == previous


@pytest.mark.parametrize("name", ["registry.json", "registry.json.lock"])
def test_file_symlinks_are_not_followed(tmp_path: Path, name: str):
    target = tmp_path / "unrelated.json"
    target.write_text(SECRET)
    target.chmod(0o644)
    providers = tmp_path / "providers"
    providers.mkdir()
    (providers / name).symlink_to(target)
    with pytest.raises(NarumiError):
        ProviderStore(tmp_path).read()
    assert target.read_text() == SECRET
    assert mode(target) == 0o644


@pytest.mark.parametrize("component", ["root", "providers"])
def test_directory_symlinks_are_not_followed(tmp_path: Path, component: str):
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir(mode=0o755)
    unrelated.chmod(0o755)
    root = tmp_path / "data"
    if component == "root":
        root.symlink_to(unrelated, target_is_directory=True)
    else:
        root.mkdir()
        (root / "providers").symlink_to(unrelated, target_is_directory=True)
    with pytest.raises(NarumiError):
        ProviderStore(root).read()
    assert not list(unrelated.iterdir())
    assert mode(unrelated) == 0o755


@pytest.mark.parametrize("kind", ["fifo", "directory", "hardlink"])
def test_non_regular_or_linked_registry_is_rejected(tmp_path: Path, kind: str):
    providers = tmp_path / "providers"
    providers.mkdir()
    target = providers / "registry.json"
    unrelated = tmp_path / "unrelated.json"
    if kind == "fifo":
        os.mkfifo(target)
    elif kind == "directory":
        target.mkdir()
    else:
        unrelated.write_text(SECRET)
        unrelated.chmod(0o644)
        os.link(unrelated, target)
    with pytest.raises(NarumiError):
        ProviderStore(tmp_path).read()
    if kind == "hardlink":
        assert unrelated.read_text() == SECRET
        assert mode(unrelated) == 0o644


@pytest.mark.parametrize("target", ["registry.json", "registry.json.lock", "providers"])
def test_existing_shared_write_permissions_are_rejected_before_chmod(tmp_path: Path, target: str):
    store = initialized(tmp_path)
    unsafe = store.path.parent if target == "providers" else store.path.parent / target
    permissions = 0o777 if unsafe.is_dir() else 0o666
    unsafe.chmod(permissions)
    with pytest.raises(NarumiError):
        store.read()
    assert mode(unsafe) == permissions


@pytest.mark.parametrize("target", ["root", "providers", "registry.json", "registry.json.lock"])
def test_acl_rejection_precedes_mode_repair_on_every_store_boundary(
    tmp_path: Path, target: str, monkeypatch
):
    store = initialized(tmp_path)
    paths = {"root": tmp_path, "providers": store.path.parent}
    path = paths.get(target, store.path.parent / target)
    permissions = 0o755 if path.is_dir() else 0o644
    path.chmod(permissions)
    inode = path.stat().st_ino
    previous = store.path.read_bytes()

    def reject_acl(descriptor):
        if os.fstat(descriptor).st_ino == inode:
            raise OSError(SECRET)

    monkeypatch.setattr(_io, "ensure_no_extended_allow_acl", reject_acl)
    with pytest.raises(NarumiError) as error:
        store.read()
    assert SECRET not in str(error.value)
    assert mode(path) == permissions
    assert store.path.read_bytes() == previous


class ACLFixture:
    """Descriptor ACL API double; ordinary mode checks cannot see these entries."""

    def __init__(self, tags, *, tag_failure=False):
        self.tags = iter(tags)
        self.tag_failure = tag_failure
        self.tag = None
        self.freed = []

    def acl_get_fd_np(self, descriptor, kind):
        return 100

    def acl_get_entry(self, acl, kind, entry):
        try:
            self.tag = next(self.tags)
        except StopIteration:
            ctypes.set_errno(errno.EINVAL)
            return -1
        entry._obj.value = 200
        return 0

    def acl_get_tag_type(self, entry, tag):
        tag._obj.value = self.tag
        return -1 if self.tag_failure else 0

    def acl_free(self, acl):
        self.freed.append(acl)
        return 0


@pytest.mark.parametrize("tags,rejected", [([], False), ([2], False), ([1], True), ([2, 1], True)])
def test_acl_guard_allows_only_empty_or_deny_only_lists(tags, rejected, monkeypatch):
    library = ACLFixture(tags)
    monkeypatch.setattr(_acl, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(_acl, "_libc", lambda: library)
    if rejected:
        with pytest.raises(OSError):
            _acl.ensure_no_extended_allow_acl(10)
    else:
        _acl.ensure_no_extended_allow_acl(10)
    assert library.freed == [100]


@pytest.mark.parametrize("failure", ["load", "tag", "too_many_entries"])
def test_acl_inspection_errors_are_fixed_fail_closed_errors(failure: str, monkeypatch):
    library = ACLFixture(
        [2] * (257 if failure == "too_many_entries" else 1), tag_failure=failure == "tag"
    )

    def load():
        if failure == "load":
            raise OSError(SECRET)
        return library

    monkeypatch.setattr(_acl, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(_acl, "_libc", load)
    with pytest.raises(OSError) as error:
        _acl.ensure_no_extended_allow_acl(10)
    assert str(error.value) == "Provider path permissions could not be verified"
    assert error.value.__suppress_context__ is True


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS extended ACL semantics")
@pytest.mark.parametrize(
    "entry,rejected", [("everyone allow read,write", True), ("everyone deny delete", False)]
)
def test_native_acl_guard_on_private_temporary_file(tmp_path: Path, entry: str, rejected: bool):
    path = tmp_path / "acl-fixture"
    path.write_text("non-secret fixture")
    path.chmod(0o600)
    subprocess.run(["/bin/chmod", "+a", entry, str(path)], check=True, capture_output=True)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            assert mode(path) == 0o600
            if rejected:
                with pytest.raises(OSError):
                    _acl.ensure_no_extended_allow_acl(descriptor)
            else:
                _acl.ensure_no_extended_allow_acl(descriptor)
        finally:
            os.close(descriptor)
    finally:
        subprocess.run(["/bin/chmod", "-a", entry, str(path)], check=True, capture_output=True)


def test_failed_replace_preserves_previous_file_and_cleans_temporary(tmp_path: Path, monkeypatch):
    store = initialized(tmp_path)
    previous = store.path.read_bytes()

    def fail_replace(source, target, **kwargs):
        assert target == "registry.json"
        assert mode(store.path.parent / source) == 0o600
        raise OSError(SECRET)

    monkeypatch.setattr(_io.os, "replace", fail_replace)
    with pytest.raises(NarumiError) as error, store.transaction() as document:
        document["connections"].clear()
    assert SECRET not in str(error.value)
    assert store.path.read_bytes() == previous
    assert sorted(path.name for path in store.path.parent.iterdir()) == [
        "registry.json",
        "registry.json.lock",
    ]


def test_file_and_parent_are_fsynced_in_commit_order(tmp_path: Path, monkeypatch):
    store = initialized(tmp_path)
    operations = []
    original_fsync, original_replace = _io.os.fsync, _io.os.replace

    def fsync(descriptor):
        operations.append("directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file")
        original_fsync(descriptor)

    def replace(*args, **kwargs):
        operations.append("replace")
        original_replace(*args, **kwargs)

    monkeypatch.setattr(_io.os, "fsync", fsync)
    monkeypatch.setattr(_io.os, "replace", replace)
    with store.transaction() as document:
        document["checks"]["check-1"] = {}
    assert operations == ["file", "replace", "directory"]


def test_size_limit_applies_to_reads_and_writes(tmp_path: Path, monkeypatch):
    store = initialized(tmp_path)
    previous = store.path.read_bytes()
    monkeypatch.setattr(_io, "MAX_REGISTRY_BYTES", 1024)
    with pytest.raises(NarumiError), store.transaction() as document:
        document["checks"]["large"] = "x" * 1024
    assert store.path.read_bytes() == previous
    store.path.write_text(" " * 1025)
    with pytest.raises(NarumiError):
        store.read()


def test_concurrent_store_instances_do_not_lose_updates(tmp_path: Path):
    def update(index: int):
        with ProviderStore(tmp_path).transaction() as document:
            document["checks"][str(index)] = {"done": True}

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(update, range(40)))
    assert len(ProviderStore(tmp_path).read()["checks"]) == 40


def test_cross_process_update_waits_for_active_transaction(tmp_path: Path):
    store = initialized(tmp_path)
    program = (
        "import sys\n"
        "from pathlib import Path\n"
        "from narumi.providers.store import ProviderStore\n"
        "print('ready', flush=True)\n"
        "with ProviderStore(Path(sys.argv[1])).transaction() as document:\n"
        "    document['checks']['child'] = {'done': True}\n"
        "print('updated', flush=True)\n"
    )
    child = None
    try:
        with store.transaction() as document:
            document["checks"]["parent"] = {"done": True}
            child = subprocess.Popen(
                [sys.executable, "-c", program, str(tmp_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert child.stdout is not None
            assert child.stdout.readline().strip() == "ready"
            with pytest.raises(subprocess.TimeoutExpired):
                child.wait(timeout=0.2)
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, stderr
        assert stdout.strip() == "updated"
        assert ProviderStore(tmp_path).read()["checks"] == {
            "parent": {"done": True},
            "child": {"done": True},
        }
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.communicate(timeout=10)
