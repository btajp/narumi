"""Read an app or archive without following untrusted paths or links."""

from __future__ import annotations

import hashlib
import os
import stat
import unicodedata
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MAX_FILES = 20_000
MAX_TOTAL_BYTES = 512 * 1024 * 1024


class InventoryError(ValueError):
    """An artifact does not match the distribution policy."""


def safe_path(value: str) -> str:
    if (
        not value
        or "\\" in value
        or ":" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise InventoryError(f"unsafe artifact path: {value!r}")
    return value


@dataclass(frozen=True)
class Entry:
    path: str
    kind: str
    size: int = 0
    read: Callable[[], bytes] | None = None
    target: str | None = None

    def data(self, limit: int = MAX_TOTAL_BYTES) -> bytes:
        if self.kind != "file" or self.read is None or self.size > limit:
            raise InventoryError(f"not a readable bounded file: {self.path}")
        result = self.read()
        if len(result) != self.size:
            raise InventoryError(f"file changed during inventory: {self.path}")
        return result


def validate_entries(entries: list[Entry]) -> dict[str, Entry]:
    if len(entries) > MAX_FILES or sum(entry.size for entry in entries) > MAX_TOTAL_BYTES:
        raise InventoryError("artifact exceeds inventory size limits")
    result: dict[str, Entry] = {}
    aliases: set[str] = set()
    for entry in entries:
        safe_path(entry.path)
        alias = unicodedata.normalize("NFC", entry.path).casefold()
        if entry.path in result or alias in aliases:
            raise InventoryError(f"duplicate artifact path: {entry.path}")
        result[entry.path] = entry
        aliases.add(alias)
    for entry in entries:
        for parent in PurePosixPath(entry.path).parents:
            if str(parent) in result and result[str(parent)].kind != "directory":
                raise InventoryError(f"artifact path traverses a non-directory: {entry.path}")
        if entry.kind == "symlink":
            if entry.target is None:
                raise InventoryError(f"missing symlink target: {entry.path}")
            safe_path(entry.target)
    return result


def read_app(app: Path) -> dict[str, Entry]:
    if app.name != "narumi.app" or app.is_symlink() or not app.is_dir():
        raise InventoryError("expected a non-symlink narumi.app directory")
    entries: list[Entry] = []
    for directory, dirs, files in os.walk(app, followlinks=False):
        for name in sorted(dirs + files):
            path = Path(directory) / name
            relative = path.relative_to(app).as_posix()
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                entry = Entry(relative, "symlink", target=os.readlink(path))
            elif stat.S_ISDIR(metadata.st_mode):
                entry = Entry(relative, "directory")
            elif stat.S_ISREG(metadata.st_mode):
                entry = Entry(relative, "file", metadata.st_size, path.read_bytes)
            else:
                raise InventoryError(f"unsupported artifact file type: {relative}")
            entries.append(entry)
    return validate_entries(entries)


def read_zip(archive: zipfile.ZipFile, *, app_root: bool) -> dict[str, Entry]:
    entries: list[Entry] = []
    seen_root = False
    for info in archive.infolist():
        if info.flag_bits & 1:
            raise InventoryError("encrypted archive entries are not allowed")
        if info.orig_filename != info.filename:
            raise InventoryError("archive filename normalization is not allowed")
        name = info.filename[:-1] if info.is_dir() else info.filename
        safe_path(name)
        if app_root:
            if name == "narumi.app" and info.is_dir():
                if seen_root:
                    raise InventoryError("duplicate archive root")
                seen_root = True
                continue
            if not name.startswith("narumi.app/"):
                raise InventoryError(f"unexpected archive root: {name}")
            name = name.removeprefix("narumi.app/")
        mode = info.external_attr >> 16
        kind = stat.S_IFMT(mode)
        if stat.S_ISLNK(mode):
            if info.file_size > 2048:
                raise InventoryError(f"oversized symlink target: {name}")
            target = archive.read(info).decode("utf-8")
            entry = Entry(name, "symlink", target=target)
        elif info.is_dir() and kind in {0, stat.S_IFDIR}:
            entry = Entry(name, "directory")
        elif not info.is_dir() and kind in {0, stat.S_IFREG}:
            entry = Entry(name, "file", info.file_size, lambda info=info: archive.read(info))
        else:
            raise InventoryError(f"unsupported archive file type: {name}")
        entries.append(entry)
    return validate_entries(entries)


def required_file(entries: dict[str, Entry], path: str) -> Entry:
    entry = entries.get(path)
    if entry is None or entry.kind != "file":
        raise InventoryError(f"missing regular file: {path}")
    return entry


def inventory_rows(entries: dict[str, Entry]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path, entry in sorted(entries.items()):
        row: dict[str, object] = {"path": path, "kind": entry.kind}
        if entry.kind == "file":
            row.update(size=entry.size, sha256=hashlib.sha256(entry.data()).hexdigest())
        elif entry.kind == "symlink":
            row["target"] = entry.target
        rows.append(row)
    return rows
