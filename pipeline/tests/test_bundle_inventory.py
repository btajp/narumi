"""Distribution inventory rejects unexpected local files before shipping."""

import hashlib
import json
import plistlib
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest

from .bundle_artifact_fixtures import (
    CODEX_VERSION,
    PROMPTS,
    ROOT,
    app_zip,
    arm64_macho,
    make_app,
    replace_wheel,
    run_inventory,
    tracked_list,
    wheel_bytes,
    write_file,
)


@pytest.mark.parametrize("archive", [False, True])
def test_inventory_accepts_runtime_and_tracked_prompts(tmp_path: Path, archive: bool):
    app = make_app(tmp_path)
    tracked = tracked_list(tmp_path)
    path = app_zip(app, tmp_path / "narumi.zip") if archive else app
    command = "check-zip" if archive else "check-app"
    result = run_inventory(command, path, "--require-runtime", "--tracked-sources", tracked)
    assert result.returncode == 0, result.stderr
    inventory = json.loads(result.stdout)
    assert inventory["runtime"] is True
    files = {entry["path"]: entry for entry in inventory["entries"]}
    assert files["Contents/Resources/runtime/uv"]["sha256"]
    assert files[f"Contents/Resources/runtime/codex/{CODEX_VERSION}/codex"]["sha256"]
    assert files["Contents/Resources/runtime/licenses/openai-codex-Apache-2.0.txt"]["size"]
    assert files["Contents/Resources/runtime/licenses/openai-codex-NOTICE.txt"]["size"]
    assert all(not path.startswith("/") for path in files)


