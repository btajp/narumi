#!/usr/bin/env python3
"""Fail closed on unexpected files in a distribution app, runtime, or ZIP."""

from __future__ import annotations

import argparse
import hashlib
import json
import plistlib
import re
import struct
import sys
import zipfile
from pathlib import Path, PurePosixPath

import bundle_sparkle
from bundle_contracts import JSON_LIMIT, contract_paths, copy_contracts, json_object
from bundle_paths import Entry, InventoryError, inventory_rows, read_app, read_zip, required_file
from bundle_wheels import check_wheel, copy_wheels, tracked_payloads, wheel_identity

RUNTIME = "Contents/Resources/runtime"
REQUIRED = {
    "Contents/Info.plist",
    "Contents/PkgInfo",
    "Contents/MacOS/NarumiMenuBar",
    "Contents/MacOS/narumi-recorder",
    "Contents/MacOS/narumi-keychain",
    "Contents/Resources/AppIcon.icns",
}
SIGNATURE_FILES = {"Contents/_CodeSignature/CodeResources", "Contents/CodeResources"}
CODEX_TEAM_ID = "2DC432GLL2"
ARM64_MACHO = b"\xcf\xfa\xed\xfe\x0c\x00\x00\x01"


def matches_hash(entry: Entry, expected: object) -> bytes:
    data = entry.data()
    if (
        not isinstance(expected, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected) is None
        or hashlib.sha256(data).hexdigest() != expected
    ):
        raise InventoryError(f"runtime manifest hash mismatch: {entry.path}")
    return data


def matches_size(entry: Entry, expected: object) -> None:
    if not isinstance(expected, int) or isinstance(expected, bool) or entry.size != expected:
        raise InventoryError(f"runtime manifest size mismatch: {entry.path}")


def exact_object(value: object, label: str, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise InventoryError(f"{label} must contain exactly: {', '.join(sorted(keys))}")
    return value


def require_string(value: object, label: str, pattern: str | None = None) -> str:
    if (
        not isinstance(value, str)
        or not value
        or (pattern and re.fullmatch(pattern, value) is None)
    ):
        raise InventoryError(f"invalid {label}")
    return value


def require_positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise InventoryError(f"invalid {label}")
    return value


def require_arm64_macho(data: bytes, path: str) -> None:
    if not data.startswith(ARM64_MACHO):
        raise InventoryError(f"expected an arm64 Mach-O binary: {path}")


def codex_files(entries: dict[str, Entry], value: object) -> set[str]:
    codex = exact_object(
        value,
        "runtime manifest codex",
        {"version", "source", "source_tag", "source_commit", "artifact", "binary", "license"},
    )
    version = require_string(
        codex["version"],
        "Codex version",
        r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)",
    )
    source_tag = require_string(codex["source_tag"], "Codex source_tag")
    if source_tag != f"rust-v{version}":
        raise InventoryError("Codex source_tag differs from version")
    if codex["source"] != f"https://github.com/openai/codex/releases/tag/{source_tag}":
        raise InventoryError("invalid Codex source")
    require_string(codex["source_commit"], "Codex source_commit", r"[0-9a-f]{40}")

    artifact = exact_object(
        codex["artifact"],
        "runtime manifest codex artifact",
        {"name", "url", "sha256", "size", "entry"},
    )
    name = require_string(artifact["name"], "Codex artifact name")
    if name != "codex-aarch64-apple-darwin.tar.gz":
        raise InventoryError("invalid Codex artifact name")
    if artifact["url"] != f"https://github.com/openai/codex/releases/download/{source_tag}/{name}":
        raise InventoryError("invalid Codex artifact URL")
    require_string(artifact["sha256"], "Codex artifact sha256", r"[0-9a-f]{64}")
    require_positive_int(artifact["size"], "Codex artifact size")
    if artifact["entry"] != "codex-aarch64-apple-darwin":
        raise InventoryError("invalid Codex archive entry")

    binary = exact_object(
        codex["binary"],
        "runtime manifest codex binary",
        {"path", "sha256", "size", "architecture", "version_output", "publisher_team_id"},
    )
    binary_path = require_string(binary["path"], "Codex binary path")
    if binary_path != f"codex/{version}/codex":
        raise InventoryError("Codex binary path differs from version")
    if binary["architecture"] != "arm64" or binary["version_output"] != f"codex-cli {version}":
        raise InventoryError("invalid Codex binary architecture or version output")
    if binary["publisher_team_id"] != CODEX_TEAM_ID:
        raise InventoryError("invalid Codex publisher TeamIdentifier")
    bundled_binary = required_file(entries, f"{RUNTIME}/{binary_path}")
    matches_size(bundled_binary, binary["size"])
    require_arm64_macho(matches_hash(bundled_binary, binary["sha256"]), bundled_binary.path)

    license_metadata = exact_object(
        codex["license"],
        "runtime manifest codex license",
        {
            "spdx",
            "path",
            "source",
            "source_tag",
            "sha256",
            "size",
            "notice_path",
            "notice_source",
            "notice_sha256",
            "notice_size",
        },
    )
    if license_metadata["spdx"] != "Apache-2.0" or license_metadata["source_tag"] != source_tag:
        raise InventoryError("invalid Codex license identity")
    license_path = "licenses/openai-codex-Apache-2.0.txt"
    notice_path = "licenses/openai-codex-NOTICE.txt"
    if license_metadata["path"] != license_path or license_metadata["notice_path"] != notice_path:
        raise InventoryError("invalid Codex license path")
    if license_metadata["source"] != f"https://github.com/openai/codex/blob/{source_tag}/LICENSE":
        raise InventoryError("invalid Codex license source")
    if (
        license_metadata["notice_source"]
        != f"https://github.com/openai/codex/blob/{source_tag}/NOTICE"
    ):
        raise InventoryError("invalid Codex NOTICE source")
    license_entry = required_file(entries, f"{RUNTIME}/{license_path}")
    notice_entry = required_file(entries, f"{RUNTIME}/{notice_path}")
    matches_size(license_entry, license_metadata["size"])
    matches_hash(license_entry, license_metadata["sha256"])
    matches_size(notice_entry, license_metadata["notice_size"])
    matches_hash(notice_entry, license_metadata["notice_sha256"])
    return {
        bundled_binary.path,
        license_entry.path,
        notice_entry.path,
    }


