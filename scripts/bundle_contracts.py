"""Copy only the JSON contracts explicitly declared by their manifest."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from bundle_paths import InventoryError, safe_path

JSON_LIMIT = 4 * 1024 * 1024


def json_object(data: bytes, label: str) -> dict:
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError) as error:
        raise InventoryError(f"invalid JSON: {label}") from error
    if not isinstance(value, dict):
        raise InventoryError(f"expected JSON object: {label}")
    return value


def contract_paths(manifest: dict) -> set[str]:
    definitions = manifest.get("defs")
    tools = manifest.get("tools")
    if not isinstance(definitions, list) or not isinstance(tools, list) or not tools:
        raise InventoryError("contract manifest needs defs and nonempty tools arrays")
    result = {"manifest.json"}
    for name in definitions:
        if not isinstance(name, str) or not re.fullmatch(
            r"defs/[A-Za-z][A-Za-z0-9_-]*\.json", name
        ):
            raise InventoryError("invalid definition path in contract manifest")
        safe_path(name)
        if name in result:
            raise InventoryError(f"duplicate contract manifest path: {name}")
        result.add(name)
    for name in tools:
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise InventoryError("invalid tool name in contract manifest")
        path = f"tools/{name}.json"
        if path in result:
            raise InventoryError(f"duplicate contract manifest path: {path}")
        result.add(path)
    return result


def copy_contracts(source: Path, destination: Path) -> list[str]:
    if source.is_symlink() or not source.is_dir():
        raise InventoryError("contracts source must be a non-symlink directory")
    manifest_path = source / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise InventoryError("contract manifest must be a regular file")
    if manifest_path.stat().st_size > JSON_LIMIT:
        raise InventoryError("contract manifest exceeds the size limit")
    paths = contract_paths(json_object(manifest_path.read_bytes(), "contract manifest"))
    for relative in sorted(paths):
        path = source / relative
        parts = Path(relative).parts
        if any(source.joinpath(*parts[:index]).is_symlink() for index in range(1, len(parts) + 1)):
            raise InventoryError(f"symlink in contract source: {relative}")
        if not path.is_file() or path.stat().st_size > JSON_LIMIT:
            raise InventoryError(f"missing or oversized contract: {relative}")
        json_object(path.read_bytes(), relative)
    if destination.exists() or destination.is_symlink():
        raise InventoryError("contracts destination must not already exist")
    destination.mkdir(parents=True)
    for relative in sorted(paths):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)
    return sorted(paths)
