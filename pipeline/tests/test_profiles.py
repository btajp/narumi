"""Unit tests for ``narumi.profiles`` (ProfileStore / profiles.json)."""

from __future__ import annotations

import fcntl
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from narumi import profiles_io
from narumi.errors import (
    BusyError,
    ConfigurationConflictError,
    ErrorCode,
    InvalidArgumentError,
    NarumiError,
    NotFoundError,
)
from narumi.models import MeetingConfig
from narumi.profiles import DEFAULT_PROFILE, PROFILES_FILE, Profile, ProfileStore


@pytest.fixture
def store(tmp_path: Path) -> ProfileStore:
    return ProfileStore(tmp_path / PROFILES_FILE)


# ---------------------------------------------------------------------------- built-in default
def test_empty_store_has_builtin_default(store: ProfileStore):
    assert not store.path.exists()
    assert store.default_name == DEFAULT_PROFILE
    assert store.names() == [DEFAULT_PROFILE]
    profiles = store.list()
    assert [p.name for p in profiles] == [DEFAULT_PROFILE]
    default = store.get(DEFAULT_PROFILE)
    assert default == profiles[0]
    assert default.is_default is True
    assert default.config == MeetingConfig()
    assert default.scope is None and default.engagement is None
    assert default.export_destinations == []
    assert store.default() == default
    assert store.peek(DEFAULT_PROFILE) == default
    assert store.peek("nope") is None
    with pytest.raises(NotFoundError):
        store.get("nope")


def test_builtin_default_can_be_customised(store: ProfileStore):
    config = MeetingConfig(transcription_engine="fake", self_name="岡村")
    saved = store.set(DEFAULT_PROFILE, config=config, engagement="社内")
    assert saved.is_default is True
    assert saved.config == config
    assert saved.engagement == "社内"
    assert store.default_name == DEFAULT_PROFILE
    assert [p.name for p in store.list()] == [DEFAULT_PROFILE]


# ---------------------------------------------------------------------------- set semantics
def test_set_creates_and_partially_updates(store: ProfileStore):
    config = MeetingConfig(diarization_engine="fake")
    created = store.set(
        "customer",
        config=config,
        scope="cloudnative",
        engagement="acme",
        export_destinations=["markdown", "html"],
    )
    assert created == Profile(
        name="customer",
        config=config,
        scope="cloudnative",
        engagement="acme",
        export_destinations=["markdown", "html"],
        is_default=False,
    )
    # omitted keyword arguments keep the stored value; None clears
    updated = store.set("customer", engagement=None)
    assert updated.engagement is None
    assert updated.scope == "cloudnative"
    assert updated.config == config
    assert updated.export_destinations == ["markdown", "html"]
    replaced = store.set("customer", export_destinations=[])
    assert replaced.export_destinations == []
    assert replaced.scope == "cloudnative"
    # names order: built-in default first, the rest sorted
    store.set("alpha")
    assert store.names() == [DEFAULT_PROFILE, "alpha", "customer"]
    assert [p.name for p in store.list()] == [DEFAULT_PROFILE, "alpha", "customer"]


def test_set_persists_atomically(tmp_path: Path):
    path = tmp_path / PROFILES_FILE
    ProfileStore(path).set("customer", scope="cloudnative", make_default=True)
    assert path.exists()
    assert not path.with_suffix(".json.tmp").exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["default"] == "customer"
    assert data["profiles"]["customer"]["scope"] == "cloudnative"
    # a fresh store instance reads the same state back
    reopened = ProfileStore(path)
    assert reopened.default_name == "customer"
    assert reopened.get("customer").is_default is True
    assert reopened.get(DEFAULT_PROFILE).is_default is False


def test_make_default_switches_exactly_one(store: ProfileStore):
    store.set("a")
    store.set("b", make_default=True)
    assert store.default_name == "b"
    assert [(p.name, p.is_default) for p in store.list()] == [
        (DEFAULT_PROFILE, False),
        ("a", False),
        ("b", True),
    ]
    store.set(DEFAULT_PROFILE, make_default=True)
    assert store.default_name == DEFAULT_PROFILE
    assert store.get("b").is_default is False


