"""Release version gates must cover the values reported by running components."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VERSION_FILES = (
    "VERSION",
    "pipeline/pyproject.toml",
    "server/pyproject.toml",
    "pipeline/src/narumi/__init__.py",
    "server/src/narumi_server/__init__.py",
    "app/Sources/NarumiRecorderKit/RecorderEvents.swift",
    "CHANGELOG.md",
)


def version_check(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(root / "scripts/check-version.sh")],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def test_repository_versions_match() -> None:
    result = version_check(ROOT)
    assert result.returncode == 0, result.stderr


@pytest.fixture
def release_tree(tmp_path: Path) -> Path:
    for relative in (*VERSION_FILES, "scripts/check-version.sh"):
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    return tmp_path


@pytest.mark.parametrize("relative", VERSION_FILES[1:])
def test_mismatched_runtime_or_metadata_is_rejected(release_tree: Path, relative: str) -> None:
    version = (release_tree / "VERSION").read_text().strip()
    target = release_tree / relative
    target.write_text(target.read_text().replace(version, "999.0.0"))
    result = version_check(release_tree)
    assert result.returncode != 0
    assert "!= VERSION=" in result.stderr


def test_missing_runtime_version_is_rejected(release_tree: Path) -> None:
    (release_tree / "server/src/narumi_server/__init__.py").write_text('"""No version."""\n')
    result = version_check(release_tree)
    assert result.returncode != 0
    assert "server __version__=" in result.stderr


def test_invalid_release_version_is_rejected(release_tree: Path) -> None:
    (release_tree / "VERSION").write_text("not-a-version\n")
    result = version_check(release_tree)
    assert result.returncode != 0
    assert "semver" in result.stderr
