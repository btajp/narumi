"""Bundled Codex selection and integrity, without authentication or provider traffic."""

from __future__ import annotations

import hashlib
import json
import os
import traceback
from pathlib import Path
from types import SimpleNamespace

import pytest
from narumi.errors import EngineUnavailableError
from narumi.providers.codex import _runtime
from narumi.providers.codex._runtime import CodexRuntime

from .provider_fakes import FakeProgress

NATIVE_BYTES = b"\xcf\xfa\xed\xfecontrolled bundled fixture, never executed"
LICENSE_BYTES = b"fixture Apache-2.0 license\n"
NOTICE_BYTES = b"fixture OpenAI Codex notice\n"
SECRET = "fixture-codex-bundle-private-39571"


def forbidden(*args, **kwargs):
    pytest.fail("the test must not inspect an external installation")


@pytest.fixture(autouse=True)
def isolate_runtime(monkeypatch):
    monkeypatch.delenv("NARUMI_CONTRACTS_DIR", raising=False)
    monkeypatch.setattr(_runtime, "installed_candidates", lambda: [])
    monkeypatch.setattr(_runtime.subprocess, "Popen", forbidden)


@pytest.fixture
def bundled_runtime(tmp_path, monkeypatch):
    root = tmp_path / "data-root"
    root.mkdir(mode=0o700)
    runtime_root = tmp_path / "Renamed Narumi.app/Contents/Resources/runtime"
    contracts = runtime_root / "contracts"
    contracts.mkdir(parents=True, mode=0o700)
    contracts_manifest = contracts / "manifest.json"
    contracts_manifest.write_text(json.dumps({"contract_version": "fixture"}))
    contracts_manifest.chmod(0o600)
    source = runtime_root / _runtime.BUNDLED_CODEX_RELATIVE_PATH
    source.parent.mkdir(parents=True, mode=0o700)
    source.write_bytes(NATIVE_BYTES)
    source.chmod(0o700)
    codex_manifest = json.loads(json.dumps(_runtime._BUNDLED_CODEX_MANIFEST))
    codex_manifest["binary"].update(
        sha256=hashlib.sha256(NATIVE_BYTES).hexdigest(), size=len(NATIVE_BYTES)
    )
    resources = {}
    for name, data in (("license", LICENSE_BYTES), ("notice", NOTICE_BYTES)):
        prefix = "" if name == "license" else "notice_"
        relative = codex_manifest["license"][f"{prefix}path"]
        path = runtime_root / relative
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_bytes(data)
        codex_manifest["license"].update(
            {f"{prefix}sha256": hashlib.sha256(data).hexdigest(), f"{prefix}size": len(data)}
        )
        resources[name] = path
    runtime_manifest = runtime_root / "manifest.json"
    runtime_manifest.write_text(json.dumps({"app_version": "fixture", "codex": codex_manifest}))
    runtime_manifest.chmod(0o600)
    monkeypatch.setenv("NARUMI_CONTRACTS_DIR", str(contracts))
    monkeypatch.setattr(_runtime, "BUNDLED_CODEX_SHA256", codex_manifest["binary"]["sha256"])
    monkeypatch.setattr(_runtime, "_BUNDLED_CODEX_MANIFEST", codex_manifest)
    verified = []

    def verify(executable, env, cwd):
        verified.append((executable, dict(env), cwd, executable.read_bytes()))

    monkeypatch.setattr(_runtime, "verify_version", verify)
    return SimpleNamespace(
        runtime=CodexRuntime(root),
        root=root,
        contracts_manifest=contracts_manifest,
        runtime_manifest=runtime_manifest,
        source=source,
        resources=resources,
        verified=verified,
    )


def assert_private_failure(error, reason):
    assert error.details == {"reason": reason}
    assert SECRET not in json.dumps(error.to_payload())
    assert SECRET not in "".join(traceback.format_exception(error))


def test_bundled_constants_pin_official_arm64_codex():
    assert _runtime.BUNDLED_CODEX_SHA256 == (
        "a14f9a907c12c8812878b70e6b7d65f81c39ed795513e46a55817d7428c0ca6b"
    )
    assert _runtime.BUNDLED_CODEX_RELATIVE_PATH == Path("codex/0.150.1/codex")


