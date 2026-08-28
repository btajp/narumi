"""Exercise build-app assembly with isolated fake build/signing tools."""

import hashlib
import io
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tarfile
import textwrap
from pathlib import Path

import pytest

from .bundle_artifact_fixtures import (
    ROOT,
    VERSION,
    make_contracts,
    tracked_list,
    wheel_bytes,
    write_file,
)

TOOL_STUB = """
import json, os, pathlib, plistlib, shutil, sys
tool = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["NARUMI_TEST_LOG"], "a") as log:
    log.write(json.dumps({"tool": tool, "args": args}) + "\\n")
if tool == "plutil":
    with open(args[-1], "rb") as source:
        plistlib.load(source)
elif tool == "ditto":
    shutil.copytree(args[0], args[1], symlinks=True)
elif tool == "uv":
    if args[0] == "build":
        package = args[args.index("--package") + 1].replace("-", "_")
        directory = pathlib.Path(args[args.index("--out-dir") + 1])
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ".gitignore").write_text("*\\n")
        for source in pathlib.Path(os.environ["NARUMI_TEST_WHEELS"]).glob(package + "-*.whl"):
            shutil.copyfile(source, directory / source.name)
    elif args[0] == "export":
        print("pydantic==2.11.1")
        if "--extra" in args and "slides" in args:
            print("pillow==11.0.0")
    else:
        sys.exit("unexpected uv action")
elif tool not in ("swift", "codesign"):
    sys.exit("unexpected external tool: " + tool)
"""


@pytest.fixture
def build_fixture(tmp_path: Path):
    project = tmp_path / "fixture project"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(ROOT / "scripts/build-app.sh", scripts / "build-app.sh")
    for helper in (ROOT / "scripts").glob("bundle_*.py"):
        shutil.copyfile(helper, scripts / helper.name)
    write_file(project / "VERSION", VERSION)
    asset = project / "app/Assets/AppIcon.icns"
    asset.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "app/Assets/AppIcon.icns", asset)
    for name in ("NarumiMenuBar", "narumi-recorder"):
        write_file(project / "app/.build/release" / name, "#!/bin/sh\nexit 0\n").chmod(0o755)
    key = write_file(project / "app/sparkle-public-key.txt", "fixture-public-key")
    commands = project / "test-bin"
    commands.mkdir()
    stub = f"#!{sys.executable}\n" + textwrap.dedent(TOOL_STUB)
    for name in ("swift", "uv", "codesign", "plutil", "ditto", "curl", "git"):
        write_file(commands / name, stub).chmod(0o755)
    (commands / "python3").symlink_to(sys.executable)
    framework = project / "app/.build/artifacts/sparkle/macos/Sparkle.framework"
    for relative in (
        "Versions/B/Sparkle",
        "Versions/B/Autoupdate",
        "Versions/B/Resources/Info.plist",
        "Versions/B/Updater.app/Contents/Info.plist",
        "Versions/B/Updater.app/Contents/MacOS/Updater",
        "Versions/B/XPCServices/local-fixture.txt",
    ):
        write_file(framework / relative, "framework fixture")
    (framework / "Versions/Current").symlink_to("B", target_is_directory=True)
    (framework / "Sparkle").symlink_to("Versions/Current/Sparkle")
    (framework / "Resources").symlink_to("Versions/Current/Resources", target_is_directory=True)
    make_contracts(project / "contracts")
    write_file(project / "contracts/local-notes.md", "must not ship")
    wheels = project / "fixture-wheels"
    for package in ("narumi", "narumi_server"):
        write_file(wheels / f"{package}-{VERSION}-py3-none-any.whl", wheel_bytes(package))
    cache = project / "uv-cache"
    archive = cache / "0.12.6/uv-aarch64-apple-darwin.tar.gz"
    archive.parent.mkdir(parents=True)
    data = b"#!/bin/sh\nexit 0\n"
    with tarfile.open(archive, "w:gz") as output:
        member = tarfile.TarInfo("uv-aarch64-apple-darwin/uv")
        member.mode = 0o755
        member.size = len(data)
        output.addfile(member, io.BytesIO(data))
    lock = {
        "uv": {
            "version": "0.12.6",
            "artifact": archive.name,
            "url": "https://example.invalid/never-download",
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        }
    }
    write_file(scripts / "runtime.lock.json", json.dumps(lock))
    environment = {
        **{
            name: os.environ[name]
            for name in ("PATH", "LANG", "LC_ALL", "TMPDIR")
            if name in os.environ
        },
        "PATH": str(commands) + os.pathsep + os.environ["PATH"],
        "DIST_DIR": str(project / "output"),
        "SPARKLE_PUBLIC_KEY_FILE": str(key),
        "NARUMI_UV_CACHE_DIR": str(cache),
        "NARUMI_TRACKED_SOURCES": str(tracked_list(project)),
        "NARUMI_TEST_WHEELS": str(wheels),
        "NARUMI_TEST_LOG": str(project / "tools.jsonl"),
    }
    return project, environment


