"""Only manifest-listed contracts may enter the bundled runtime."""

import json
from pathlib import Path

import pytest

from .bundle_artifact_fixtures import make_contracts, run_inventory, write_file


def test_copy_contracts_omits_local_and_unlisted_files(tmp_path: Path):
    source = make_contracts(tmp_path / "source")
    for name in (".env", "notes.md", "tools/local_only.json", "local-work/private.pem"):
        write_file(source / name, "fake local-only fixture")
    destination = tmp_path / "output"
    result = run_inventory("copy-contracts", source, destination)
    assert result.returncode == 0, result.stderr
    expected = {"manifest.json", "defs/common.json", "tools/ping.json"}
    assert set(json.loads(result.stdout)["files"]) == expected
    assert {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file()
    } == expected
    assert all(
        path.read_bytes() == (source / path.relative_to(destination)).read_bytes()
        for path in destination.rglob("*.json")
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("defs", ["../private.json"]),
        ("defs", ["/tmp/private.json"]),
        ("defs", [r"defs\common.json"]),
        ("defs", ["defs/common.json", "defs/common.json"]),
        ("tools", ["../private"]),
        ("tools", ["ping", "ping"]),
        ("tools", []),
    ],
)
def test_invalid_manifest_never_creates_destination(tmp_path: Path, field: str, value):
    source = make_contracts(tmp_path / "source")
    manifest = source / "manifest.json"
    data = json.loads(manifest.read_text())
    data[field] = value
    manifest.write_text(json.dumps(data))
    destination = tmp_path / "output"
    result = run_inventory("copy-contracts", source, destination)
    assert result.returncode == 1
    assert not destination.exists()


@pytest.mark.parametrize("relative", ["manifest.json", "tools/ping.json", "defs"])
def test_contract_source_symlinks_are_rejected(tmp_path: Path, relative: str):
    source = make_contracts(tmp_path / "source")
    original = source / relative
    moved = tmp_path / "outside"
    original.rename(moved)
    original.symlink_to(moved, target_is_directory=moved.is_dir())
    destination = tmp_path / "output"
    result = run_inventory("copy-contracts", source, destination)
    assert result.returncode == 1
    assert not destination.exists()
    assert moved.exists()


def test_copy_contracts_preserves_existing_destination(tmp_path: Path):
    source = make_contracts(tmp_path / "source")
    destination = tmp_path / "output"
    sentinel = write_file(destination / "keep.txt", "keep this fixture")
    result = run_inventory("copy-contracts", source, destination)
    assert result.returncode == 1
    assert sentinel.read_text() == "keep this fixture"
    assert not (destination / "manifest.json").exists()


def test_invalid_contract_json_does_not_partially_copy(tmp_path: Path):
    source = make_contracts(tmp_path / "source")
    write_file(source / "tools/ping.json", "not JSON")
    destination = tmp_path / "output"
    result = run_inventory("copy-contracts", source, destination)
    assert result.returncode == 1
    assert "invalid JSON" in result.stderr
    assert not destination.exists()
