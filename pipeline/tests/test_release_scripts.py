"""Run the real shipping scripts with fake processes, without touching apps or credentials."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TOOL_SOURCE = Path(__file__).parent / "fixtures/release_tool.py"


@pytest.fixture
def shipping(tmp_path):
    root = tmp_path / "repo"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    for name in (
        "release-app.sh",
        "check-updater-key-policy.sh",
        "release-common.sh",
        "release_artifacts.py",
        "release_public.py",
        "release_verify.py",
    ):
        shutil.copy2(REPO / "scripts" / name, scripts / name)
    tool = tmp_path / "fake-tool.py"
    tool.write_text(f"#!{sys.executable}\n" + TOOL_SOURCE.read_text())
    tool.chmod(0o755)
    binary = tmp_path / "bin"
    binary.mkdir()
    (binary / "python3").symlink_to(sys.executable)
    for name in ("git", "gh", "uv", "codesign", "spctl", "xcrun", "ditto", "hdiutil"):
        (binary / name).symlink_to(tool)
    for name in ("build-app.sh", "check-version.sh", "bundle_inventory.py", "release_dmg.py"):
        (scripts / name).symlink_to(tool)
    sparkle = root / "app/.build/artifacts/sparkle/Sparkle/bin"
    sparkle.mkdir(parents=True)
    for name in ("generate_keys", "sign_update", "generate_appcast"):
        (sparkle / name).symlink_to(tool)
    (root / "app/sparkle-public-key.txt").write_text(base64.b64encode(b"k" * 32).decode())
    (root / "VERSION").write_text("0.1.1\n")
    (root / "CHANGELOG.md").write_text("# Changelog\n\n## 0.1.1\n\nTest release.\n")
    env_file = tmp_path / "release.env"
    env_file.write_text("# Test settings only.\n")
    key_file = tmp_path / "dummy.p8"
    key_file.write_text("not a real signing key\n")
    # A fresh environment prevents real tool overrides, credentials, or user config from leaking in.
    env = {
        "PATH": f"{binary}{os.pathsep}{os.defpath}",
        "FAKE_RELEASE_ROOT": str(root),
        "NARUMI_RELEASE_ENV": str(env_file),
        "APPLE_SIGNING_IDENTITY": "Developer ID Application: Fixture",
        "APPLE_API_KEY": "fixture-secret-key-id",
        "APPLE_API_ISSUER": "fixture-secret-issuer",
        "APPLE_API_KEY_PATH": str(key_file),
    }
    return root, env, env_file


@pytest.fixture
def modern_shipping(shipping):
    root, env, _ = shipping
    env["FAKE_RELEASE_VERSION"] = "0.1.4"
    (root / "VERSION").write_text("0.1.4\n")
    (root / "CHANGELOG.md").write_text("# Changelog\n\n## 0.1.4\n\nTest DMG release.\n")
    return shipping


def invoke(shipping, *args, script="release-app.sh"):
    root, env, _ = shipping
    return subprocess.run(
        ["/bin/bash", str(root / "scripts" / script), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
    )


def calls(shipping):
    path = shipping[0] / "fake-state/calls.jsonl"
    return [json.loads(row) for row in path.read_text().splitlines()] if path.exists() else []


def uploads(shipping):
    return [
        args for name, args in calls(shipping) if name == "gh" and args[:2] == ["release", "create"]
    ]


def test_release_isolated_signed_notarized_draft_and_reverified(shipping):
    root, env, _ = shipping
    live = root / "dist/narumi.app"
    live.mkdir(parents=True)
    (live / "live-marker").write_text("untouched")
    env["DIST_DIR"] = str(live.parent)
    env["SPARKLE_PUBLIC_KEY_FILE"] = str(root / "wrong-public-key")
    result = invoke(shipping, "0.1.1")
    assert result.returncode == 0, result.stderr + result.stdout
    assert (live / "live-marker").read_text() == "untouched"
    directory = root / "dist/release/v0.1.1"
    sealed = json.loads((directory / "release.json").read_text())
    assert sealed["commit"] == "1" * 40 and sealed["build"] == 25
    assert set(sealed["assets"]) == {"narumi-0.1.1.zip", "appcast.xml"}
    assert (directory / "build/narumi.app/Contents/Info.plist").is_file()
    assert (directory / "verify-unpacked/narumi.app/Contents/Info.plist").is_file()
    assert len(uploads(shipping)) == 1
    assert "--draft" in uploads(shipping)[0]
    assert not any(name == "gh" and "edit" in args for name, args in calls(shipping))
    assert "fixture-secret" not in result.stdout + result.stderr
    assert invoke(shipping, "0.1.1", "--verify-draft").returncode == 0
    assert len(uploads(shipping)) == 1


def test_dmg_release_seals_final_stapled_bytes_and_uploads_exactly_three(modern_shipping):
    root, _, _ = modern_shipping
    old = root / "dist/release/v0.1.3/immutable-marker"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"previous release stays unchanged")
    result = invoke(modern_shipping, "0.1.4")
    assert result.returncode == 0, result.stderr + result.stdout
    directory = root / "dist/release/v0.1.4"
    sealed = json.loads((directory / "release.json").read_text())
    created = json.loads((directory / "installer-create.json").read_text())
    assert sealed["schema_version"] == 2
    assert set(sealed["assets"]) == {"narumi-0.1.4.zip", "appcast.xml", "narumi-0.1.4.dmg"}
    assert {p.name for p in (directory / "feed").iterdir()} == {"narumi-0.1.4.zip", "appcast.xml"}
    assert {p.name for p in (directory / "installer").iterdir()} == {"narumi-0.1.4.dmg"}
    assert (directory / "installer/narumi-0.1.4.dmg").read_bytes().endswith(b":signed:stapled")
    assert sealed["assets"]["narumi-0.1.4.dmg"]["sha256"] != created["sha256"]
    assert sealed["installer"]["notarization"] == {
        "id": "22222222-2222-2222-2222-222222222222",
        "status": "Accepted",
    }
    assert old.read_bytes() == b"previous release stays unchanged"
    assert len(uploads(modern_shipping)) == 1
    assert [Path(p).name for p in uploads(modern_shipping)[0][-3:]] == [
        "narumi-0.1.4.zip",
        "appcast.xml",
        "narumi-0.1.4.dmg",
    ]
    assert "実機検証後" not in result.stdout
    assert not any(name == "gh" and "edit" in args for name, args in calls(modern_shipping))
    assert invoke(modern_shipping, "0.1.4", "--verify-draft").returncode == 0


def test_last_legacy_release_does_not_create_or_accept_a_dmg(shipping):
    root, env, _ = shipping
    env["FAKE_RELEASE_VERSION"] = "0.1.3"
    (root / "VERSION").write_text("0.1.3\n")
    (root / "CHANGELOG.md").write_text("# Changelog\n\n## 0.1.3\n\nLegacy release.\n")
    result = invoke(shipping, "0.1.3")
    assert result.returncode == 0, result.stderr + result.stdout
    sealed = json.loads((root / "dist/release/v0.1.3/release.json").read_text())
    assert sealed["schema_version"] == 1 and "installer" not in sealed
    assert set(sealed["assets"]) == {"narumi-0.1.3.zip", "appcast.xml"}
    assert not any(name == "release_dmg.py" for name, _ in calls(shipping))


@pytest.mark.parametrize(
    "mode",
    [
        "dmg_create_failure",
        "dmg_signing_failure",
        "dmg_notary_failure",
        "dmg_notary_rejected",
        "dmg_staple_failure",
        "dmg_verification_failure",
        "dmg_helper_wrong_hash",
    ],
)
def test_dmg_failures_never_upload_or_disclose_diagnostics(modern_shipping, mode):
    modern_shipping[1]["FAKE_RELEASE_MODE"] = mode
    result = invoke(modern_shipping, "0.1.4")
    assert result.returncode != 0
    assert not uploads(modern_shipping)
    assert "fixture-secret" not in result.stdout + result.stderr
    directory = modern_shipping[0] / "dist/release/v0.1.4"
    if mode == "dmg_notary_failure":
        assert b"fixture-secret" in (directory / "installer-notary-error.log").read_bytes()
    if mode == "dmg_verification_failure":
        diagnostics = list(directory.glob("dmg-verification-*.log"))
        assert len(diagnostics) == 1
        assert b"retained-mount diagnostic" in diagnostics[0].read_bytes()
        assert diagnostics[0].stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("change", ["schema", "dmg", "notarization"])
def test_sealed_dmg_changes_fail_before_mount(modern_shipping, change):
    assert invoke(modern_shipping, "0.1.4").returncode == 0
    directory = modern_shipping[0] / "dist/release/v0.1.4"
    if change == "schema":
        path = directory / "release.json"
        sealed = json.loads(path.read_text())
        sealed["schema_version"] = 1
        path.write_text(json.dumps(sealed))
    elif change == "dmg":
        path = directory / "installer/narumi-0.1.4.dmg"
        path.write_bytes(path.read_bytes() + b"changed")
    else:
        path = directory / "installer-notary-result.json"
        record = json.loads(path.read_text())
        record["id"] = "33333333-3333-3333-3333-333333333333"
        path.write_text(json.dumps(record))
    previous = [row for row in calls(modern_shipping) if row[0] == "release_dmg.py"]
    result = invoke(modern_shipping, "0.1.4", "--verify-draft")
    assert result.returncode != 0
    assert [row for row in calls(modern_shipping) if row[0] == "release_dmg.py"] == previous
    assert len(uploads(modern_shipping)) == 1


def test_remote_dmg_tampering_fails_without_replacing_uploaded_assets(modern_shipping):
    modern_shipping[1]["FAKE_RELEASE_MODE"] = "remote_dmg_mutation"
    result = invoke(modern_shipping, "0.1.4")
    assert result.returncode != 0 and "SHA256" in result.stderr
    assert len(uploads(modern_shipping)) == 1
    assert not any(name == "gh" and "delete" in args for name, args in calls(modern_shipping))


def test_explicit_release_environment_and_sparkle_account(shipping):
    _, env, env_file = shipping
    env_file.write_text("export SPARKLE_KEY_ACCOUNT=fixture.narumi\nprintf 'fixture-secret'\n")
    result = invoke(shipping, "0.1.1")
    assert result.returncode == 0, result.stderr + result.stdout
    assert "fixture-secret" not in result.stdout + result.stderr
    for name, args in calls(shipping):
        if name in ("generate_keys", "sign_update", "generate_appcast"):
            assert args[args.index("--account") + 1] == "fixture.narumi"
    assert "SPARKLE_BIN" not in env


@pytest.mark.parametrize(
    "mode",
    [
        "wrong_branch",
        "dirty",
        "stale",
        "fork_main",
        "tag_exists",
        "canonical_tag_exists",
        "local_tag_exists",
        "gh_error",
        "existing_release",
        "wrong_key",
        "dirty_after_build",
        "head_after_build",
        "wrong_plist",
        "inventory_failure",
        "notary_rejected",
        "lost_ticket",
        "tampered_feed",
        "verification_failure",
        "tag_race",
    ],
)
def test_release_failures_never_upload(shipping, mode):
    shipping[1]["FAKE_RELEASE_MODE"] = mode
    result = invoke(shipping, "0.1.1")
    assert result.returncode != 0, result.stdout
    assert not uploads(shipping)


@pytest.mark.parametrize("version", ["../escape", "01.1.1", "1.2", "1.2.3-rc.1", "1.2.3+build"])
def test_invalid_version_rejected_before_tools(shipping, version):
    result = invoke(shipping, version)
    assert result.returncode != 0
    assert calls(shipping) == []


def test_existing_release_directory_is_never_overwritten(shipping):
    directory = shipping[0] / "dist/release/v0.1.1"
    directory.mkdir(parents=True)
    marker = directory / "appcast.xml"
    marker.write_text("keep")
    result = invoke(shipping, "0.1.1")
    assert result.returncode != 0
    assert marker.read_text() == "keep"
    assert not uploads(shipping)


def test_changed_remote_assets_rejected_without_replacement(shipping):
    shipping[1]["FAKE_RELEASE_MODE"] = "remote_mutation"
    result = invoke(shipping, "0.1.1")
    assert result.returncode != 0
    assert "SHA256" in result.stderr
    assert len(uploads(shipping)) == 1
    assert not any(name == "gh" and "delete" in args for name, args in calls(shipping))


def test_draft_local_tampering_and_remote_commit_mismatch(shipping):
    assert invoke(shipping, "0.1.1").returncode == 0
    remote = shipping[0] / "fake-state/release.json"
    document = json.loads(remote.read_text())
    document["target_commitish"] = "2" * 40
    remote.write_text(json.dumps(document))
    result = invoke(shipping, "0.1.1", "--verify-draft")
    assert result.returncode != 0 and "commit" in result.stderr
    feed = shipping[0] / "dist/release/v0.1.1/feed/appcast.xml"
    feed.write_bytes(feed.read_bytes() + b"\n")
    result = invoke(shipping, "0.1.1", "--verify-draft")
    assert result.returncode != 0 and "SHA256" in result.stderr
    assert len(uploads(shipping)) == 1


def test_rotation_flag_rejected_without_keychain_access(shipping):
    result = invoke(shipping, "--allow-pubkey-rotation", script="check-updater-key-policy.sh")
    assert result.returncode != 0
    assert calls(shipping) == []


def test_explicit_missing_environment_is_not_ignored(shipping):
    shipping[1]["NARUMI_RELEASE_ENV"] = str(shipping[0] / "missing.env")
    assert invoke(shipping, "0.1.1").returncode != 0
    assert calls(shipping) == []