def run_build(project: Path, environment: dict, *options: str):
    return subprocess.run(
        [
            "/bin/bash" if sys.platform == "darwin" else "bash",
            str(project / "scripts/build-app.sh"),
            "--build-override",
            "101",
            *options,
        ],
        cwd=project,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
    )


def test_build_uses_system_shell_and_minimal_environment(build_fixture):
    project, environment = build_fixture
    assert (project / "scripts/build-app.sh").read_text().splitlines()[0] == "#!/bin/bash"
    assert set(environment) <= {
        "PATH",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "DIST_DIR",
        "SPARKLE_PUBLIC_KEY_FILE",
        "NARUMI_UV_CACHE_DIR",
        "NARUMI_TRACKED_SOURCES",
        "NARUMI_TEST_WHEELS",
        "NARUMI_TEST_LOG",
    }


@pytest.mark.parametrize(
    "options",
    [
        (),
        ("--skip-build",),
        ("--skip-build", "--release"),
        ("--skip-build", "--runtime"),
        ("--skip-build", "--release", "--runtime"),
    ],
)
def test_icon_is_registered_in_every_bundle_mode(build_fixture, options):
    project, environment = build_fixture
    release = "--release" in options
    environment["APPLE_SIGNING_IDENTITY"] = (
        "Developer ID Application: Fixture (ABCDE12345)" if release else "-"
    )
    result = run_build(project, environment, *options)
    assert result.returncode == 0, result.stdout + result.stderr
    app = project / "output/narumi.app"
    plist = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    assert plist["CFBundleIconFile"] == "AppIcon"
    assert (app / "Contents/Resources/AppIcon.icns").read_bytes() == (
        project / "app/Assets/AppIcon.icns"
    ).read_bytes()
    calls = [json.loads(line) for line in (project / "tools.jsonl").read_text().splitlines()]
    assert any(call["tool"] == "swift" for call in calls) == ("--skip-build" not in options)
    signing = [
        call["args"] for call in calls if call["tool"] == "codesign" and "--sign" in call["args"]
    ]
    assert signing[-1][-1] == str(app)
    for args in signing:
        assert ("--timestamp" in args) == release
        assert ("--options" in args and args[args.index("--options") + 1] == "runtime") == release
    if "--runtime" in options:
        runtime = app / "Contents/Resources/runtime"
        assert signing[0][-1] == str(runtime / "uv")
        assert {path.name for path in (runtime / "wheels").iterdir()} == {
            f"{package}-{VERSION}-py3-none-any.whl" for package in ("narumi", "narumi_server")
        }
        for call in calls:
            if call["tool"] == "uv" and call["args"][0] == "build":
                staging = Path(call["args"][call["args"].index("--out-dir") + 1])
                assert not staging.is_relative_to(app)
                assert not staging.exists()
        assert "pillow==11.0.0" in (runtime / "requirements.txt").read_text()
        assert not (runtime / "contracts/local-notes.md").exists()
        assert any(
            call["tool"] == "uv" and "export" in call["args"] and "slides" in call["args"]
            for call in calls
        )
    assert not any(call["tool"] in {"git", "curl"} for call in calls)


@pytest.mark.parametrize("bad_icon", [None, b"not an icon", b"icns\0\0\0\x08", b"icns\0\0\0\x10x"])
def test_missing_or_invalid_icon_preserves_existing_bundle(build_fixture, bad_icon):
    project, environment = build_fixture
    icon = project / "app/Assets/AppIcon.icns"
    if bad_icon is None:
        icon.unlink()
    else:
        icon.write_bytes(bad_icon)
    sentinel = write_file(project / "output/narumi.app/keep.txt", "keep existing app")
    result = run_build(project, environment, "--skip-build")
    assert result.returncode == 1
    assert sentinel.read_text() == "keep existing app"
    assert not (project / "tools.jsonl").exists()


@pytest.mark.parametrize(
    "identity", ["-", "", "Apple Development: Fixture", "Developer ID Installer: Fixture"]
)
def test_release_requires_developer_id_application(build_fixture, identity):
    project, environment = build_fixture
    environment["APPLE_SIGNING_IDENTITY"] = identity
    sentinel = write_file(project / "output/narumi.app/keep.txt", "keep existing app")
    result = run_build(project, environment, "--skip-build", "--release")
    assert result.returncode == 1
    assert "Developer ID Application" in result.stderr
    assert sentinel.read_text() == "keep existing app"
    assert not (project / "tools.jsonl").exists()
