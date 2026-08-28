"""Validate runtime wheel payloads and their tracked source-file inventory."""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path, PurePosixPath

from bundle_paths import MAX_TOTAL_BYTES, InventoryError, read_zip, required_file, safe_path

PROMPTS = {
    "narumi/generate/prompts/integrate_interval.md",
    "narumi/generate/prompts/minutes_chunk.md",
    "narumi/generate/prompts/minutes_final.md",
    "narumi/slides/prompts/layer3_speakers.md",
}
METADATA_FILES = {"METADATA", "WHEEL", "RECORD", "entry_points.txt"}
SOURCE_PREFIXES = {"pipeline/src/": "narumi", "server/src/": "narumi_server"}


def wheel_identity(name: str) -> tuple[str, str]:
    safe_path(name)
    match = re.fullmatch(r"(narumi|narumi_server)-([0-9][A-Za-z0-9_.+]*)-py3-none-any\.whl", name)
    if match is None:
        raise InventoryError(f"unexpected runtime wheel: {name}")
    return match.group(1), match.group(2)


def payload_path(path: str, package: str) -> bool:
    parts = PurePosixPath(path).parts
    if not parts or parts[0] != package or "_generated" in parts:
        return False
    if path in PROMPTS:
        return True
    return (
        len(parts) >= 2
        and path.endswith(".py")
        and all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) for part in parts[:-1])
        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\.py", parts[-1]) is not None
    )


def tracked_payloads(path: Path) -> dict[str, set[str]]:
    if path.stat().st_size > 8 * 1024 * 1024:
        raise InventoryError("tracked-sources list exceeds the size limit")
    data = path.read_bytes()
    if not data or not data.endswith(b"\0"):
        raise InventoryError("tracked-sources must be a nonempty NUL-delimited list")
    result = {package: set() for package in SOURCE_PREFIXES.values()}
    seen: set[str] = set()
    for raw in data[:-1].split(b"\0"):
        source = safe_path(raw.decode("utf-8"))
        if source in seen:
            raise InventoryError("duplicate tracked source path")
        seen.add(source)
        for prefix, package in SOURCE_PREFIXES.items():
            if not source.startswith(prefix + package + "/"):
                continue
            relative = source.removeprefix(prefix)
            if "_generated" in PurePosixPath(relative).parts:
                continue
            if not payload_path(relative, package):
                raise InventoryError(f"unsupported tracked package file: {source}")
            result[package].add(relative)
    for package, paths in result.items():
        if f"{package}/__init__.py" not in paths:
            raise InventoryError(f"tracked-sources is missing the {package} package")
    if not PROMPTS <= result["narumi"]:
        raise InventoryError("tracked-sources is missing required prompt files")
    return result


def check_wheel(data: bytes, name: str, tracked: dict[str, set[str]] | None) -> set[str]:
    package, version = wheel_identity(name)
    metadata = f"{package}-{version}.dist-info"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = read_zip(archive, app_root=False)
        payload = {
            path
            for path, entry in entries.items()
            if entry.kind == "file" and payload_path(path, package)
        }
        allowed = payload | {f"{metadata}/{file}" for file in METADATA_FILES}
        directories = {str(parent) for path in allowed for parent in PurePosixPath(path).parents}
        for path, entry in entries.items():
            if (entry.kind == "directory" and path in directories) or (
                entry.kind == "file" and path in allowed
            ):
                continue
            raise InventoryError(f"unexpected wheel member: {name}: {path}")
        for file in ("METADATA", "WHEEL", "RECORD"):
            required_file(entries, f"{metadata}/{file}")
        if f"{package}/__init__.py" not in payload:
            raise InventoryError(f"wheel has no package initializer: {name}")
        if package == "narumi" and not PROMPTS <= payload:
            raise InventoryError(f"wheel is missing required prompt files: {name}")
        if tracked is not None and payload != tracked[package]:
            unexpected = sorted(payload - tracked[package])
            missing = sorted(tracked[package] - payload)
            raise InventoryError(
                f"wheel does not match tracked sources: {name}; "
                f"unexpected={unexpected}; missing={missing}"
            )
        for entry in entries.values():
            if entry.kind == "file":
                entry.data()
    return payload


def copy_wheels(source: Path, destination: Path, tracked: dict[str, set[str]] | None) -> list[str]:
    """Copy validated wheels, not build sidecars, into a fresh bundle directory."""
    if source.is_symlink() or not source.is_dir():
        raise InventoryError("wheel source must be a non-symlink directory")
    paths = sorted(source.glob("*.whl"))
    if len(paths) != 2:
        raise InventoryError("wheel build must produce exactly two wheels")
    payloads: dict[str, bytes] = {}
    packages: set[str] = set()
    for path in paths:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_TOTAL_BYTES:
            raise InventoryError("wheel source must contain bounded regular files")
        package, _ = wheel_identity(path.name)
        if package in packages:
            raise InventoryError("wheel build produced a duplicate package")
        packages.add(package)
        data = path.read_bytes()
        check_wheel(data, path.name, tracked)
        payloads[path.name] = data
    if packages != {"narumi", "narumi_server"}:
        raise InventoryError("wheel build must include narumi and narumi_server")
    if destination.exists() or destination.is_symlink():
        raise InventoryError("wheel destination must not already exist")
    destination.mkdir(parents=True)
    for name, data in payloads.items():
        (destination / name).write_bytes(data)
    return sorted(payloads)
