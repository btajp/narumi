"""Recording permissions are exact, isolated, and checked after signing."""

import plistlib
import re
import shutil
import subprocess
import sys

import pytest

from .bundle_artifact_fixtures import ROOT, write_file
from .test_app_icon_bundle import build_fixture as build_fixture
from .test_app_icon_bundle import run_build

AUDIO_INPUT = "com.apple.security.device.audio-input"
RECORDING_ENTITLEMENTS = {AUDIO_INPUT: True}


def test_recording_entitlement_source_is_minimal():
    source = plistlib.loads((ROOT / "app/recording.entitlements.plist").read_bytes())
    assert set(source) == {AUDIO_INPUT}
    assert source[AUDIO_INPUT] is True


@pytest.mark.parametrize(
    "invalid_source",
    [
        pytest.param(None, id="missing"),
        pytest.param(b"", id="empty"),
        pytest.param(b"not a plist", id="malformed"),
        pytest.param(plistlib.dumps({}), id="missing-key"),
        pytest.param(plistlib.dumps({AUDIO_INPUT: False}), id="false"),
        pytest.param(plistlib.dumps({AUDIO_INPUT: "true"}), id="string"),
        pytest.param(plistlib.dumps({AUDIO_INPUT: 1}), id="integer"),
        pytest.param(plistlib.dumps([]), id="not-dictionary"),
        pytest.param(
            plistlib.dumps({AUDIO_INPUT: True, "com.apple.security.get-task-allow": True}),
            id="extra-permission",
        ),
    ],
)
def test_invalid_recording_entitlements_preserve_existing_app(build_fixture, invalid_source):
    project, environment = build_fixture
    source = project / "app/recording.entitlements.plist"
    if invalid_source is None:
        source.unlink()
    else:
        source.write_bytes(invalid_source)
    sentinel = write_file(project / "output/narumi.app/keep.txt", "keep existing app")

    result = run_build(project, environment, "--skip-build")

    assert result.returncode == 1
    assert "entitlement" in result.stderr
    assert sentinel.read_text() == "keep existing app"
    assert not (project / "tools.jsonl").exists()


def test_symlink_recording_entitlements_preserve_existing_app(build_fixture):
    project, environment = build_fixture
    source = project / "app/recording.entitlements.plist"
    alternate = source.with_name("alternate.plist")
    source.rename(alternate)
    source.symlink_to(alternate)
    sentinel = write_file(project / "output/narumi.app/keep.txt", "keep existing app")

    result = run_build(project, environment, "--skip-build")

    assert result.returncode == 1
    assert "entitlement" in result.stderr
    assert sentinel.read_text() == "keep existing app"
    assert not (project / "tools.jsonl").exists()


@pytest.mark.parametrize("target", ["", "Contents/MacOS/narumi-recorder"], ids=["app", "recorder"])
@pytest.mark.parametrize(
    "fault",
    [
        "empty",
        "missing",
        "false",
        "string",
        "integer",
        "extra",
        "not-dictionary",
        "invalid",
        "display-error",
        "verify-error",
    ],
)
def test_invalid_embedded_entitlements_fail_build(build_fixture, target, fault):
    project, environment = build_fixture
    environment["NARUMI_TEST_CODESIGN_FAULT_TARGET"] = str(project / "output/narumi.app" / target)
    environment["NARUMI_TEST_CODESIGN_FAULT"] = fault

    result = run_build(project, environment, "--skip-build")

    assert result.returncode != 0
    assert "built:" not in result.stdout
    if fault != "verify-error":
        assert "署名済みの録音用 entitlement" in result.stderr
    else:
        assert "invalid signature" in result.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS codesign and Mach-O")
def test_real_hardened_signatures_embed_recording_entitlements(build_fixture):
    """Sign temporary no-op binaries, without launching an app or requesting permission."""
    project, environment = build_fixture
    executable = project / "no-op"
    compile_result = subprocess.run(
        ["/usr/bin/xcrun", "clang", "-x", "c", "-", "-o", str(executable)],
        input="int main(void) { return 0; }\n",
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert compile_result.returncode == 0, compile_result.stderr
    for name in ("NarumiMenuBar", "narumi-recorder", "narumi-keychain"):
        shutil.copyfile(executable, project / "app/.build/release" / name)
    # The synthetic Sparkle fixture is not Mach-O; fake tests cover its signing calls.
    (project / "app/.build/artifacts").rename(project / "unused-artifacts")
    write_file(
        project / "test-bin/codesign",
        f"#!{sys.executable}\n"
        "import os, sys\n"
        'flags = ["--options", "runtime"] if "--sign" in sys.argv else []\n'
        'os.execv("/usr/bin/codesign", ["codesign", *flags, *sys.argv[1:]])\n',
    ).chmod(0o755)

    result = run_build(project, environment, "--skip-build")

    assert result.returncode == 0, result.stdout + result.stderr
    app = project / "output/narumi.app"
    for target in (app, app / "Contents/MacOS/narumi-recorder"):
        verification = subprocess.run(
            ["/usr/bin/codesign", "--verify", "--strict", str(target)],
            env=environment,
            capture_output=True,
            timeout=20,
        )
        assert verification.returncode == 0, verification.stderr.decode()
        signature = subprocess.run(
            [
                "/usr/bin/codesign",
                "--display",
                "--verbose=4",
                "--entitlements",
                "-",
                "--xml",
                str(target),
            ],
            env=environment,
            capture_output=True,
            timeout=20,
        )
        assert signature.returncode == 0, signature.stderr.decode()
        entitlements = plistlib.loads(signature.stdout)
        assert set(entitlements) == {AUDIO_INPUT}
        assert entitlements[AUDIO_INPUT] is True
        flags = re.search(rb"flags=0x([0-9a-fA-F]+)", signature.stderr)
        assert flags is not None, signature.stderr.decode()
        assert int(flags[1], 16) & 0x10000  # CS_RUNTIME
