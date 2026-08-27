"""Private permissions, atomic failure handling and process-shared Gaia update locking."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from narumi.errors import NarumiError
from narumi.gaia import _settings_io
from narumi.gaia.settings import GAIA_CONNECTION_FILE, GaiaConnectionStore

URL = "http://127.0.0.1:4111/mcp"
KEY = "fake-permissions-secret-789465"


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_new_parents_files_and_lock_are_private(tmp_path: Path):
    parent = tmp_path / "new" / "private"
    store = GaiaConnectionStore(parent / GAIA_CONNECTION_FILE, environ={})
    store.set(url=URL, api_key=KEY)
    assert mode(parent.parent) == 0o700
    assert mode(parent) == 0o700
    assert mode(store.path) == 0o600
    assert mode(parent / (GAIA_CONNECTION_FILE + ".lock")) == 0o600
    assert sorted(path.name for path in parent.iterdir()) == ["gaia.json", "gaia.json.lock"]


def test_existing_parent_permissions_are_not_changed(tmp_path: Path):
    parent = tmp_path / "existing"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    store = GaiaConnectionStore(parent / GAIA_CONNECTION_FILE, environ={})
    store.set(url=URL, api_key=KEY)
    store.path.chmod(0o644)
    assert store.get()["has_api_key"] is True
    assert mode(store.path) == 0o600
    lock = parent / (GAIA_CONNECTION_FILE + ".lock")
    lock.chmod(0o644)
    store.set(api_key=None)
    assert mode(lock) == 0o600
    assert mode(parent) == 0o755


def test_replacement_is_private_and_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store = GaiaConnectionStore(tmp_path / GAIA_CONNECTION_FILE, environ={})
    store.set(url=URL, api_key=KEY)
    previous = store.path.read_bytes()

    def failed_replace(source, target):
        assert Path(target) == store.path
        assert mode(Path(source)) == 0o600
        assert store.path.read_bytes() == previous
        raise OSError(KEY)

    monkeypatch.setattr(_settings_io.os, "replace", failed_replace)
    with pytest.raises(NarumiError) as error:
        store.set(api_key="replacement-key")
    assert error.value.code == "internal"
    assert KEY not in str(error.value)
    assert error.value.__suppress_context__ is True
    assert store.path.read_bytes() == previous
    assert not list(tmp_path.glob(".gaia.json.*"))


@pytest.mark.parametrize("name", ["gaia.json", "gaia.json.lock"])
def test_symlink_targets_are_not_read_or_modified(tmp_path: Path, name: str):
    target = tmp_path / "unrelated.json"
    target.write_text(KEY)
    target.chmod(0o644)
    (tmp_path / name).symlink_to(target)
    store = GaiaConnectionStore(tmp_path / GAIA_CONNECTION_FILE, environ={})
    with pytest.raises(NarumiError) as error:
        store.set(url=URL, api_key="replacement-key")
    assert KEY not in str(error.value)
    assert target.read_text() == KEY
    assert mode(target) == 0o644


def test_concurrent_store_instances_preserve_key_only_update(tmp_path: Path):
    path = tmp_path / GAIA_CONNECTION_FILE
    GaiaConnectionStore(path, environ={}).set(url=URL)

    def update(index: int):
        store = GaiaConnectionStore(path, environ={})
        return store.set(url=URL) if index % 2 else store.set(api_key=KEY)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(update, range(40)))
    assert json.loads(path.read_text())["api_key"] == KEY
    assert mode(path) == 0o600


def test_cross_process_update_waits_for_file_transaction_lock(tmp_path: Path):
    path = tmp_path / GAIA_CONNECTION_FILE
    store = GaiaConnectionStore(path, environ={})
    store.set(url=URL)
    program = (
        "import sys\n"
        "from pathlib import Path\n"
        "from narumi.gaia.settings import GaiaConnectionStore\n"
        "print('ready', flush=True)\n"
        f"GaiaConnectionStore(Path(sys.argv[1]), environ={{}}).set(api_key={KEY!r})\n"
        "print('updated', flush=True)\n"
    )
    child = None
    try:
        with _settings_io.write_lock(path):
            child = subprocess.Popen(
                [sys.executable, "-c", program, str(path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={**os.environ, "NARUMI_HOME": str(tmp_path)},
            )
            assert child.stdout is not None
            assert child.stdout.readline().strip() == "ready"
            with pytest.raises(subprocess.TimeoutExpired):
                child.wait(timeout=0.2)
            assert json.loads(path.read_text())["api_key"] is None
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, stderr
        assert stdout.strip() == "updated"
        assert json.loads(path.read_text())["api_key"] == KEY
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.communicate(timeout=10)