def runtime_files(
    entries: dict[str, Entry], app_version: str, tracked: dict[str, set[str]] | None
) -> set[str]:
    allowed = {f"{RUNTIME}/{name}" for name in ("uv", "manifest.json", "requirements.txt")}
    for path in allowed:
        required_file(entries, path)
    manifest = json_object(entries[f"{RUNTIME}/manifest.json"].data(JSON_LIMIT), "runtime manifest")
    if manifest.get("app_version") != app_version:
        raise InventoryError("runtime manifest app_version differs from Info.plist")
    allowed |= codex_files(entries, manifest.get("codex"))
    require_arm64_macho(entries[f"{RUNTIME}/uv"].data(), f"{RUNTIME}/uv")
    wheels = manifest.get("wheels")
    if not isinstance(wheels, dict) or len(wheels) != 2:
        raise InventoryError("runtime manifest must declare exactly two wheels")
    packages = set()
    for name, digest in wheels.items():
        package, _ = wheel_identity(name)
        packages.add(package)
        path = f"{RUNTIME}/wheels/{name}"
        entry = required_file(entries, path)
        matches_hash(entry, digest)
        check_wheel(entry.data(), name, tracked)
        allowed.add(path)
    if packages != {"narumi", "narumi_server"}:
        raise InventoryError("runtime manifest must include narumi and narumi_server")
    matches_hash(entries[f"{RUNTIME}/requirements.txt"], manifest.get("requirements_sha256"))
    contract_manifest_path = f"{RUNTIME}/contracts/manifest.json"
    manifest_entry = required_file(entries, contract_manifest_path)
    contracts = contract_paths(json_object(manifest_entry.data(JSON_LIMIT), "contract manifest"))
    for relative in contracts:
        path = f"{RUNTIME}/contracts/{relative}"
        json_object(required_file(entries, path).data(JSON_LIMIT), relative)
        allowed.add(path)
    return allowed


