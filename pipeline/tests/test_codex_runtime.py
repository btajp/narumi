"""Installed-runtime boundaries, without executing Codex or using ambient credentials."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

import pytest
from narumi.errors import CancelledError, EngineUnavailableError
from narumi.providers.codex import _runtime
from narumi.providers.codex._runtime import CodexRuntime, private_environment

from .provider_fakes import FakeProgress

NATIVE_BYTES = b"\xcf\xfa\xed\xfecontrolled fixture, never executed"
SECRET = "fixture-codex-runtime-private-71639"
REAL_CANDIDATES = _runtime.installed_candidates
REAL_VERIFY_VERSION = _runtime.verify_version
REAL_POPEN = subprocess.Popen


def forbidden(*args, **kwargs):
    pytest.fail("the test must not execute Codex or inspect a real installation")


@pytest.fixture(autouse=True)
def isolate_runtime(monkeypatch):
    monkeypatch.delenv("NARUMI_CONTRACTS_DIR", raising=False)
    monkeypatch.setattr(_runtime, "installed_candidates", lambda: [])
    monkeypatch.setattr(_runtime, "verify_version", forbidden)
    monkeypatch.setattr(_runtime.subprocess, "Popen", forbidden)


@pytest.fixture
def installed_runtime(tmp_path, monkeypatch):
    root, installation = tmp_path / "data-root", tmp_path / "installation"
    root.mkdir(mode=0o700)
    source = installation / "Contents/Resources/codex"
    source.parent.mkdir(parents=True, mode=0o700)
    source.write_bytes(NATIVE_BYTES)
    source.chmod(0o700)
    metadata = installation / "package.json"
    metadata.write_text(
        json.dumps({"name": "@openai/codex", "version": _runtime.SUPPORTED_VERSION})
    )
    metadata.chmod(0o600)
    monkeypatch.setattr(_runtime, "installed_candidates", lambda: [(installation, source)])
    verified = []

    def verify(executable, env, cwd):
        verified.append((executable, dict(env), cwd, executable.read_bytes()))

    monkeypatch.setattr(_runtime, "verify_version", verify)
    return SimpleNamespace(
        runtime=CodexRuntime(root),
        root=root,
        source=source,
        metadata=metadata,
        installation=installation,
        verified=verified,
    )


def prepare(fixture):
    resource = fixture.runtime.resource()
    fixture.runtime.prepare(resource, FakeProgress("runtime-fixture"))
    return resource


def assert_private_failure(error, reason):
    assert error.details == {"reason": reason}
    assert SECRET not in json.dumps(error.to_payload())
    assert SECRET not in "".join(traceback.format_exception(error))


@pytest.mark.parametrize("anchor", ["relative", "repo_override", "wrong_leaf"])
def test_repo_contracts_override_keeps_installed_fallback(installed_runtime, monkeypatch, anchor):
    fixture = installed_runtime
    if anchor == "relative":
        configured = Path("runtime/contracts")
    elif anchor == "repo_override":
        configured = fixture.root / "runtime/contracts"
    else:
        configured = fixture.root / "narumi.app/Contents/Resources/runtime/other-contracts"
    monkeypatch.setenv("NARUMI_CONTRACTS_DIR", str(configured))
    resource = fixture.runtime.resource()
    assert resource["source"] == "installed"
    assert resource["version"] == _runtime.SUPPORTED_VERSION
    assert resource["sha256"] == hashlib.sha256(NATIVE_BYTES).hexdigest()


def test_listing_is_passive_and_does_not_prepare_or_touch_installation(installed_runtime):
    fixture = installed_runtime
    fixture.source.chmod(0o755)
    before = fixture.source.stat()
    metadata_before = fixture.metadata.stat()
    resource = fixture.runtime.resource()
    assert resource["source"] == "installed"
    assert resource["sha256"] == hashlib.sha256(NATIVE_BYTES).hexdigest()
    assert fixture.runtime.resource() == resource
    assert fixture.verified == []
    assert not fixture.runtime.directory.exists()
    after = fixture.source.stat()
    assert (after.st_mode, after.st_mtime_ns, after.st_ctime_ns) == (
        before.st_mode,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    metadata_after = fixture.metadata.stat()
    assert (metadata_after.st_mode, metadata_after.st_mtime_ns, metadata_after.st_ctime_ns) == (
        metadata_before.st_mode,
        metadata_before.st_mtime_ns,
        metadata_before.st_ctime_ns,
    )


def test_missing_runtime_checks_do_not_create_data_directories(tmp_path):
    root = tmp_path / "not-created"
    runtime = CodexRuntime(root)
    assert runtime.resource()["sha256"] is None
    with pytest.raises(EngineUnavailableError) as failure:
        runtime.require_prepared()
    assert_private_failure(failure.value, "codex_runtime_preparation_required")
    assert not root.exists()


def test_path_and_ambient_codex_home_are_not_runtime_discovery_sources(tmp_path, monkeypatch):
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    candidate = ambient / "codex"
    candidate.write_bytes(NATIVE_BYTES)
    candidate.chmod(0o700)
    monkeypatch.setenv("PATH", str(ambient))
    monkeypatch.setenv("CODEX_HOME", str(ambient))
    candidates = REAL_CANDIDATES()
    assert all(not path.is_relative_to(ambient) for _, path in candidates)
    assert all(path.is_relative_to(root) for root, path in candidates)
    assert CodexRuntime(tmp_path / "data-root").resource()["sha256"] is None


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "wrong_name",
        "wrong_version",
        "missing_name",
        "missing_version",
        "numeric_version",
        "non_object",
        "invalid_json",
        "oversized",
        "symlink",
        "hardlink",
        "group_writable",
        "world_writable",
        "wrong_owner",
        "allow_acl",
    ],
)
def test_unverified_package_metadata_never_advertises_pinned_version(
    installed_runtime, monkeypatch, kind
):
    fixture, metadata = installed_runtime, installed_runtime.metadata
    value = json.loads(metadata.read_text())
    if kind == "missing":
        metadata.unlink()
    elif kind in {
        "wrong_name",
        "wrong_version",
        "missing_name",
        "missing_version",
        "numeric_version",
    }:
        if kind == "wrong_name":
            value["name"] = "@another-package/codex"
        elif kind == "wrong_version":
            value["version"] = "0.999.0"
        elif kind == "missing_name":
            del value["name"]
        elif kind == "missing_version":
            del value["version"]
        else:
            value["version"] = 150
        metadata.write_text(json.dumps(value))
    elif kind == "non_object":
        metadata.write_text(json.dumps([value]))
    elif kind == "invalid_json":
        metadata.write_text(SECRET)
    elif kind == "oversized":
        metadata.write_text(json.dumps(value) + " " * (16 * 1024))
    elif kind in {"symlink", "hardlink"}:
        target = fixture.root / "other-package.json"
        metadata.rename(target)
        if kind == "symlink":
            metadata.symlink_to(target)
        else:
            os.link(target, metadata)
    elif kind in {"group_writable", "world_writable"}:
        metadata.chmod(0o620 if kind == "group_writable" else 0o602)
    elif kind == "wrong_owner":
        inode, original_stat = metadata.stat().st_ino, os.fstat

        def wrong_owner(descriptor):
            result = original_stat(descriptor)
            if result.st_ino == inode:
                fields = {
                    name: getattr(result, name) for name in dir(result) if name.startswith("st_")
                }
                return SimpleNamespace(**{**fields, "st_uid": os.geteuid() + 1000})
            return result

        monkeypatch.setattr(_runtime.os, "fstat", wrong_owner)
    elif kind == "allow_acl":
        inode, original_guard = metadata.stat().st_ino, _runtime.ensure_no_extended_allow_acl

        def shared_metadata(descriptor):
            if os.fstat(descriptor).st_ino == inode:
                raise OSError(SECRET)
            original_guard(descriptor)

        monkeypatch.setattr(_runtime, "ensure_no_extended_allow_acl", shared_metadata)
    resource = fixture.runtime.resource()
    assert resource["sha256"] is None and resource["version"] is None
    assert fixture.verified == [] and not fixture.runtime.directory.exists()


def test_valid_npm_metadata_can_include_normal_package_fields(installed_runtime):
    fixture = installed_runtime
    value = json.loads(fixture.metadata.read_text())
    fixture.metadata.write_text(
        json.dumps({**value, "bin": {"codex": "bin/codex.js"}, "type": "module"})
    )
    assert fixture.runtime.resource()["sha256"] == hashlib.sha256(NATIVE_BYTES).hexdigest()
    assert fixture.verified == []


def test_unverified_app_candidate_does_not_shadow_later_verified_npm_package(
    installed_runtime, monkeypatch, tmp_path
):
    fixture = installed_runtime
    app = tmp_path / "Codex.app"
    unknown = app / "Contents/Resources/codex"
    unknown.parent.mkdir(parents=True, mode=0o700)
    unknown.write_bytes(NATIVE_BYTES + b"unknown application version")
    unknown.chmod(0o700)
    monkeypatch.setattr(
        _runtime,
        "installed_candidates",
        lambda: [(app, unknown), (fixture.installation, fixture.source)],
    )
    resource = prepare(fixture)
    assert resource["sha256"] == hashlib.sha256(NATIVE_BYTES).hexdigest()
    assert fixture.verified[0][3] == NATIVE_BYTES


@pytest.mark.parametrize("marker_kind", ["missing", "wrong_hash", "wrong_version", "extra_field"])
def test_unverified_cached_copy_cannot_shadow_verified_installation(
    installed_runtime, monkeypatch, marker_kind
):
    fixture, runtime = installed_runtime, installed_runtime.runtime
    runtime.directory.mkdir(parents=True, mode=0o700)
    unverified = NATIVE_BYTES + b"unverified cache"
    runtime.executable.write_bytes(unverified)
    runtime.executable.chmod(0o700)
    value = {
        "version": _runtime.SUPPORTED_VERSION,
        "sha256": hashlib.sha256(unverified).hexdigest(),
    }
    if marker_kind != "missing":
        if marker_kind == "wrong_hash":
            value["sha256"] = "0" * 64
        elif marker_kind == "wrong_version":
            value["version"] = "0.0.0"
        else:
            value["trusted"] = True
        marker = runtime.directory / "verification.json"
        marker.write_text(json.dumps(value))
        marker.chmod(0o600)
    assert runtime.resource()["sha256"] == hashlib.sha256(NATIVE_BYTES).hexdigest()
    monkeypatch.setattr(_runtime, "installed_candidates", lambda: [])
    unavailable = runtime.resource()
    assert unavailable["sha256"] is None and unavailable["version"] is None
    assert fixture.verified == []


def test_verified_cached_copy_is_preferred_and_listing_remains_read_only(installed_runtime):
    fixture = installed_runtime
    resource = prepare(fixture)
    fixture.source.write_bytes(NATIVE_BYTES + b"updated external installation")
    paths = [
        fixture.runtime.directory,
        fixture.runtime.executable,
        fixture.runtime.directory / "verification.json",
    ]
    before = [path.stat() for path in paths]
    assert fixture.runtime.resource() == resource
    after = [path.stat() for path in paths]
    assert [(value.st_mode, value.st_mtime_ns, value.st_ctime_ns) for value in before] == [
        (value.st_mode, value.st_mtime_ns, value.st_ctime_ns) for value in after
    ]
    assert len(fixture.verified) == 1


def test_package_version_change_invalidates_a_previous_resource_selection(installed_runtime):
    fixture = installed_runtime
    resource = fixture.runtime.resource()
    fixture.metadata.write_text(json.dumps({"name": "@openai/codex", "version": "0.999.0"}))
    with pytest.raises(EngineUnavailableError) as failure:
        fixture.runtime.prepare(resource, FakeProgress("metadata-changed"))
    assert_private_failure(failure.value, "codex_installed_runtime_unavailable")
    assert fixture.verified == [] and not fixture.runtime.directory.exists()


def test_binary_version_must_still_match_verified_package_metadata(installed_runtime, monkeypatch):
    fixture = installed_runtime

    def wrong_binary_version(executable, env, cwd):
        raise _runtime.unavailable("codex_runtime_version_unsupported")

    monkeypatch.setattr(_runtime, "verify_version", wrong_binary_version)
    with pytest.raises(EngineUnavailableError) as failure:
        fixture.runtime.prepare(fixture.runtime.resource(), FakeProgress("version-mismatch"))
    assert_private_failure(failure.value, "codex_runtime_version_unsupported")
    assert not fixture.runtime.executable.exists()
    assert not (fixture.runtime.directory / "verification.json").exists()


def test_preparation_copies_verifies_and_pins_private_executable(installed_runtime):
    fixture = installed_runtime
    fixture.source.chmod(0o755)
    resource = prepare(fixture)
    runtime = fixture.runtime
    assert runtime.executable.read_bytes() == NATIVE_BYTES
    assert stat.S_IMODE(runtime.executable.stat().st_mode) == 0o700
    assert stat.S_IMODE(fixture.source.stat().st_mode) == 0o755
    assert len(fixture.verified) == 1
    executable, env, cwd, data = fixture.verified[0]
    assert executable.parent == runtime.directory and executable != fixture.source
    assert data == NATIVE_BYTES and cwd == runtime.directory
    assert env["HOME"] == str(runtime.directory / "verification-home")
    assert env["CODEX_HOME"] == str(runtime.directory / "verification-state")
    assert env["TMPDIR"] == str(runtime.directory / "verification-tmp")
    for directory in runtime.directory.rglob("*"):
        if directory.is_dir():
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    marker = runtime.directory / "verification.json"
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert json.loads(marker.read_text()) == {
        "version": _runtime.SUPPORTED_VERSION,
        "sha256": resource["sha256"],
    }
    fixture.source.unlink()
    assert CodexRuntime(fixture.root).require_prepared() == runtime.executable
    assert runtime.resource()["sha256"] == resource["sha256"]
    assert not list(runtime.directory.glob(".*.tmp"))


@pytest.mark.parametrize(
    "kind",
    [
        "script",
        "not_executable",
        "group_writable",
        "world_writable",
        "setuid",
        "setgid",
        "symlink",
        "hardlink",
        "fifo",
        "too_small",
        "oversized",
    ],
)
def test_untrusted_installed_binary_is_never_offered(installed_runtime, monkeypatch, kind):
    fixture, source = installed_runtime, installed_runtime.source
    modes = {
        "not_executable": 0o600,
        "group_writable": 0o720,
        "world_writable": 0o702,
        "setuid": 0o4700,
        "setgid": 0o2700,
    }
    if kind in modes:
        source.chmod(modes[kind])
    elif kind == "script":
        source.write_bytes(b"#!/bin/sh\nexit 0\n")
    elif kind in {"symlink", "hardlink"}:
        target = fixture.root / "other-native-file"
        target.write_bytes(NATIVE_BYTES)
        target.chmod(0o700)
        source.unlink()
        if kind == "symlink":
            source.symlink_to(target)
        else:
            os.link(target, source)
    elif kind == "fifo":
        source.unlink()
        os.mkfifo(source, mode=0o700)
    elif kind == "too_small":
        source.write_bytes(NATIVE_BYTES[:3])
    elif kind == "oversized":
        monkeypatch.setattr(_runtime, "MAX_BINARY_BYTES", len(NATIVE_BYTES) - 1)
    assert fixture.runtime.resource()["sha256"] is None
    assert fixture.verified == []
    assert not fixture.runtime.directory.exists()


@pytest.mark.parametrize("linked_component", ["root", "Contents", "Resources"])
def test_source_directory_symlink_is_not_followed(installed_runtime, linked_component):
    fixture = installed_runtime
    path = (
        fixture.installation
        if linked_component == "root"
        else (
            fixture.installation / "Contents"
            if linked_component == "Contents"
            else fixture.source.parent
        )
    )
    target = fixture.root / "moved-installation"
    path.rename(target)
    path.symlink_to(target, target_is_directory=True)
    assert fixture.runtime.resource()["sha256"] is None
    assert fixture.verified == []


def test_source_with_extended_allow_acl_is_not_offered(installed_runtime, monkeypatch):
    fixture = installed_runtime
    source_inode = fixture.source.stat().st_ino
    original_guard = _runtime.ensure_no_extended_allow_acl

    def reject_shared_binary(descriptor):
        if os.fstat(descriptor).st_ino == source_inode:
            raise OSError(SECRET)
        original_guard(descriptor)

    monkeypatch.setattr(_runtime, "ensure_no_extended_allow_acl", reject_shared_binary)
    assert fixture.runtime.resource()["sha256"] is None
    assert fixture.verified == []


def test_changed_selection_is_rejected_before_creating_runtime(installed_runtime):
    fixture = installed_runtime
    resource = fixture.runtime.resource()
    fixture.source.write_bytes(NATIVE_BYTES + b"changed after selection")
    with pytest.raises(EngineUnavailableError) as failure:
        fixture.runtime.prepare(resource, FakeProgress("stale-runtime"))
    assert_private_failure(failure.value, "codex_installed_runtime_unavailable")
    assert fixture.verified == []
    assert not fixture.runtime.directory.exists()


@pytest.mark.parametrize(
    "kind",
    [
        "binary",
        "marker_hash",
        "marker_version",
        "marker_extra",
        "marker_oversized",
        "marker_invalid",
        "marker_symlink",
    ],
)
def test_prepared_runtime_requires_unchanged_binary_and_exact_marker(installed_runtime, kind):
    fixture = installed_runtime
    prepare(fixture)
    marker = fixture.runtime.directory / "verification.json"
    value = json.loads(marker.read_text())
    if kind == "binary":
        fixture.runtime.executable.write_bytes(NATIVE_BYTES + b"modified")
    elif kind == "marker_hash":
        value["sha256"] = "0" * 64
        marker.write_text(json.dumps(value))
    elif kind == "marker_version":
        value["version"] = "0.0.0"
        marker.write_text(json.dumps(value))
    elif kind == "marker_extra":
        marker.write_text(json.dumps({**value, "trusted": True}))
    elif kind == "marker_oversized":
        marker.write_text(" " * 4097)
    elif kind == "marker_invalid":
        marker.write_text(SECRET)
    elif kind == "marker_symlink":
        target = fixture.root / "outside-marker"
        target.write_text(json.dumps(value))
        marker.unlink()
        marker.symlink_to(target)
    with pytest.raises(EngineUnavailableError) as failure:
        fixture.runtime.require_prepared()
    assert_private_failure(failure.value, "codex_runtime_preparation_required")


@pytest.mark.parametrize(
    "relative",
    [
        ".",
        "providers",
        "providers/runtime",
        "providers/runtime/codex-app-server",
        f"providers/runtime/codex-app-server/{_runtime.SUPPORTED_VERSION}",
        *(
            f"providers/runtime/codex-app-server/{_runtime.SUPPORTED_VERSION}/verification-{name}"
            for name in ("home", "state", "tmp")
        ),
    ],
)
def test_preparation_rejects_symlinks_in_every_owned_root(installed_runtime, relative):
    fixture = installed_runtime
    outside = fixture.root.parent / "outside"
    outside.mkdir(mode=0o700)
    sentinel = outside / "untouched"
    sentinel.write_text(SECRET)
    path = fixture.root / relative
    if path == fixture.root:
        fixture.root.rmdir()
    else:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.symlink_to(outside, target_is_directory=True)
    resource = fixture.runtime.resource()
    with pytest.raises(EngineUnavailableError) as failure:
        fixture.runtime.prepare(resource, FakeProgress("unsafe-root"))
    assert_private_failure(failure.value, "codex_runtime_not_secure")
    assert list(outside.iterdir()) == [sentinel]
    assert sentinel.read_text() == SECRET and fixture.verified == []


def test_cancellation_during_version_check_does_not_publish_ready(installed_runtime, monkeypatch):
    fixture = installed_runtime
    progress = FakeProgress("cancel-during-version")

    def cancel(executable, env, cwd):
        progress.cancelled = True

    monkeypatch.setattr(_runtime, "verify_version", cancel)
    with pytest.raises(CancelledError):
        fixture.runtime.prepare(fixture.runtime.resource(), progress)
    assert not fixture.runtime.executable.exists()
    assert not (fixture.runtime.directory / "verification.json").exists()
    assert not list(fixture.runtime.directory.glob(".*.tmp"))


def test_version_verification_cannot_replace_hashed_copy(installed_runtime, monkeypatch):
    fixture = installed_runtime

    def tamper(executable, env, cwd):
        executable.write_bytes(NATIVE_BYTES + b"modified during version verification")

    monkeypatch.setattr(_runtime, "verify_version", tamper)
    with pytest.raises(EngineUnavailableError) as failure:
        fixture.runtime.prepare(fixture.runtime.resource(), FakeProgress("modified-copy"))
    assert_private_failure(failure.value, "codex_runtime_changed")
    assert not fixture.runtime.executable.exists()
    assert not (fixture.runtime.directory / "verification.json").exists()


def test_private_environment_excludes_ambient_auth_configuration_and_injection(
    tmp_path, monkeypatch
):
    for name in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_BASE_URL",
        "CODEX_HOME",
        "HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "SSL_CERT_FILE",
        "NODE_OPTIONS",
        "DYLD_INSERT_LIBRARIES",
        "LD_PRELOAD",
        "RUST_LOG",
        "CODEX_CONFIG",
    ):
        monkeypatch.setenv(name, SECRET)
    home, codex_home, temporary = (tmp_path / name for name in ("home", "state", "tmp"))
    env = private_environment(home, codex_home, temporary)
    assert SECRET not in str(env)
    assert env["HOME"] == str(home) and env["CODEX_HOME"] == str(codex_home)
    assert env["TMPDIR"] == str(temporary)
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        assert Path(env[name]).is_relative_to(home)
    assert env["PATH"] == "/usr/bin:/bin"
    assert not {"OPENAI_API_KEY", "HTTPS_PROXY", "DYLD_INSERT_LIBRARIES"} & env.keys()
    assert not any(path.exists() for path in (home, codex_home, temporary))


@pytest.fixture
def version_process(tmp_path, monkeypatch):
    processes, launches = [], []
    executable = tmp_path / "native-placeholder"
    env = private_environment(tmp_path / "home", tmp_path / "state", tmp_path / "tmp")

    def run(script):
        def spawn(command, **kwargs):
            assert command == [str(executable), "--version"]
            assert kwargs["start_new_session"] is True
            assert kwargs["stdin"] == subprocess.DEVNULL
            assert kwargs["stderr"] == subprocess.DEVNULL
            assert kwargs["env"] == env
            process = REAL_POPEN([sys.executable, "-I", "-u", "-c", script], **kwargs)
            processes.append(process)
            launches.append(kwargs)
            return process

        monkeypatch.setattr(_runtime.subprocess, "Popen", spawn)
        return REAL_VERIFY_VERSION(executable, env, tmp_path)

    run.processes = processes
    yield run
    for process in processes:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        process.wait(timeout=3)
        if process.stdout is not None:
            process.stdout.close()


def test_version_probe_accepts_only_pinned_version_and_reaps_process(version_process):
    version_process(f"print('codex-cli {_runtime.SUPPORTED_VERSION}')")
    process = version_process.processes[0]
    assert process.poll() == 0 and process.stdout.closed


@pytest.mark.parametrize("output", ["codex-cli 0.0.0", "0.150.1", SECRET])
def test_unsupported_or_untrusted_version_output_is_not_exposed(version_process, output):
    with pytest.raises(EngineUnavailableError) as failure:
        version_process(f"print({output!r})")
    assert_private_failure(failure.value, "codex_runtime_version_unsupported")


@pytest.mark.parametrize(
    "script",
    [
        f"import sys; print('codex-cli {_runtime.SUPPORTED_VERSION}'); "
        f"print({SECRET!r}, file=sys.stderr); sys.exit(1)",
        "import sys; sys.stdout.write('x' * 4097); sys.stdout.flush()",
    ],
)
def test_version_probe_rejects_failed_or_oversized_output(version_process, script, capfd):
    with pytest.raises(EngineUnavailableError) as failure:
        version_process(script)
    assert_private_failure(failure.value, "codex_version_unverified")
    assert SECRET not in repr(capfd.readouterr())


def test_version_probe_has_absolute_deadline_for_partial_output(version_process, monkeypatch):
    clock = iter((0.0, 1.0, 11.0))
    monkeypatch.setattr(_runtime, "time", SimpleNamespace(monotonic=lambda: next(clock, 11.0)))
    with pytest.raises(EngineUnavailableError) as failure:
        version_process(
            "import sys,time; sys.stdout.write('c'); sys.stdout.flush(); time.sleep(30)"
        )
    assert_private_failure(failure.value, "codex_version_unverified")
    assert version_process.processes[0].poll() is not None


def test_version_probe_start_failure_is_sanitized(monkeypatch, tmp_path):
    def fail_start(*args, **kwargs):
        raise OSError(SECRET)

    monkeypatch.setattr(_runtime.subprocess, "Popen", fail_start)
    with pytest.raises(EngineUnavailableError) as failure:
        REAL_VERIFY_VERSION(tmp_path / "native-placeholder", {}, tmp_path)
    assert_private_failure(failure.value, "codex_version_unverified")
