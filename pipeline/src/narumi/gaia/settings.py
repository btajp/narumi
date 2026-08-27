"""Dedicated, write-only Gaia credentials, separate from profiles and meeting bundles.

The versioned connection file overrides environment settings, including an explicit null URL
that disables Gaia. Public projections only disclose the URL, credential presence and source.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

from narumi.config import data_root
from narumi.errors import ErrorCode, InvalidArgumentError, NarumiError
from narumi.gaia._protocol import local_url, validate_api_key
from narumi.gaia._settings_io import read_private, replace_private, write_lock
from narumi.gaia.client import GaiaClient

GAIA_CONNECTION_FILE: Final = "gaia.json"
ENV_GAIA_URL: Final = "NARUMI_GAIA_URL"
ENV_GAIA_API_KEY: Final = "NARUMI_GAIA_API_KEY"
_VERSION = 1


class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


UNSET: Final = _Unset()


@dataclass(frozen=True)
class _Connection:
    url: str | None
    _api_key: str | None = field(repr=False)
    source: Literal["saved", "environment", "unconfigured"]

    def public(self) -> dict[str, Any]:
        return {"url": self.url, "has_api_key": self._api_key is not None, "source": self.source}


def _url(value: Any, *, api_key: str | None = None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        raise InvalidArgumentError("Gaia URL must be a non-empty string of at most 2048 characters")
    return local_url(value, api_key=api_key)


def _key(value: Any) -> str | None:
    if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 4096):
        raise InvalidArgumentError("Gaia API key must be a string of 1 to 4096 characters, or null")
    validate_api_key(value)
    return value


class GaiaConnectionStore:
    """Owner-only JSON settings with atomic replacement and serialized partial updates.

    ``environ`` is optional for isolated callers and tests. No values are retained in the
    store's repr. The key is only read into a private value object and the transport client.
    """

    def __init__(self, path: Path, *, environ: Mapping[str, str] | None = None) -> None:
        self.path = Path(path)
        self._environ = environ
        self._lock = threading.Lock()

    def get(self) -> dict[str, Any]:
        """Read effective settings without connecting to Gaia or returning the credential."""
        return self._effective().public()

    def set(
        self,
        *,
        url: str | None | _Unset = UNSET,
        api_key: str | None | _Unset = UNSET,
    ) -> dict[str, Any]:
        """Persist supplied fields; URL changes clear the old credential unless replaced."""
        if url is UNSET and api_key is UNSET:
            raise InvalidArgumentError("supply a Gaia URL or API key to update")
        if api_key is not UNSET:
            _key(api_key)
        try:
            with self._lock, write_lock(self.path):
                try:
                    current = self._effective()
                except InvalidArgumentError:
                    if url is UNSET:
                        raise
                    # A valid explicit URL can repair invalid environment settings. Corrupt
                    # saved files raise INTERNAL instead and are never silently overwritten.
                    current = _Connection(None, None, "unconfigured")
                next_url = current.url if url is UNSET else _url(url, api_key=current._api_key)
                if url is UNSET and next_url is None:
                    raise InvalidArgumentError("a Gaia URL is required before saving an API key")
                next_key = current._api_key
                if next_url != current.url:
                    next_key = None
                if api_key is not UNSET:
                    next_key = api_key
                if next_url is None:
                    next_key = None
                else:
                    next_url = _url(next_url, api_key=next_key)
                updated = _Connection(next_url, next_key, "saved")
                contents = {"version": _VERSION, "url": updated.url, "api_key": updated._api_key}
                replace_private(
                    self.path, json.dumps(contents, ensure_ascii=False, indent=2) + "\n"
                )
                return updated.public()
        except OSError:
            raise NarumiError(
                "Gaia connection settings could not be saved", code=ErrorCode.INTERNAL
            ) from None

    def client(self, *, timeout: float = 30.0) -> GaiaClient | None:
        """Create a fresh transport client; an absent/disabled URL is the only null result."""
        connection = self._effective()
        if connection.url is None:
            return None
        return GaiaClient(connection.url, api_key=connection._api_key, timeout=timeout)

    def _effective(self) -> _Connection:
        saved = self._load_saved()
        if saved is not None:
            return saved
        environ = os.environ if self._environ is None else self._environ
        url = environ.get(ENV_GAIA_URL)
        if url in (None, ""):
            return _Connection(None, None, "unconfigured")
        try:
            api_key = _key(environ.get(ENV_GAIA_API_KEY) or None)
            return _Connection(_url(url, api_key=api_key), api_key, "environment")
        except InvalidArgumentError:
            raise InvalidArgumentError("Gaia environment connection settings are invalid") from None

    def _load_saved(self) -> _Connection | None:
        try:
            contents = read_private(self.path)
            if contents is None:
                return None
            document = json.loads(contents)
            if not isinstance(document, dict) or set(document) != {"version", "url", "api_key"}:
                raise ValueError("invalid connection document")
            if type(document["version"]) is not int or document["version"] != _VERSION:
                raise ValueError("unsupported connection version")
            api_key = _key(document["api_key"])
            url = _url(document["url"], api_key=api_key)
            if url is None and api_key is not None:
                raise ValueError("disabled connection contains a key")
            return _Connection(url, api_key, "saved")
        except (OSError, ValueError, InvalidArgumentError):
            raise NarumiError(
                "Saved Gaia connection settings are unreadable or invalid", code=ErrorCode.INTERNAL
            ) from None


def get_default_gaia_client(*, timeout: float = 30.0) -> GaiaClient | None:
    """Resolve the same dedicated settings for CLI/dev callers using the default data root."""
    return GaiaConnectionStore(data_root() / GAIA_CONNECTION_FILE).client(timeout=timeout)
