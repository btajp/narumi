"""Saved meeting profiles: the source of truth is ``<NARUMI_HOME>/profiles.json``.

A profile bundles config defaults, a default scope / engagement and automatic export
destinations. ``start_recording`` / ``import_recording`` apply the named (or default) profile as
defaults — explicit arguments win — and a successful process job exports the minutes to the
profile's ``export_destinations``. The built-in ``default`` profile (plain :class:`MeetingConfig`)
always exists; it can be customised but never deleted. Exactly one profile is the default at a
time (``is_default``).

This module only stores and validates shapes; policy / engine / exporter checks against the
running server's registries belong to the server handlers.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from narumi.errors import (
    ConfigurationConflictError,
    ErrorCode,
    InvalidArgumentError,
    NarumiError,
    NotFoundError,
)
from narumi.models import MeetingConfig
from narumi.profiles_io import replace_file, write_lock

DEFAULT_PROFILE: Final = "default"
PROFILES_FILE: Final = "profiles.json"

ProfileName = Annotated[str, StringConstraints(min_length=1, max_length=64)]
ScopeName = Annotated[str, StringConstraints(min_length=1, max_length=64)]
Engagement = Annotated[str, StringConstraints(max_length=200)]
Destination = Annotated[str, StringConstraints(min_length=1)]


class Profile(BaseModel):
    """One saved profile (mirrors ``contracts/defs/common.json#/$defs/profile``)."""

    model_config = ConfigDict(extra="forbid")

    name: ProfileName
    config: MeetingConfig = Field(default_factory=MeetingConfig)
    scope: ScopeName | None = None
    engagement: Engagement | None = None
    export_destinations: list[Destination] = Field(default_factory=list)
    is_default: bool = False


class _StoredProfile(BaseModel):
    """On-disk shape of one profile (``is_default`` is derived from the file's ``default``)."""

    model_config = ConfigDict(extra="forbid")

    config: MeetingConfig = Field(default_factory=MeetingConfig)
    scope: ScopeName | None = None
    engagement: Engagement | None = None
    export_destinations: list[Destination] = Field(default_factory=list)


class _StoreFile(BaseModel):
    """Top-level shape of ``profiles.json``."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    default: ProfileName = DEFAULT_PROFILE
    profiles: dict[ProfileName, _StoredProfile] = Field(default_factory=dict)


class _Unset:
    """Sentinel distinguishing "argument omitted" from an explicit ``None`` (= clear)."""

    def __repr__(self) -> str:  # pragma: no cover - repr only
        return "UNSET"


UNSET: Final = _Unset()


class ProfileStore:
    """Reads / writes ``profiles.json`` (atomic replace; read-modify-write is serialized).

    A missing file is an empty store: only the implicit built-in ``default`` profile exists.
    A corrupt or hand-edited-invalid file raises a structured ``internal`` error — it is never
    silently reset.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # ------------------------------------------------------------------ reads
    @property
    def default_name(self) -> str:
        """Name of the profile applied when ``profile`` is omitted."""
        return self._load().default

    def names(self) -> list[str]:
        """Every profile name, the built-in ``default`` first, the rest sorted."""
        store = self._load()
        return [DEFAULT_PROFILE, *sorted(n for n in store.profiles if n != DEFAULT_PROFILE)]

    def list(self) -> list[Profile]:
        store = self._load()
        rest = sorted(n for n in store.profiles if n != DEFAULT_PROFILE)
        return [self._profile(store, name) for name in (DEFAULT_PROFILE, *rest)]

    def get(self, name: str) -> Profile:
        """The named profile; :class:`NotFoundError` for an unknown name."""
        return self._profile(self._load(), name)

    def peek(self, name: str) -> Profile | None:
        """The named profile, or ``None`` when it does not exist (no error)."""
        try:
            return self.get(name)
        except NotFoundError:
            return None

    def default(self) -> Profile:
        """The profile currently marked ``is_default``."""
        store = self._load()
        return self._profile(store, store.default)

    # ------------------------------------------------------------------ writes
    def set(
        self,
        name: str,
        *,
        config: MeetingConfig | None = None,
        expected_config: MeetingConfig | None = None,
        scope: str | None | _Unset = UNSET,
        engagement: str | None | _Unset = UNSET,
        export_destinations: Sequence[str] | None = None,
        make_default: bool = False,
    ) -> Profile:
        """Create or update a profile; omitted arguments keep their stored value.

        ``config`` replaces the stored config wholesale (callers merge beforehand);
        ``scope`` / ``engagement`` accept ``None`` to clear; ``export_destinations`` replaces
        the stored list. ``make_default=True`` makes this the default profile.
        ``expected_config`` compares the previous effective config under the write lock;
        a new profile starts from the built-in config defaults.
        """
        with write_lock(self.path):
            store = self._load()
            stored = store.profiles.get(name, _StoredProfile())
            if expected_config is not None and stored.config != expected_config:
                raise ConfigurationConflictError("The profile configuration changed; reload it")
            data: dict[str, Any] = {
                "config": stored.config if config is None else config,
                "scope": stored.scope if isinstance(scope, _Unset) else scope,
                "engagement": (stored.engagement if isinstance(engagement, _Unset) else engagement),
                "export_destinations": (
                    list(stored.export_destinations)
                    if export_destinations is None
                    else list(dict.fromkeys(export_destinations))
                ),
            }
            try:
                Profile(name=name, **data)  # full validation (name, scope, … constraints)
                updated = _StoredProfile(**data)
            except ValidationError as exc:
                raise InvalidArgumentError(
                    f"invalid profile {name!r}",
                    details={"errors": json.loads(exc.json(include_url=False))},
                ) from exc
            store.profiles[name] = updated
            if make_default:
                store.default = name
            self._save(store)
            return self._profile(store, name)

    def delete(self, name: str) -> None:
        """Delete a profile; the built-in ``default`` and the current default are undeletable."""
        with write_lock(self.path):
            store = self._load()
            if name == DEFAULT_PROFILE:
                raise InvalidArgumentError(
                    "the built-in 'default' profile cannot be deleted", details={"name": name}
                )
            if name not in store.profiles:
                raise NotFoundError(
                    f"unknown profile: {name}",
                    details={"name": name, "known": self._names(store)},
                )
            if name == store.default:
                raise InvalidArgumentError(
                    f"profile {name!r} is the current default; make another profile the default"
                    " first (set_profile make_default=true)",
                    details={"name": name},
                )
            del store.profiles[name]
            self._save(store)

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _names(store: _StoreFile) -> list[str]:
        return [DEFAULT_PROFILE, *sorted(n for n in store.profiles if n != DEFAULT_PROFILE)]

    @staticmethod
    def _profile(store: _StoreFile, name: str) -> Profile:
        stored = store.profiles.get(name)
        if stored is None:
            if name != DEFAULT_PROFILE:
                raise NotFoundError(
                    f"unknown profile: {name}",
                    details={"name": name, "known": ProfileStore._names(store)},
                )
            stored = _StoredProfile()
        return Profile(
            name=name,
            config=stored.config,
            scope=stored.scope,
            engagement=stored.engagement,
            export_destinations=list(stored.export_destinations),
            is_default=(name == store.default),
        )

    def _load(self) -> _StoreFile:
        if not self.path.exists():
            return _StoreFile()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            store = _StoreFile.model_validate(data)
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise NarumiError(
                f"profiles file is unreadable or invalid: {self.path}: {exc}",
                code=ErrorCode.INTERNAL,
                details={"path": str(self.path)},
            ) from exc
        if store.default != DEFAULT_PROFILE and store.default not in store.profiles:
            raise NarumiError(
                f"profiles file names an unknown default profile {store.default!r}",
                code=ErrorCode.INTERNAL,
                details={"path": str(self.path), "default": store.default},
            )
        return store

    def _save(self, store: _StoreFile) -> None:
        try:
            replace_file(self.path, store.model_dump_json(indent=2) + "\n")
        except OSError:
            raise NarumiError(
                "Profile settings could not be saved", code=ErrorCode.INTERNAL
            ) from None