def inspect(
    entries: dict[str, Entry], *, require_runtime: bool, tracked: dict[str, set[str]] | None
) -> dict[str, object]:
    for path in REQUIRED:
        required_file(entries, path)
    plist = plistlib.loads(entries["Contents/Info.plist"].data(JSON_LIMIT))
    if not isinstance(plist, dict) or plist.get("CFBundleIconFile") != "AppIcon":
        raise InventoryError("Info.plist must register CFBundleIconFile=AppIcon")
    if plist.get("CFBundleExecutable") != "NarumiMenuBar":
        raise InventoryError("unexpected CFBundleExecutable")
    icon = entries["Contents/Resources/AppIcon.icns"].data(8 * 1024 * 1024)
    if len(icon) <= 8 or icon[:4] != b"icns" or struct.unpack(">I", icon[4:8])[0] != len(icon):
        raise InventoryError("invalid AppIcon.icns header")
    allowed = REQUIRED | SIGNATURE_FILES
    has_runtime = any(path == RUNTIME or path.startswith(RUNTIME + "/") for path in entries)
    if require_runtime and not has_runtime:
        raise InventoryError("distribution app is missing its bundled runtime")
    if has_runtime:
        for executable in ("NarumiMenuBar", "narumi-recorder", "narumi-keychain"):
            path = f"Contents/MacOS/{executable}"
            require_arm64_macho(entries[path].data(), path)
        allowed |= runtime_files(entries, plist.get("CFBundleShortVersionString", ""), tracked)
    has_sparkle = any(path.startswith(bundle_sparkle.PREFIX + "/") for path in entries)
    if has_sparkle:
        for path in bundle_sparkle.REQUIRED:
            required_file(entries, path)
    allowed |= bundle_sparkle.FILES
    links = bundle_sparkle.LINKS
    directories = {
        str(parent) for path in allowed | links.keys() for parent in PurePosixPath(path).parents
    }
    for path, entry in entries.items():
        if entry.kind == "directory" and path in directories:
            continue
        if entry.kind == "file" and path in allowed:
            continue
        if entry.kind == "symlink" and links.get(path) == entry.target:
            target = str(PurePosixPath(path).parent / entry.target)
            target = target.replace(
                f"{bundle_sparkle.PREFIX}/Versions/Current", bundle_sparkle.VERSION, 1
            )
            if target in entries or any(name.startswith(target + "/") for name in entries):
                continue
            raise InventoryError(f"dangling framework symlink: {path}")
        raise InventoryError(f"unexpected app member: {path}")
    return {"runtime": has_runtime, "entries": inventory_rows(entries)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check-app", "check-zip"):
        check = commands.add_parser(name)
        check.add_argument("path", type=Path)
        check.add_argument("--require-runtime", action="store_true")
        check.add_argument("--tracked-sources", type=Path)
    copy = commands.add_parser("copy-contracts")
    copy.add_argument("source", type=Path)
    copy.add_argument("destination", type=Path)
    wheels = commands.add_parser("copy-wheels")
    wheels.add_argument("source", type=Path)
    wheels.add_argument("destination", type=Path)
    wheels.add_argument("--tracked-sources", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "copy-contracts":
            result = {"files": copy_contracts(args.source, args.destination)}
        else:
            tracked = tracked_payloads(args.tracked_sources) if args.tracked_sources else None
            if args.command == "copy-wheels":
                result = {"files": copy_wheels(args.source, args.destination, tracked)}
            elif args.command == "check-app":
                result = inspect(
                    read_app(args.path), require_runtime=args.require_runtime, tracked=tracked
                )
            else:
                with zipfile.ZipFile(args.path) as archive:
                    result = inspect(
                        read_zip(archive, app_root=True),
                        require_runtime=args.require_runtime,
                        tracked=tracked,
                    )
    except (InventoryError, OSError, ValueError, UnicodeError, zipfile.BadZipFile) as error:
        print(f"bundle-inventory: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