def test_repository_wheels_match_tracked_package_sources(tmp_path: Path):
    """Exercise real package data so matching synthetic allowlists cannot hide omissions."""
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--", "pipeline/src/narumi", "server/src/narumi_server"],
        cwd=ROOT,
        capture_output=True,
        check=True,
        timeout=20,
    )
    source_list = write_file(tmp_path / "tracked-sources.nul", tracked.stdout)
    wheels = tmp_path / "wheels"
    for package in ("narumi", "narumi-server"):
        build = subprocess.run(
            ["uv", "build", "--wheel", "--package", package, "--out-dir", str(wheels)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=120,
        )
        assert build.returncode == 0, build.stderr
    result = run_inventory(
        "copy-wheels", wheels, tmp_path / "runtime-wheels", "--tracked-sources", source_list
    )
    assert result.returncode == 0, result.stderr
    assert len(json.loads(result.stdout)["files"]) == 2


@pytest.mark.parametrize("archive", [False, True])
def test_inventory_requires_keychain_helper(tmp_path: Path, archive: bool):
    app = make_app(tmp_path)
    (app / "Contents/MacOS/narumi-keychain").unlink()
    path = app_zip(app, tmp_path / "narumi.zip") if archive else app
    result = run_inventory("check-zip" if archive else "check-app", path)
    assert result.returncode == 1
    assert "narumi-keychain" in result.stderr


def test_repo_mode_is_only_accepted_without_require_runtime(tmp_path: Path):
    app = make_app(tmp_path, runtime=False)
    result = run_inventory("check-app", app)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["runtime"] is False
    result = run_inventory("check-app", app, "--require-runtime")
    assert result.returncode == 1
    assert "missing its bundled runtime" in result.stderr


@pytest.mark.parametrize(
    "relative",
    [
        "local-work/meeting-notes.md",
        "Contents/Resources/.env",
        "Contents/Resources/runtime/private-key.pem",
        "Contents/Resources/runtime/contracts/tools/private_note.json",
        "Contents/Resources/runtime/contracts/README.md",
        "Contents/Resources/runtime/wheels/.gitignore",
        "Contents/Resources/runtime/wheels/extra.whl",
    ],
)
@pytest.mark.parametrize("archive", [False, True])
def test_inventory_rejects_unlisted_files(tmp_path: Path, relative: str, archive: bool):
    app = make_app(tmp_path)
    write_file(app / relative, "fake local-only data")
    path = app_zip(app, tmp_path / "narumi.zip") if archive else app
    result = run_inventory("check-zip" if archive else "check-app", path)
    assert result.returncode == 1
    assert "unexpected app member" in result.stderr
    assert "fake local-only data" not in result.stderr


@pytest.mark.parametrize(
    "relative",
    [
        "narumi/private_notes.py",
        "narumi/private_notes.md",
        "narumi/contracts/_generated/leaked.py",
        "narumi_server/local_settings.py",
    ],
)
def test_wheel_rejects_untracked_sources_even_with_updated_hash(tmp_path: Path, relative: str):
    app = make_app(tmp_path)
    package = relative.split("/")[0]
    replace_wheel(app, package, wheel_bytes(package, extra={relative: b"local-only fixture"}))
    result = run_inventory("check-app", app, "--tracked-sources", tracked_list(tmp_path))
    assert result.returncode == 1
    assert "unexpected wheel member" in result.stderr or "does not match tracked" in result.stderr
    assert "local-only fixture" not in result.stderr


@pytest.mark.parametrize("prompt", sorted(PROMPTS))
def test_wheel_requires_each_runtime_prompt(tmp_path: Path, prompt: str):
    app = make_app(tmp_path)
    replace_wheel(app, "narumi", wheel_bytes("narumi", omit={prompt}))
    result = run_inventory("check-app", app)
    assert result.returncode == 1
    assert "missing required prompt" in result.stderr


def test_runtime_hash_mismatch_is_rejected(tmp_path: Path):
    app = make_app(tmp_path)
    write_file(app / "Contents/Resources/runtime/requirements.txt", "altered fixture\n")
    result = run_inventory("check-app", app)
    assert result.returncode == 1
    assert "hash mismatch" in result.stderr


@pytest.mark.parametrize(
    "relative",
    [
        f"Contents/Resources/runtime/codex/{CODEX_VERSION}/codex",
        "Contents/Resources/runtime/licenses/openai-codex-Apache-2.0.txt",
        "Contents/Resources/runtime/licenses/openai-codex-NOTICE.txt",
    ],
)
def test_codex_runtime_file_mismatch_is_rejected(tmp_path: Path, relative: str):
    app = make_app(tmp_path)
    with (app / relative).open("ab") as output:
        output.write(b"altered")
    result = run_inventory("check-app", app)
    assert result.returncode == 1
    assert "size mismatch" in result.stderr


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("binary", "publisher_team_id", "NOTOPENAI00"),
        ("binary", "version_output", "codex-cli 0.150.0"),
        ("artifact", "entry", "nested/codex"),
        ("license", "spdx", "MIT"),
    ],
)
def test_codex_manifest_metadata_is_fail_closed(
    tmp_path: Path, section: str, field: str, value: str
):
    app = make_app(tmp_path)
    manifest_path = app / "Contents/Resources/runtime/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["codex"][section][field] = value
    manifest_path.write_text(json.dumps(manifest))
    result = run_inventory("check-app", app)
    assert result.returncode == 1
    assert "Codex" in result.stderr


def test_codex_binary_must_be_thin_arm64_even_with_updated_metadata(tmp_path: Path):
    app = make_app(tmp_path)
    binary_path = app / f"Contents/Resources/runtime/codex/{CODEX_VERSION}/codex"
    x86_64 = bytearray(arm64_macho(b"wrong architecture"))
    x86_64[4:8] = b"\x07\x00\x00\x01"
    binary_path.write_bytes(x86_64)
    manifest_path = app / "Contents/Resources/runtime/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["codex"]["binary"]["size"] = len(x86_64)
    manifest["codex"]["binary"]["sha256"] = hashlib.sha256(x86_64).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    result = run_inventory("check-app", app)
    assert result.returncode == 1
    assert "arm64 Mach-O" in result.stderr


def test_runtime_app_executables_must_be_arm64(tmp_path: Path):
    app = make_app(tmp_path)
    write_file(app / "Contents/MacOS/narumi-recorder", b"not a Mach-O")
    result = run_inventory("check-app", app)
    assert result.returncode == 1
    assert "arm64 Mach-O" in result.stderr


@pytest.mark.parametrize("invalid", [b"not-nul-terminated", b"../private.py\0", b"\0"])
def test_tracked_list_rejects_invalid_input(tmp_path: Path, invalid: bytes):
    app = make_app(tmp_path)
    tracked = write_file(tmp_path / "bad.nul", invalid)
    result = run_inventory("check-app", app, "--tracked-sources", tracked)
    assert result.returncode == 1
    assert "bundle-inventory:" in result.stderr