def test_expected_config_prevents_overwriting_changed_profile(store: ProfileStore):
    original = MeetingConfig()
    updated = MeetingConfig(language="en")
    store.set("customer", config=updated, expected_config=original, scope="original")
    before = store.path.read_bytes()
    with pytest.raises(ConfigurationConflictError):
        store.set(
            "customer",
            config=MeetingConfig(language="fr"),
            expected_config=original,
            scope="replacement",
            make_default=True,
        )
    assert store.path.read_bytes() == before
    assert store.get("customer").config == updated
    assert store.default_name == DEFAULT_PROFILE


def test_new_profile_cas_compares_builtin_defaults(store: ProfileStore):
    with pytest.raises(ConfigurationConflictError):
        store.set("new", expected_config=MeetingConfig(language="en"))
    assert store.peek("new") is None
    assert not store.path.exists()
    created = store.set("new", expected_config=MeetingConfig(), config=MeetingConfig(language="ja"))
    assert created.config == MeetingConfig()


@pytest.mark.parametrize("same_store", [False, True])
def test_stores_cannot_both_save_the_same_expected_config(tmp_path: Path, same_store: bool):
    path = tmp_path / PROFILES_FILE
    barrier = Barrier(2)
    shared = ProfileStore(path)

    def update(language: str) -> str:
        store = shared if same_store else ProfileStore(path)
        barrier.wait(timeout=5)
        try:
            store.set(
                "customer", config=MeetingConfig(language=language), expected_config=MeetingConfig()
            )
        except ConfigurationConflictError:
            return "conflict"
        return language

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(update, ("en", "fr")))
    assert outcomes.count("conflict") == 1
    assert ProfileStore(path).get("customer").config.language == next(
        outcome for outcome in outcomes if outcome != "conflict"
    )


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_profile_lock_rejects_links_without_modifying_target(tmp_path: Path, link_kind: str):
    path = tmp_path / PROFILES_FILE
    target = tmp_path / "unrelated.txt"
    target.write_text("leave this unchanged", encoding="utf-8")
    target.chmod(0o600)
    lock = path.with_name(path.name + ".lock")
    if link_kind == "symlink":
        lock.symlink_to(target)
    else:
        os.link(target, lock)
    with pytest.raises(NarumiError, match="could not be locked"):
        ProfileStore(path).set("new")
    assert target.read_text(encoding="utf-8") == "leave this unchanged"
    assert not path.exists()


def test_profile_lock_contention_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / PROFILES_FILE
    lock = path.with_name(path.name + ".lock")
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    monkeypatch.setattr(profiles_io, "LOCK_TIMEOUT_SECONDS", 0)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BusyError):
            ProfileStore(path).set("new")
    finally:
        os.close(descriptor)
    assert not path.exists()


def test_failed_profile_replace_keeps_previous_settings(
    store: ProfileStore, monkeypatch: pytest.MonkeyPatch
):
    store.set("customer", config=MeetingConfig(language="en"))
    before = store.path.read_bytes()

    def fail_replace(*_args: object) -> None:
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(profiles_io.os, "replace", fail_replace)
    with pytest.raises(ConfigurationConflictError, match="may already have been saved"):
        store.set("customer", config=MeetingConfig(language="fr"))
    assert store.path.read_bytes() == before
    assert not list(store.path.parent.glob(f".{store.path.name}.*"))


