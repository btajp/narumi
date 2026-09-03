"""Release version gates must cover the values reported by running components."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VERSION_FILES = (
    "VERSION",
    "uv.lock",
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


WORKSPACE_SOURCES = {
    "narumi": "pipeline",
    "narumi-server": "server",
}


def write_workspace_lock(
    root: Path,
    package_versions: dict[str, tuple[str, ...]],
) -> None:
    package_tables = []
    for name, versions in package_versions.items():
        for package_version in versions:
            package_tables.append(
                "\n".join(
                    (
                        "[[package]]",
                        f'name = "{name}"',
                        f'version = "{package_version}"',
                        f'source = {{ editable = "{WORKSPACE_SOURCES[name]}" }}',
                    )
                )
            )
    (root / "uv.lock").write_text(
        "version = 1\nrevision = 3\n\n"
        "[manifest]\n"
        'members = ["narumi", "narumi-server"]\n\n' + "\n\n".join(package_tables) + "\n"
    )


@pytest.mark.parametrize("missing_package", WORKSPACE_SOURCES)
def test_missing_workspace_package_in_lock_is_rejected(
    release_tree: Path,
    missing_package: str,
) -> None:
    version = (release_tree / "VERSION").read_text().strip()
    packages = {name: (version,) for name in WORKSPACE_SOURCES if name != missing_package}
    write_workspace_lock(release_tree, packages)

    result = version_check(release_tree)

    assert result.returncode != 0
    assert f"workspace package '{missing_package}' がありません" in result.stderr


@pytest.mark.parametrize("duplicate_package", WORKSPACE_SOURCES)
def test_duplicate_workspace_package_in_lock_is_rejected(
    release_tree: Path,
    duplicate_package: str,
) -> None:
    version = (release_tree / "VERSION").read_text().strip()
    packages = {name: (version,) for name in WORKSPACE_SOURCES}
    packages[duplicate_package] = (version, version)
    write_workspace_lock(release_tree, packages)

    result = version_check(release_tree)

    assert result.returncode != 0
    assert f"workspace package '{duplicate_package}' が重複しています" in result.stderr


@pytest.mark.parametrize("mismatched_package", WORKSPACE_SOURCES)
def test_workspace_package_version_mismatch_in_lock_is_rejected(
    release_tree: Path,
    mismatched_package: str,
) -> None:
    version = (release_tree / "VERSION").read_text().strip()
    packages = {name: (version,) for name in WORKSPACE_SOURCES}
    packages[mismatched_package] = ("999.0.0",)
    write_workspace_lock(release_tree, packages)

    result = version_check(release_tree)

    assert result.returncode != 0
    assert f"uv.lock {mismatched_package} version != VERSION={version}" in result.stderr


def test_registry_package_cannot_replace_workspace_package_in_lock(
    release_tree: Path,
) -> None:
    version = (release_tree / "VERSION").read_text().strip()
    write_workspace_lock(
        release_tree,
        {name: (version,) for name in WORKSPACE_SOURCES},
    )
    lock_path = release_tree / "uv.lock"
    lock_path.write_text(
        lock_path.read_text().replace(
            'source = { editable = "pipeline" }',
            'source = { registry = "https://example.invalid/simple" }',
            1,
        )
    )

    result = version_check(release_tree)

    assert result.returncode != 0
    assert "'narumi' が正しい workspace package ではありません" in result.stderr


def test_invalid_lock_does_not_echo_its_contents(release_tree: Path) -> None:
    confidential_marker = "DO-NOT-ECHO-LOCK-CONTENTS"
    (release_tree / "uv.lock").write_text(f'[[package]]\nname = "{confidential_marker}\n')

    result = version_check(release_tree)

    assert result.returncode != 0
    assert "uv.lock を解析できません" in result.stderr
    assert confidential_marker not in result.stderr