@pytest.mark.parametrize(
    "name",
    [
        "../private.txt",
        "/tmp/private.txt",
        "narumi.app/Contents/../private.txt",
        "narumi.app//Contents/private.txt",
        r"narumi.app\Contents\private.txt",
        "__MACOSX/._narumi.app",
        "narumi.app/Contents/MacOS/NarumiMenuBar/child.py",
    ],
)
def test_zip_rejects_unsafe_member_paths(tmp_path: Path, name: str):
    archive = app_zip(make_app(tmp_path), tmp_path / "narumi.zip")
    with zipfile.ZipFile(archive, "a") as output:
        output.writestr(name, b"untrusted fixture")
    result = run_inventory("check-zip", archive)
    assert result.returncode == 1
    assert any(
        error in result.stderr
        for error in ("unsafe artifact path", "unexpected archive root", "non-directory")
    )


def test_zip_rejects_duplicate_paths(tmp_path: Path):
    archive = app_zip(make_app(tmp_path), tmp_path / "narumi.zip")
    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(archive, "a") as output:
            output.writestr("narumi.app/Contents/PkgInfo", b"duplicate")
    result = run_inventory("check-zip", archive)
    assert result.returncode == 1
    assert "duplicate artifact path" in result.stderr


def test_zip_rejects_filenames_truncated_at_nul(tmp_path: Path):
    app = make_app(tmp_path, runtime=False)
    archive = tmp_path / "narumi.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for file in app.rglob("*"):
            if file.is_file():
                name = file.relative_to(app.parent).as_posix()
                output.write(file, name + "X" if name.endswith("/PkgInfo") else name)
    original = b"narumi.app/Contents/PkgInfoX"
    archive.write_bytes(archive.read_bytes().replace(original, original[:-1] + b"\0"))
    result = run_inventory("check-zip", archive)
    assert result.returncode == 1
    assert "filename normalization" in result.stderr


def test_zip_rejects_duplicate_root_directories(tmp_path: Path):
    archive = app_zip(make_app(tmp_path), tmp_path / "narumi.zip")
    with zipfile.ZipFile(archive, "a") as output:
        output.writestr("narumi.app/", b"")
        with pytest.warns(UserWarning, match="Duplicate name"):
            output.writestr("narumi.app/", b"")
    result = run_inventory("check-zip", archive)
    assert result.returncode == 1
    assert "duplicate archive root" in result.stderr


def test_wheel_rejects_path_traversal(tmp_path: Path):
    app = make_app(tmp_path)
    replace_wheel(app, "narumi", wheel_bytes("narumi", extra={"../private.txt": b"fixture"}))
    result = run_inventory("check-app", app)
    assert result.returncode == 1
    assert "unsafe artifact path" in result.stderr


@pytest.mark.parametrize("archive", [False, True])
def test_inventory_rejects_escape_symlink(tmp_path: Path, archive: bool):
    app = make_app(tmp_path)
    link = app / "Contents/Resources/secret"
    link.symlink_to("../../../../private.txt")
    path = app_zip(app, tmp_path / "narumi.zip") if archive else app
    result = run_inventory("check-zip" if archive else "check-app", path)
    assert result.returncode == 1
    assert "unsafe artifact path" in result.stderr


def test_zip_rejects_special_device_entry(tmp_path: Path):
    archive = app_zip(make_app(tmp_path), tmp_path / "narumi.zip")
    info = zipfile.ZipInfo("narumi.app/Contents/Resources/device")
    info.create_system = 3
    info.external_attr = (stat.S_IFCHR | 0o600) << 16
    with zipfile.ZipFile(archive, "a") as output:
        output.writestr(info, b"")
    result = run_inventory("check-zip", archive)
    assert result.returncode == 1
    assert "unsupported archive file type" in result.stderr


def test_info_plist_must_register_bundled_icon(tmp_path: Path):
    app = make_app(tmp_path)
    path = app / "Contents/Info.plist"
    plist = plistlib.loads(path.read_bytes())
    del plist["CFBundleIconFile"]
    path.write_bytes(plistlib.dumps(plist))
    result = run_inventory("check-app", app)
    assert result.returncode == 1
    assert "CFBundleIconFile" in result.stderr
