"""Build-side files never enter the wheel payload shipped inside the app."""

import json
from pathlib import Path

import pytest

from .bundle_artifact_fixtures import (
    VERSION,
    run_inventory,
    tracked_list,
    wheel_bytes,
    write_file,
)


def make_wheels(source: Path) -> set[str]:
    names = set()
    for package in ("narumi", "narumi_server"):
        name = f"{package}-{VERSION}-py3-none-any.whl"
        write_file(source / name, wheel_bytes(package))
        names.add(name)
    return names


def test_copy_wheels_omits_uv_sidecars(tmp_path: Path):
    source = tmp_path / "build"
    names = make_wheels(source)
    write_file(source / ".gitignore", "*\n")
    write_file(source / "local-notes.md", "local fixture")
    destination = tmp_path / "bundled"
    result = run_inventory(
        "copy-wheels", source, destination, "--tracked-sources", tracked_list(tmp_path)
    )
    assert result.returncode == 0, result.stderr
    assert set(json.loads(result.stdout)["files"]) == names
    assert {path.name for path in destination.iterdir()} == names
    for name in names:
        assert (destination / name).read_bytes() == (source / name).read_bytes()


@pytest.mark.parametrize("mutation", ["missing", "extra", "invalid", "symlink", "untracked"])
def test_copy_wheels_validates_before_creating_destination(tmp_path: Path, mutation: str):
    source = tmp_path / "build"
    make_wheels(source)
    wheel = source / f"narumi-{VERSION}-py3-none-any.whl"
    if mutation == "missing":
        wheel.unlink()
    elif mutation == "extra":
        write_file(source / "other-1.0-py3-none-any.whl", "unwanted")
    elif mutation == "invalid":
        wheel.write_bytes(b"not a wheel")
    elif mutation == "symlink":
        outside = tmp_path / "outside.whl"
        wheel.rename(outside)
        wheel.symlink_to(outside)
    else:
        wheel.write_bytes(wheel_bytes("narumi", extra={"narumi/private_notes.py": b"fixture"}))
    destination = tmp_path / "bundled"
    result = run_inventory(
        "copy-wheels", source, destination, "--tracked-sources", tracked_list(tmp_path)
    )
    assert result.returncode == 1
    assert not destination.exists()


def test_copy_wheels_preserves_existing_destination(tmp_path: Path):
    source = tmp_path / "build"
    make_wheels(source)
    destination = tmp_path / "bundled"
    sentinel = write_file(destination / "keep.txt", "keep fixture")
    result = run_inventory("copy-wheels", source, destination)
    assert result.returncode == 1
    assert sentinel.read_text() == "keep fixture"
    assert {path.name for path in destination.iterdir()} == {"keep.txt"}
