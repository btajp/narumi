"""Fail-closed handling for private Codex credential temporaries."""

from __future__ import annotations

import os
import stat

import pytest
from narumi.errors import EngineUnavailableError
from narumi.providers.codex import _session
from narumi.providers.codex._rpc import PROCESS_CLEANUP_REASON
from narumi.providers.codex.backend import CodexBackend

CONNECTION = "conn-0123456789ab"
SYNTHETIC_CREDENTIAL = b'{"fixture_token":"temporary cleanup regression"}'


@pytest.mark.parametrize("directory_close_fails", [False, True])
def test_write_and_unlink_failure_poisons_connection(tmp_path, monkeypatch, directory_close_fails):
    root = tmp_path / "narumi"
    root.mkdir(mode=0o700)
    backend = CodexBackend(root)
    state = _session.connection_directory(root, CONNECTION) / "state"
    state.mkdir(parents=True, mode=0o700)

    original_fsync = _session.os.fsync
    original_unlink = _session.os.unlink
    original_close = _session.os.close
    state_metadata = os.stat(state)
    leaked_descriptors = []
    failed = {
        "directory_close": False,
        "file_sync": False,
        "temporary_unlink": False,
    }

    def reject_temporary_sync(descriptor):
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            failed["file_sync"] = True
            raise OSError("fixture credential write sync failure")
        return original_fsync(descriptor)

    def reject_temporary_unlink(name, *, dir_fd=None):
        if isinstance(name, str) and _session._AUTH_TEMPORARY_NAME.fullmatch(name):
            failed["temporary_unlink"] = True
            raise OSError("fixture credential temporary unlink failure")
        return original_unlink(name, dir_fd=dir_fd)

    def reject_state_directory_close(descriptor):
        metadata = os.fstat(descriptor)
        if (
            directory_close_fails
            and failed["temporary_unlink"]
            and os.path.samestat(metadata, state_metadata)
        ):
            failed["directory_close"] = True
            leaked_descriptors.append(descriptor)
            raise OSError("fixture credential directory close failure")
        return original_close(descriptor)

    monkeypatch.setattr(_session.os, "fsync", reject_temporary_sync)
    monkeypatch.setattr(_session.os, "unlink", reject_temporary_unlink)
    monkeypatch.setattr(_session.os, "close", reject_state_directory_close)

    with pytest.raises(EngineUnavailableError) as failure:
        with backend._operation(CONNECTION, "models") as operation:
            session = _session.CodexSession(backend.runtime, CONNECTION, operation)
            session.codex_home.mkdir(parents=True, mode=0o700)
            credential = session.codex_home / "auth.json"
            credential.write_bytes(SYNTHETIC_CREDENTIAL)
            credential.chmod(0o600)
            session._authenticated = True
            session.close(persist=True)

    assert failed == {
        "directory_close": directory_close_fails,
        "file_sync": True,
        "temporary_unlink": True,
    }
    assert failure.value.details.get("reason") == "codex_credential_cleanup_unverified"
    assert SYNTHETIC_CREDENTIAL.decode() not in str(failure.value)
    temporaries = list(state.glob(".auth.*.tmp"))
    assert len(temporaries) == 1
    assert temporaries[0].read_bytes() == SYNTHETIC_CREDENTIAL
    assert not (state / "auth.json").exists()
    assert CONNECTION in backend._poisoned_connections

    with pytest.raises(EngineUnavailableError) as blocked:
        backend.list_models(CONNECTION)
    assert blocked.value.details.get("reason") == PROCESS_CLEANUP_REASON

    monkeypatch.setattr(_session.os, "fsync", original_fsync)
    monkeypatch.setattr(_session.os, "unlink", original_unlink)
    monkeypatch.setattr(_session.os, "close", original_close)
    for descriptor in leaked_descriptors:
        original_close(descriptor)
    _session.recover_connection_artifacts(root, CONNECTION)
    assert not temporaries[0].exists()

    with pytest.raises(EngineUnavailableError) as shutdown:
        backend.close()
    assert shutdown.value.details.get("reason") == PROCESS_CLEANUP_REASON