def test_bundled_runtime_is_preferred_and_prepared_from_private_copy(bundled_runtime, monkeypatch):
    fixture = bundled_runtime
    monkeypatch.setattr(_runtime, "installed_candidates", forbidden)
    resource = fixture.runtime.resource()
    fixture.runtime.prepare(resource, FakeProgress("runtime-fixture"))
    assert resource["source"] == "bundled"
    assert resource["version"] == _runtime.SUPPORTED_VERSION
    assert fixture.runtime.executable.read_bytes() == fixture.source.read_bytes() == NATIVE_BYTES
    executable, env, cwd, data = fixture.verified[0]
    assert executable.parent == fixture.runtime.directory and executable != fixture.source
    assert data == NATIVE_BYTES and cwd == fixture.runtime.directory
    assert env["CODEX_HOME"] == str(fixture.runtime.directory / "verification-state")


@pytest.mark.parametrize(
    "kind",
    [
        "binary_missing",
        "binary_wrong_hash",
        "binary_symlink",
        "contracts_manifest",
        "manifest_missing",
        "manifest_wrong",
        "manifest_extra",
        "license_missing",
        "license_wrong_hash",
        "license_symlink",
        "license_group_writable",
        "license_acl",
        "notice_missing",
        "notice_wrong_hash",
        "notice_hardlink",
    ],
)
def test_invalid_bundled_runtime_fails_closed_without_installed_fallback(
    bundled_runtime, monkeypatch, kind
):
    fixture = bundled_runtime
    if kind == "binary_missing":
        fixture.source.unlink()
    elif kind == "binary_wrong_hash":
        fixture.source.write_bytes(NATIVE_BYTES + b"corrupt")
    elif kind == "binary_symlink":
        target = fixture.root / "other-codex"
        target.write_bytes(NATIVE_BYTES)
        target.chmod(0o700)
        fixture.source.unlink()
        fixture.source.symlink_to(target)
    elif kind == "contracts_manifest":
        fixture.contracts_manifest.unlink()
    elif kind == "manifest_missing":
        fixture.runtime_manifest.unlink()
    elif kind.startswith("license_") or kind.startswith("notice_"):
        name = kind.split("_", 1)[0]
        path = fixture.resources[name]
        if kind.endswith("missing"):
            path.unlink()
        elif kind.endswith("wrong_hash"):
            path.write_bytes(path.read_bytes() + b"corrupt")
        elif kind.endswith("symlink"):
            target = fixture.root / f"other-{name}"
            target.write_bytes(path.read_bytes())
            path.unlink()
            path.symlink_to(target)
        elif kind.endswith("group_writable"):
            path.chmod(0o620)
        elif kind.endswith("hardlink"):
            os.link(path, fixture.root / f"other-{name}")
        else:
            target_inode = path.stat().st_ino
            original_guard = _runtime.ensure_no_extended_allow_acl

            def reject_resource_acl(descriptor):
                if os.fstat(descriptor).st_ino == target_inode:
                    raise OSError("fixture extended allow ACL")
                original_guard(descriptor)

            monkeypatch.setattr(_runtime, "ensure_no_extended_allow_acl", reject_resource_acl)
    else:
        value = json.loads(fixture.runtime_manifest.read_text())
        if kind == "manifest_wrong":
            value["codex"]["binary"]["architecture"] = "x64"
        else:
            value["codex"]["trusted"] = True
        fixture.runtime_manifest.write_text(json.dumps(value))
    monkeypatch.setattr(_runtime, "installed_candidates", forbidden)
    resource = fixture.runtime.resource()
    assert resource["source"] == "bundled"
    assert resource["version"] is None and resource["sha256"] is None
    with pytest.raises(EngineUnavailableError) as failure:
        fixture.runtime.prepare(resource, FakeProgress("invalid-bundled-runtime"))
    assert_private_failure(failure.value, "codex_bundled_runtime_unavailable")
    assert not fixture.runtime.directory.exists() and fixture.verified == []


def test_bundled_selection_change_is_rejected_before_private_copy(bundled_runtime):
    fixture = bundled_runtime
    resource = fixture.runtime.resource()
    fixture.source.write_bytes(NATIVE_BYTES + b"changed after selection")
    with pytest.raises(EngineUnavailableError) as failure:
        fixture.runtime.prepare(resource, FakeProgress("stale-bundled-runtime"))
    assert_private_failure(failure.value, "codex_bundled_runtime_unavailable")
    assert not fixture.runtime.directory.exists() and fixture.verified == []