@pytest.mark.parametrize("sync_phase", ["temporary", "directory"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_profile_sync_failure_distinguishes_before_and_after_publication(
    store: ProfileStore, monkeypatch: pytest.MonkeyPatch, sync_phase: str, cleanup_fails: bool
):
    store.set("customer", config=MeetingConfig(language="en"))
    original_sync = profiles_io.os.fsync
    sync_count = 0

    def fail_sync(descriptor: int) -> None:
        nonlocal sync_count
        sync_count += 1
        if sync_count == (1 if sync_phase == "temporary" else 2):
            raise OSError("synthetic sync failure")
        original_sync(descriptor)

    monkeypatch.setattr(profiles_io.os, "fsync", fail_sync)
    if cleanup_fails:

        def fail_cleanup(_path) -> None:
            raise OSError("synthetic cleanup failure")

        monkeypatch.setattr(profiles_io.os, "unlink", fail_cleanup)
    with pytest.raises(NarumiError) as failure:
        store.set("customer", config=MeetingConfig(language="fr"))
    saved = store.get("customer").config.language
    if sync_phase == "temporary":
        assert saved == "en"
        assert failure.value.code == ErrorCode.INTERNAL
    else:
        assert saved == "fr"
        assert failure.value.code == ErrorCode.CONFIGURATION_CONFLICT
        assert failure.value.details == {
            "reason": "profile_save_outcome_unknown",
            "outcome_unknown": True,
        }
    if not cleanup_fails:
        assert not list(store.path.parent.glob(f".{store.path.name}.*"))


def test_set_validates_shapes(store: ProfileStore):
    with pytest.raises(InvalidArgumentError):
        store.set("x" * 65)
    with pytest.raises(InvalidArgumentError):
        store.set("ok", scope="")
    with pytest.raises(InvalidArgumentError):
        store.set("ok", engagement="e" * 201)
    assert store.names() == [DEFAULT_PROFILE]  # nothing was persisted


# ---------------------------------------------------------------------------- delete
def test_delete_rules(store: ProfileStore):
    store.set("a")
    store.set("b", make_default=True)
    with pytest.raises(InvalidArgumentError):  # built-in default is never deletable
        store.delete(DEFAULT_PROFILE)
    with pytest.raises(InvalidArgumentError):  # current default is not deletable either
        store.delete("b")
    with pytest.raises(NotFoundError):
        store.delete("missing")
    store.delete("a")
    assert store.names() == [DEFAULT_PROFILE, "b"]
    with pytest.raises(NotFoundError):  # deleting twice
        store.delete("a")


# ---------------------------------------------------------------------------- broken files
def test_corrupt_file_raises_internal(tmp_path: Path):
    path = tmp_path / PROFILES_FILE
    path.write_text("{not json", encoding="utf-8")
    store = ProfileStore(path)
    with pytest.raises(NarumiError) as exc:
        store.list()
    assert exc.value.code == ErrorCode.INTERNAL
    path.write_text(json.dumps({"default": "ghost", "profiles": {}}), encoding="utf-8")
    with pytest.raises(NarumiError) as exc:
        store.default_name  # noqa: B018 - the property raises
    assert exc.value.code == ErrorCode.INTERNAL


@pytest.mark.parametrize("model_id", ["whisper-1", "gpt-4o-transcribe-diarize"])
def test_api_transcription_profile_roundtrip_preserves_local_selection(
    store: ProfileStore, model_id: str
):
    config = MeetingConfig.model_validate(
        {
            "transcription_engine": "fake",
            "transcription_model": {
                "provider": "openai-api",
                "connection_id": "conn-0123456789ab",
                "connection_revision": 2,
                "model_id": model_id,
                "parameters": {},
                "cache_epoch": 3,
            },
            "external_send_policy": "api_ok",
            "language": "auto",
        }
    )
    store.set("api-audio", config=config)
    reopened = ProfileStore(store.path)
    assert reopened.get("api-audio").config == config
    assert reopened.set("api-audio", engagement="internal").config == config

    local_config = MeetingConfig.model_validate(
        {**config.model_dump(), "transcription_model": None, "language": "ja-JP"}
    )
    reopened.set("api-audio", config=local_config)
    restored = ProfileStore(store.path).get("api-audio").config
    assert restored == local_config
    assert restored.transcription_engine == "fake"


@pytest.mark.parametrize("language", ["xx", "zz", "ja-JP"])
def test_profile_rejects_stored_invalid_api_transcription_language(
    store: ProfileStore, language: str
):
    store.set("api-audio")
    data = json.loads(store.path.read_text(encoding="utf-8"))
    data["profiles"]["api-audio"]["config"].update(
        transcription_model={
            "provider": "openai-api",
            "connection_id": "conn-0123456789ab",
            "connection_revision": 1,
            "model_id": "whisper-1",
        },
        external_send_policy="api_ok",
        language=language,
    )
    store.path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(NarumiError) as exc:
        ProfileStore(store.path).get("api-audio")
    assert exc.value.code == ErrorCode.INTERNAL
