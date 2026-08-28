#!/usr/bin/env python3
"""Create an initial-install DMG and verify it against the exact release ZIP."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import stat
import subprocess
import sys
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path

import bundle_inventory
from bundle_paths import InventoryError, read_app, read_zip
from bundle_wheels import tracked_payloads
from release_artifacts import ReleaseError, require, sha256, version_tuple

DEVELOPER_ID = (
    "=anchor apple generic and "
    "certificate 1[field.1.2.840.113635.100.6.2.6] exists and "
    "certificate leaf[field.1.2.840.113635.100.6.1.13] exists"
)
DEVICE = re.compile(r"(/dev/disk[0-9]+)(?:s[0-9]+)*", re.ASCII)


def run(*command: str) -> bytes:
    """Do not disclose tool arguments or diagnostic output in release logs."""
    try:
        return subprocess.run(command, check=True, capture_output=True, timeout=300).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleaseError(f"{Path(command[0]).name} の DMG 処理に失敗しました") from exc


def regular_file(path: Path) -> None:
    require(path.is_file() and not path.is_symlink(), "DMG/ZIP は通常ファイルが必要です")


def work_directory(path: Path) -> Path:
    require(
        path.is_absolute() and path.is_dir() and not path.is_symlink(),
        "DMG 作業先は既存の絶対パスのディレクトリが必要です",
    )
    return path.resolve()


def zip_inventory(archive: Path, tracked: Path) -> dict:
    """ZIP Unix modes, including its explicit app root, are part of the seal."""
    regular_file(archive)
    with zipfile.ZipFile(archive) as zipped:
        result = bundle_inventory.inspect(
            read_zip(zipped, app_root=True),
            require_runtime=True,
            tracked=tracked_payloads(tracked),
        )
        roots = [entry for entry in zipped.infolist() if entry.filename == "narumi.app/"]
        require(len(roots) == 1, "ZIP に app ルートの mode 情報がありません")
        modes = {}
        for entry in zipped.infolist():
            mode = entry.external_attr >> 16
            require(entry.create_system == 3 and stat.S_IFMT(mode), "ZIP の Unix mode が不正です")
            relative = entry.filename.removeprefix("narumi.app/").rstrip("/")
            modes[relative] = stat.S_IMODE(mode)
        require(stat.S_ISDIR(roots[0].external_attr >> 16), "ZIP の app ルートが不正です")
        result["app_root_mode"] = modes.pop("")
        require(
            set(modes) == {row["path"] for row in result["entries"]},
            "ZIP の mode 一覧が inventory と不一致です",
        )
        for row in result["entries"]:
            row["mode"] = modes[row["path"]]
        return result


def app_inventory(app: Path, tracked: Path) -> dict:
    result = bundle_inventory.inspect(
        read_app(app), require_runtime=True, tracked=tracked_payloads(tracked)
    )
    result["app_root_mode"] = stat.S_IMODE(app.lstat().st_mode)
    for row in result["entries"]:
        row["mode"] = stat.S_IMODE((app / row["path"]).lstat().st_mode)
    return result


def inventory_hash(inventory: dict) -> str:
    content = json.dumps(inventory, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode()).hexdigest()


def verify_app(app: Path) -> None:
    run("codesign", "--verify", "--deep", "--strict", "-R", DEVELOPER_ID, str(app))
    run("xcrun", "stapler", "validate", str(app))


@contextmanager
def extracted_app(archive: Path, work: Path, tracked: Path, expected: dict):
    # This directory never contains a mountpoint; only validated ZIP members are extracted.
    with tempfile.TemporaryDirectory(prefix="narumi-dmg-source-", dir=work) as temporary:
        directory = Path(temporary)
        run("ditto", "-x", "-k", str(archive.resolve()), str(directory))
        require(
            {entry.name for entry in directory.iterdir()} == {"narumi.app"},
            "展開した ZIP に app 以外の項目があります",
        )
        app = directory / "narumi.app"
        require(app_inventory(app, tracked) == expected, "ZIP 展開後の内容または mode が不一致です")
        verify_app(app)
        yield directory, app


def create_dmg(archive: Path, output: Path, work: Path, tracked: Path) -> dict:
    work = work_directory(work)
    require(
        output.is_absolute()
        and output.suffix == ".dmg"
        and output.parent.is_dir()
        and not output.parent.is_symlink()
        and not output.exists()
        and not output.is_symlink(),
        "DMG 出力先は未使用の絶対パスが必要です",
    )
    expected = zip_inventory(archive, tracked)
    with extracted_app(archive, work, tracked, expected) as (directory, app):
        version = plistlib.loads((app / "Contents/Info.plist").read_bytes())[
            "CFBundleShortVersionString"
        ]
        version_tuple(version)
        (directory / "Applications").symlink_to("/Applications", target_is_directory=True)
        run(
            "hdiutil",
            "create",
            "-srcfolder",
            str(directory),
            "-volname",
            f"narumi {version}",
            "-fs",
            "HFS+",
            "-format",
            "UDZO",
            "-nospotlight",
            "-noskipunreadable",
            str(output),
        )
    regular_file(output)
    return {"sha256": sha256(output), "size": output.stat().st_size}


def plist_object(content: bytes) -> dict:
    require(len(content) <= 2 * 1024 * 1024, "hdiutil の応答が大きすぎます")
    try:
        result = plistlib.loads(content)
    except (ValueError, plistlib.InvalidFileException) as exc:
        raise ReleaseError("hdiutil の plist 応答が不正です") from exc
    require(isinstance(result, dict), "hdiutil の応答が辞書ではありません")
    return result


def owned_device(entities: object, mountpoint: Path) -> str | None:
    require(
        isinstance(entities, list) and all(isinstance(entry, dict) for entry in entities),
        "hdiutil の device 一覧が不正です",
    )
    matches = [entry for entry in entities if entry.get("mount-point") == str(mountpoint)]
    require(len(matches) <= 1, "hdiutil の mountpoint が重複しています")
    if not matches:
        return None
    match = DEVICE.fullmatch(str(matches[0].get("dev-entry", "")))
    require(match, "所有する DMG device を特定できません")
    assert match is not None
    device = match[1]
    require(
        any(entry.get("dev-entry") == device for entry in entities),
        "所有する DMG の root device がありません",
    )
    return device


def recovery_device(dmg: Path, mountpoint: Path) -> str | None:
    """Re-query ownership, including a failed attach that may still have mounted."""
    images = plist_object(run("hdiutil", "info", "-plist")).get("images")
    require(
        isinstance(images, list) and all(isinstance(image, dict) for image in images),
        "hdiutil の image 一覧が不正です",
    )
    matches = []
    for image in images:
        device = owned_device(image.get("system-entities"), mountpoint)
        if device is not None:
            require(image.get("image-path") == str(dmg), "DMG mountpoint の所有元が不一致です")
            matches.append(device)
        else:
            require(
                image.get("image-path") != str(dmg),
                "DMG device の残存がありますが、今回の mountpoint と対応付けられません",
            )
    require(len(matches) <= 1, "DMG mountpoint の所有元が重複しています")
    return matches[0] if matches else None


@contextmanager
def mounted_dmg(dmg: Path):
    # Never nest this under a caller's TemporaryDirectory: failed detach must preserve it.
    directory = Path(tempfile.mkdtemp(prefix="narumi-dmg-mount-")).resolve()
    mountpoint = directory / "volume"
    mountpoint.mkdir(mode=0o700)
    device = None
    try:
        response = plist_object(
            run(
                "hdiutil",
                "attach",
                "-readonly",
                "-nobrowse",
                "-noautoopen",
                "-owners",
                "on",
                "-mountpoint",
                str(mountpoint),
                "-plist",
                str(dmg),
            )
        )
        entities = response.get("system-entities")
        device = owned_device(entities, mountpoint)
        require(device, "要求した DMG mountpoint がありません")
        require(
            all(
                not entry.get("mount-point") or entry["mount-point"] == str(mountpoint)
                for entry in entities
            ),
            "DMG に予期しない追加 volume があります",
        )
        require(mountpoint.is_dir() and not mountpoint.is_symlink(), "DMG mountpoint が不正です")
        yield mountpoint
    finally:
        try:
            current_device = recovery_device(dmg, mountpoint)
            if device is None:
                device = current_device
            else:
                require(
                    current_device == device, "DMG device の所有状態が attach 時から変わっています"
                )
            if device is not None:
                run("hdiutil", "detach", device)
            require(not os.path.ismount(mountpoint), "DMG がまだマウントされています")
            # Only empty, unmounted directories are removed, never a recursive mount cleanup.
            mountpoint.rmdir()
            directory.rmdir()
        except (OSError, ReleaseError) as exc:
            raise ReleaseError(
                f"DMG の解除・後片付けを確認できません。一時領域を保持しました: {directory}"
            ) from exc


def check_dmg_hash(dmg: Path, expected_hash: str, expected_size: int) -> None:
    regular_file(dmg)
    require(re.fullmatch(r"[0-9a-f]{64}", expected_hash), "DMG の期待 SHA256 が不正です")
    require(type(expected_size) is int and expected_size > 0, "DMG の期待長が不正です")
    require(
        dmg.stat().st_size == expected_size and sha256(dmg) == expected_hash,
        "DMG の SHA256 / 長さが不一致です",
    )


def verify_dmg(
    archive: Path,
    dmg: Path,
    work: Path,
    tracked: Path,
    expected_hash: str,
    expected_size: int,
) -> dict:
    work = work_directory(work)
    check_dmg_hash(dmg, expected_hash, expected_size)
    dmg = dmg.resolve()
    expected = zip_inventory(archive, tracked)
    run("hdiutil", "verify", str(dmg))
    run("codesign", "--verify", "--strict", "-R", DEVELOPER_ID, str(dmg))
    run("xcrun", "stapler", "validate", str(dmg))
    with extracted_app(archive, work, tracked, expected):
        with mounted_dmg(dmg) as mountpoint:
            require(
                {entry.name for entry in mountpoint.iterdir()} == {"narumi.app", "Applications"},
                "DMG に予期しない外側の項目があります",
            )
            applications = mountpoint / "Applications"
            require(
                applications.is_symlink() and os.readlink(applications) == "/Applications",
                "DMG の Applications リンクが不正です",
            )
            app = mountpoint / "narumi.app"
            require(
                app_inventory(app, tracked) == expected, "DMG app の内容または mode が不一致です"
            )
            verify_app(app)
    check_dmg_hash(dmg, expected_hash, expected_size)
    return {
        "sha256": expected_hash,
        "size": expected_size,
        "app_inventory_sha256": inventory_hash(expected),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("create", "verify"):
        sub = commands.add_parser(name)
        sub.add_argument("--zip", required=True, type=Path)
        sub.add_argument("--work-dir", required=True, type=Path)
        sub.add_argument("--tracked-sources", required=True, type=Path)
        if name == "create":
            sub.add_argument("--output", required=True, type=Path)
        else:
            sub.add_argument("--dmg", required=True, type=Path)
            sub.add_argument("--expected-sha256", required=True)
            sub.add_argument("--expected-size", required=True, type=int)
    args = parser.parse_args()
    try:
        if args.command == "create":
            result = create_dmg(args.zip, args.output, args.work_dir, args.tracked_sources)
        else:
            result = verify_dmg(
                args.zip,
                args.dmg,
                args.work_dir,
                args.tracked_sources,
                args.expected_sha256,
                args.expected_size,
            )
    except (ReleaseError, InventoryError, OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"release-dmg: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
