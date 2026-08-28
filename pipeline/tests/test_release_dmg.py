"""DMG packaging and fail-closed mount cleanup with fake macOS tools only."""

from __future__ import annotations

import importlib.util
import os
import plistlib
import shutil
import stat
import zipfile
from pathlib import Path

import pytest

from .bundle_artifact_fixtures import make_app, tracked_list

ROOT = Path(__file__).resolve().parents[2]


def complete_zip(app: Path, destination: Path, *, root=True, unix=True) -> Path:
    with zipfile.ZipFile(destination, "w") as zipped:
        for path in ([app] if root else []) + sorted(app.rglob("*")):
            name = path.relative_to(app.parent).as_posix()
            mode = path.lstat().st_mode
            entry = zipfile.ZipInfo(name + ("/" if path.is_dir() and not path.is_symlink() else ""))
            entry.create_system = 3 if unix else 0
            entry.external_attr = mode << 16
            content = os.readlink(path).encode() if path.is_symlink() else b""
            if path.is_file() and not path.is_symlink():
                content = path.read_bytes()
            zipped.writestr(entry, content)
    return destination


class FakeTools:
    def __init__(self, module, app: Path):
        self.module = module
        self.app = app
        self.calls = []
        self.mountpoint = None
        self.image = None
        self.mounted = False
        self.mode = ""
        self.mutate = lambda mountpoint: None
        self.create_items = None

    def entities(self):
        return [
            {"dev-entry": "/dev/disk991"},
            {"dev-entry": "/dev/disk991s1", "mount-point": str(self.mountpoint)},
        ]

    def __call__(self, *command):
        self.calls.append(command)
        if command[:3] == ("ditto", "-x", "-k"):
            shutil.copytree(self.app, Path(command[-1]) / "narumi.app", symlinks=True)
            if self.mode == "extract_mode":
                (Path(command[-1]) / "narumi.app").chmod(0o711)
        elif command[:2] == ("hdiutil", "create"):
            stage = Path(command[command.index("-srcfolder") + 1])
            self.create_items = {p.name for p in stage.iterdir()}
            assert (stage / "Applications").is_symlink()
            assert os.readlink(stage / "Applications") == "/Applications"
            assert command[command.index("-format") + 1] == "UDZO"
            Path(command[-1]).write_bytes(b"unsigned fake DMG")
        elif command[:2] == ("hdiutil", "attach"):
            self.image = Path(command[-1])
            self.mountpoint = Path(command[command.index("-mountpoint") + 1])
            if self.mode in {"attach_before_mount", "unmounted_device"}:
                raise self.module.ReleaseError("attach failed")
            shutil.copytree(self.app, self.mountpoint / "narumi.app", symlinks=True)
            (self.mountpoint / "Applications").symlink_to("/Applications")
            self.mounted = True
            self.mutate(self.mountpoint)
            if self.mode in {"attach_after_mount", "foreign_image", "info_failure"}:
                raise self.module.ReleaseError("attach failed")
            if self.mode == "malformed_attach":
                return b"not a plist"
            entities = self.entities()
            if self.mode == "extra_volume":
                entities.append({"dev-entry": "/dev/disk991s2", "mount-point": "/Volumes/extra"})
            return plistlib.dumps({"system-entities": entities})
        elif command[:2] == ("hdiutil", "info"):
            if self.mode in {"info_failure", "unknown_info"}:
                raise self.module.ReleaseError("info failed")
            if self.mode == "malformed_info":
                return b"not a plist"
            image_path = (
                "/unrelated.dmg"
                if self.mode in {"foreign_image", "reused_device"}
                else str(self.image)
            )
            images = (
                [{"image-path": image_path, "system-entities": self.entities()}]
                if self.mounted
                else []
            )
            if self.mode == "unmounted_device":
                images = [
                    {"image-path": image_path, "system-entities": [{"dev-entry": "/dev/disk991"}]}
                ]
            elif self.mode == "lost_ownership":
                images = []
            elif self.mode == "changed_device":
                images[0]["system-entities"] = [
                    {"dev-entry": "/dev/disk992"},
                    {"dev-entry": "/dev/disk992s1", "mount-point": str(self.mountpoint)},
                ]
            elif self.mode == "changed_mountpoint":
                images[0]["system-entities"][1]["mount-point"] = "/Volumes/unrelated"
            return plistlib.dumps({"images": images})
        elif command[:2] == ("hdiutil", "detach"):
            assert command == ("hdiutil", "detach", "/dev/disk991")
            if self.mode == "detach_failure":
                raise self.module.ReleaseError("detach failed")
            for path in self.mountpoint.iterdir():
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            self.mounted = False
            if self.mode == "changed_dmg":
                self.image.write_bytes(b"changed DMG")
        elif command[:2] == ("hdiutil", "verify"):
            if self.mode == "bad_image":
                raise self.module.ReleaseError("image verification failed")
        elif command[0] == "codesign" or command[:3] == ("xcrun", "stapler", "validate"):
            if self.mode == "bad_dmg_signature" and command[-1].endswith(".dmg"):
                raise self.module.ReleaseError("signature verification failed")
            if self.mode == "bad_ticket" and command[0] == "xcrun":
                raise self.module.ReleaseError("ticket verification failed")
            if self.mode == "bad_app_signature" and self.mountpoint is not None:
                raise self.module.ReleaseError("app signature verification failed")
        else:
            raise AssertionError(f"unexpected tool: {command[0]}")
        return b""


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "narumi_release_dmg", ROOT / "scripts/release_dmg.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = make_app(tmp_path / "source")
    archive = complete_zip(app, tmp_path / "narumi.zip")
    dmg = tmp_path / "narumi.dmg"
    dmg.write_bytes(b"signed and stapled fake DMG")
    tracked = tracked_list(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    mounts = tmp_path / "independent-mounts"
    mounts.mkdir()
    original_mkdtemp = module.tempfile.mkdtemp

    def mkdtemp(*args, **kwargs):
        if kwargs.get("prefix") == "narumi-dmg-mount-":
            kwargs["dir"] = mounts
        return original_mkdtemp(*args, **kwargs)

    monkeypatch.setattr(module.tempfile, "mkdtemp", mkdtemp)
    tools = FakeTools(module, app)
    monkeypatch.setattr(module, "run", tools)

    def verify():
        return module.verify_dmg(
            archive, dmg, work, tracked, module.sha256(dmg), dmg.stat().st_size
        )

    return module, tools, app, archive, dmg, tracked, work, mounts, verify


def test_zip_inventory_seals_root_and_all_entry_modes(fixture):
    module, _, app, archive, _, tracked, *_ = fixture
    expected = module.zip_inventory(archive, tracked)
    assert expected == module.app_inventory(app, tracked)
    assert expected["app_root_mode"] == stat.S_IMODE(app.stat().st_mode)
    assert all("mode" in row for row in expected["entries"])
    before = module.inventory_hash(expected)
    app.chmod(0o711)
    assert module.inventory_hash(module.app_inventory(app, tracked)) != before


@pytest.mark.parametrize("mutation", ["missing_root", "non_unix"])
def test_zip_requires_explicit_root_and_unix_modes(fixture, mutation):
    module, tools, app, archive, _, tracked, *_, verify = fixture
    complete_zip(app, archive, root=mutation != "missing_root", unix=mutation != "non_unix")
    with pytest.raises(module.ReleaseError, match="ZIP"):
        module.zip_inventory(archive, tracked)
    with pytest.raises(module.ReleaseError):
        verify()
    assert not tools.calls


def test_create_uses_verified_zip_app_and_exact_outer_layout(fixture):
    module, tools, _, archive, _, tracked, work, *_ = fixture
    destination = work / "narumi-0.1.1.dmg"
    result = module.create_dmg(archive, destination, work, tracked)
    assert result == {"sha256": module.sha256(destination), "size": destination.stat().st_size}
    assert tools.create_items == {"narumi.app", "Applications"}
    assert list(work.iterdir()) == [destination]
    assert any(call[:2] == ("codesign", "--verify") for call in tools.calls)
    assert any(call[:3] == ("xcrun", "stapler", "validate") for call in tools.calls)


def test_create_refuses_overwrite(fixture):
    module, tools, _, archive, dmg, tracked, work, *_ = fixture
    before = dmg.read_bytes()
    with pytest.raises(module.ReleaseError, match="未使用"):
        module.create_dmg(archive, dmg, work, tracked)
    assert dmg.read_bytes() == before
    assert not tools.calls


def test_extraction_mode_drift_prevents_create_or_mount(fixture):
    module, tools, _, archive, _, tracked, work, *_, verify = fixture
    tools.mode = "extract_mode"
    with pytest.raises(module.ReleaseError, match="mode"):
        module.create_dmg(archive, work / "new.dmg", work, tracked)
    with pytest.raises(module.ReleaseError, match="mode"):
        verify()
    assert not any(
        call[:2] in {("hdiutil", "create"), ("hdiutil", "attach")} for call in tools.calls
    )
    assert not list(work.iterdir())


def test_verified_dmg_has_stable_content_mode_seal_and_safe_mount_flags(fixture):
    module, tools, _, archive, dmg, tracked, work, mounts, verify = fixture
    result = verify()
    assert result == {
        "sha256": module.sha256(dmg),
        "size": dmg.stat().st_size,
        "app_inventory_sha256": module.inventory_hash(module.zip_inventory(archive, tracked)),
    }
    attach = next(call for call in tools.calls if call[:2] == ("hdiutil", "attach"))
    assert {"-readonly", "-nobrowse", "-noautoopen", "-plist"}.issubset(attach)
    assert attach[attach.index("-owners") + 1] == "on"
    assert tools.calls[-2:] == [
        ("hdiutil", "info", "-plist"),
        ("hdiutil", "detach", "/dev/disk991"),
    ]
    assert not tools.mountpoint.exists()
    assert not list(work.iterdir())
    assert not list(mounts.iterdir())


@pytest.mark.parametrize("mutation", ["hash", "size", "invalid_hash", "zero", "symlink"])
def test_hash_and_length_are_verified_before_any_image_tool(fixture, mutation):
    module, tools, _, archive, dmg, tracked, work, *_ = fixture
    digest, size = module.sha256(dmg), dmg.stat().st_size
    if mutation == "hash":
        digest = "0" * 64
    elif mutation == "size":
        size += 1
    elif mutation == "invalid_hash":
        digest = "invalid"
    elif mutation == "zero":
        size = 0
    else:
        link = work / "linked.dmg"
        link.symlink_to(dmg)
        dmg = link
    with pytest.raises(module.ReleaseError):
        module.verify_dmg(archive, dmg, work, tracked, digest, size)
    assert not tools.calls


@pytest.mark.parametrize("mode", ["bad_image", "bad_dmg_signature", "bad_ticket"])
def test_invalid_signature_ticket_or_disk_image_is_never_mounted(fixture, mode):
    module, tools, *_, verify = fixture
    tools.mode = mode
    with pytest.raises(module.ReleaseError):
        verify()
    assert not any(call[:2] == ("hdiutil", "attach") for call in tools.calls)


@pytest.mark.parametrize("mutation", ["extra", "wrong_link", "plain_link", "app_link"])
def test_unexpected_outer_layout_is_rejected_and_detached(fixture, mutation):
    module, tools, app, *_, verify = fixture

    def mutate(mountpoint):
        if mutation == "extra":
            (mountpoint / ".DS_Store").write_bytes(b"unexpected")
        elif mutation == "app_link":
            shutil.rmtree(mountpoint / "narumi.app")
            (mountpoint / "narumi.app").symlink_to(app)
        else:
            link = mountpoint / "Applications"
            link.unlink()
            if mutation == "wrong_link":
                link.symlink_to("/tmp")
            else:
                link.write_text("/Applications")

    tools.mutate = mutate
    with pytest.raises((module.ReleaseError, module.InventoryError)):
        verify()
    assert not tools.mounted
    assert tools.calls[-1] == ("hdiutil", "detach", "/dev/disk991")
    assert not tools.mountpoint.exists()


@pytest.mark.parametrize("mutation", ["content", "root_mode", "directory_mode", "file_mode", "key"])
def test_app_content_and_permission_mode_mismatches_are_rejected(fixture, mutation):
    module, tools, *_, verify = fixture

    def mutate(mountpoint):
        app = mountpoint / "narumi.app"
        if mutation == "content":
            (app / "Contents/PkgInfo").write_bytes(b"different")
        elif mutation == "key":
            path = app / "Contents/Info.plist"
            info = plistlib.loads(path.read_bytes())
            info["SUPublicEDKey"] = "another key"
            path.write_bytes(plistlib.dumps(info))
        else:
            target = {
                "root_mode": app,
                "directory_mode": app / "Contents",
                "file_mode": app / "Contents/PkgInfo",
            }[mutation]
            target.chmod(0o711)

    tools.mutate = mutate
    with pytest.raises(module.ReleaseError, match="mode"):
        verify()
    assert not tools.mounted
    assert not tools.mountpoint.exists()


@pytest.mark.parametrize("mode", ["bad_app_signature", "extra_volume"])
def test_validation_failure_always_detaches_the_owned_device(fixture, mode):
    module, tools, *_, verify = fixture
    tools.mode = mode
    with pytest.raises(module.ReleaseError):
        verify()
    assert not tools.mounted
    assert tools.calls[-1] == ("hdiutil", "detach", "/dev/disk991")


@pytest.mark.parametrize("mode", ["attach_before_mount", "attach_after_mount", "malformed_attach"])
def test_failed_attach_uses_image_and_mountpoint_ownership_for_cleanup(fixture, mode):
    module, tools, *_, mounts, verify = fixture
    tools.mode = mode
    with pytest.raises(module.ReleaseError):
        verify()
    assert ("hdiutil", "info", "-plist") in tools.calls
    assert not tools.mounted
    assert not list(mounts.iterdir())
    detach = [call for call in tools.calls if call[:2] == ("hdiutil", "detach")]
    assert detach == (
        [] if mode == "attach_before_mount" else [("hdiutil", "detach", "/dev/disk991")]
    )


@pytest.mark.parametrize("mode", ["detach_failure", "foreign_image", "info_failure"])
def test_uncertain_detach_preserves_mountpoint_and_reports_its_location(fixture, mode):
    module, tools, _, _, _, _, work, mounts, verify = fixture
    tools.mode = mode
    with pytest.raises(module.ReleaseError, match="一時領域を保持") as caught:
        verify()
    assert str(tools.mountpoint.parent) in str(caught.value)
    assert tools.mountpoint.exists()
    assert (tools.mountpoint / "narumi.app/Contents/Info.plist").exists()
    assert tools.mountpoint.parent.parent == mounts
    assert not tools.mountpoint.is_relative_to(work)
    assert not list(work.iterdir())
    if mode != "detach_failure":
        assert not any(call[:2] == ("hdiutil", "detach") for call in tools.calls)


def test_post_validation_dmg_change_is_rejected(fixture):
    module, tools, *_, verify = fixture
    tools.mode = "changed_dmg"
    with pytest.raises(module.ReleaseError, match="SHA256"):
        verify()
    assert not tools.mountpoint.exists()


def test_attach_leftover_device_without_owned_mountpoint_is_not_detached_or_hidden(fixture):
    module, tools, *_, verify = fixture
    tools.mode = "unmounted_device"
    with pytest.raises(module.ReleaseError, match="一時領域を保持") as caught:
        verify()
    assert str(tools.mountpoint.parent) in str(caught.value)
    assert tools.mountpoint.exists()
    assert not any(call[:2] == ("hdiutil", "detach") for call in tools.calls)


@pytest.mark.parametrize(
    "mode",
    [
        "lost_ownership",
        "reused_device",
        "changed_device",
        "changed_mountpoint",
        "unknown_info",
        "malformed_info",
    ],
)
def test_successful_attach_must_still_own_the_same_device_immediately_before_detach(fixture, mode):
    module, tools, *_, verify = fixture
    tools.mode = mode
    with pytest.raises(module.ReleaseError, match="一時領域を保持") as caught:
        verify()
    assert str(tools.mountpoint.parent) in str(caught.value)
    assert tools.mountpoint.is_dir()
    assert tools.calls[-1] == ("hdiutil", "info", "-plist")
    assert not any(call[:2] == ("hdiutil", "detach") for call in tools.calls)
    assert any(
        call[:3] == ("xcrun", "stapler", "validate")
        and call[-1] == str(tools.mountpoint / "narumi.app")
        for call in tools.calls
    )


def test_tool_failures_do_not_expose_diagnostics(fixture, monkeypatch):
    module, *_ = fixture
    original = importlib.util.spec_from_file_location(
        "dmg_redaction", ROOT / "scripts/release_dmg.py"
    )
    fresh = importlib.util.module_from_spec(original)
    original.loader.exec_module(fresh)

    def fail(*args, **kwargs):
        raise module.subprocess.CalledProcessError(1, ["hdiutil", "secret"], stderr=b"secret")

    monkeypatch.setattr(fresh.subprocess, "run", fail)
    with pytest.raises(module.ReleaseError) as caught:
        fresh.run("hdiutil", "secret")
    assert "secret" not in str(caught.value)
