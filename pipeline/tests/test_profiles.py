"""Unit tests for ``narumi.profiles`` (ProfileStore / profiles.json)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from narumi.errors import ErrorCode, InvalidArgumentError, NarumiError, NotFoundError
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
