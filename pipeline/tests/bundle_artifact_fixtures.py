"""Small distribution artifacts; never build or sign a real desktop app."""

from __future__ import annotations

import hashlib
import io
import json
import plistlib
import stat
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / "scripts" / "bundle_inventory.py"
VERSION = "0.1.1"
PROMPTS = {
    "narumi/generate/prompts/integrate_interval.md",
    "narumi/generate/prompts/minutes_chunk.md",
    "narumi/generate/prompts/minutes_final.md",
    "narumi/slides/prompts/layer3_speakers.md",
}


def write_file(path: Path, data: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    return path


def run_inventory(*args: str | Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(INVENTORY), *map(str, args)],
        text=True,
        capture_output=True,
        timeout=20,
    )


def make_contracts(path: Path) -> Path:
    write_file(
        path / "manifest.json",
        json.dumps({"contract_version": "1.0.0", "defs": ["defs/common.json"], "tools": ["ping"]}),
    )
    write_file(path / "defs/common.json", "{}")
    write_file(path / "tools/ping.json", '{"name":"ping"}')
    return path


def wheel_bytes(
    package: str, extra: dict[str, bytes] | None = None, omit: set[str] | None = None
) -> bytes:
    files = {f"{package}/__init__.py": b""}
    if package == "narumi":
        files.update({name: b"fixed prompt\n" for name in PROMPTS})
    metadata = f"{package}-{VERSION}.dist-info"
    files.update(
        {
            f"{metadata}/METADATA": f"Name: {package}\nVersion: {VERSION}\n".encode(),
            f"{metadata}/WHEEL": b"Wheel-Version: 1.0\nTag: py3-none-any\n",
            f"{metadata}/RECORD": b"",
        }
    )
    files.update(extra or {})
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for path, data in sorted(files.items()):
            if path not in (omit or set()):
                archive.writestr(path, data)
    return output.getvalue()


def make_app(parent: Path, *, runtime: bool = True) -> Path:
    app = parent / "narumi.app"
    plist = {
        "CFBundleExecutable": "NarumiMenuBar",
        "CFBundleIconFile": "AppIcon",
        "CFBundleShortVersionString": VERSION,
    }
    write_file(app / "Contents/Info.plist", plistlib.dumps(plist))
    write_file(app / "Contents/PkgInfo", "APPL????")
    write_file(app / "Contents/MacOS/NarumiMenuBar", "fake menu app")
    write_file(app / "Contents/MacOS/narumi-recorder", "fake recorder")
    icon = b"icns" + struct.pack(">I", 16) + b"icp4" + struct.pack(">I", 8)
    write_file(app / "Contents/Resources/AppIcon.icns", icon)
    if runtime:
        directory = app / "Contents/Resources/runtime"
        write_file(directory / "uv", "fake uv")
        requirements = write_file(directory / "requirements.txt", "pillow==11.0.0\n")
        wheels = {}
        for package in ("narumi", "narumi_server"):
            name = f"{package}-{VERSION}-py3-none-any.whl"
            data = wheel_bytes(package)
            write_file(directory / "wheels" / name, data)
            wheels[name] = hashlib.sha256(data).hexdigest()
        make_contracts(directory / "contracts")
        write_file(
            directory / "manifest.json",
            json.dumps(
                {
                    "app_version": VERSION,
                    "python": "3.13",
                    "uv_version": "0.12.6",
                    "wheels": wheels,
                    "requirements_sha256": hashlib.sha256(requirements.read_bytes()).hexdigest(),
                }
            ),
        )
    return app


def replace_wheel(app: Path, package: str, data: bytes) -> None:
    runtime = app / "Contents/Resources/runtime"
    name = f"{package}-{VERSION}-py3-none-any.whl"
    write_file(runtime / "wheels" / name, data)
    path = runtime / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["wheels"][name] = hashlib.sha256(data).hexdigest()
    write_file(path, json.dumps(manifest))


def tracked_list(parent: Path) -> Path:
    files = {
        "pipeline/src/narumi/__init__.py",
        "server/src/narumi_server/__init__.py",
        *{f"pipeline/src/{name}" for name in PROMPTS},
    }
    data = b"".join(name.encode() + b"\0" for name in sorted(files))
    return write_file(parent / "tracked-sources.nul", data)


def app_zip(app: Path, path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for file in sorted(app.rglob("*")):
            name = file.relative_to(app.parent).as_posix()
            if file.is_symlink():
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, str(file.readlink()).encode())
            elif file.is_file():
                archive.write(file, name)
    return path
