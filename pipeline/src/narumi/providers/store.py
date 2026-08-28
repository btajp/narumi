"""Versioned provider metadata storage; raw credentials never belong in this file."""

from __future__ import annotations

import copy
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from narumi.errors import ErrorCode, NarumiError
from narumi.providers import _io

_SECTIONS = (
    "connections",
    "catalogs",
    "auth_operations",
    "runtimes",
    "requests",
    "checks",
)
_SECRET_FIELDS = {
    "api_key",
    "access_token",
    "refresh_token",
    "id_token",
    "client_token",
    "authorization",
    "hmac_secret",
    "password",
    "secret",
}


@contextmanager
def public_errors() -> Iterator[None]:
    """Expose a fixed failure without echoing JSON, credentials or filesystem paths."""
    try:
        yield
    except NarumiError:
        raise
    except (OSError, ValueError, TypeError, RecursionError):
        raise NarumiError(
            "Provider settings could not be read or saved securely", code=ErrorCode.INTERNAL
        ) from None


def _empty_document() -> dict[str, Any]:
    return {"version": 1, **{section: {} for section in _SECTIONS}}


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate provider field")
        result[key] = value
    return result


def _validate_value(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or key.lower() in _SECRET_FIELDS:
                raise ValueError("invalid provider metadata field")
            _validate_value(child)
    elif isinstance(value, list):
        for child in value:
            _validate_value(child)
    elif value is not None and type(value) not in (str, int, float, bool):
        raise TypeError("invalid provider metadata value")


def _encode(document: dict[str, Any]) -> str:
    if not isinstance(document, dict) or set(document) != {"version", *_SECTIONS}:
        raise ValueError("invalid provider registry fields")
    if type(document["version"]) is not int or document["version"] != 1:
        raise ValueError("unsupported provider registry version")
    if any(not isinstance(document[name], dict) for name in _SECTIONS):
        raise ValueError("invalid provider registry section")
    _validate_value(document)
    return (
        json.dumps(document, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n"
    )


class ProviderStore:
    """Serialized metadata transactions with optional durable pre-side-effect commits.

    The transaction dictionary is private to its caller. ``commit`` is only legal for
    that same dictionary while its transaction is active. A second unchanged commit
    at normal context exit does not rewrite the file. Exceptions do not roll back an
    earlier explicit commit, which is how callers persist intent before a side effect.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.path = self.root / "providers" / _io.REGISTRY_NAME
        self._lock = threading.RLock()
        self._document: dict[str, Any] | None = None
        self._directory: int | None = None
        self._saved: str | None = None

    @contextmanager
    def _locked(self) -> Iterator[int]:
        # Only I/O failures are wrapped: exceptions raised by the transaction body
        # retain their type and do not expose a raw serialization error by accident.
        lock = _io.locked_directory(self.root)
        with public_errors():
            directory = lock.__enter__()
        try:
            yield directory
        finally:
            with public_errors():
                lock.__exit__(None, None, None)

    def _load(self, directory: int) -> tuple[dict[str, Any], str | None]:
        with public_errors():
            contents = _io.read_private(directory)
            if contents is None:
                return _empty_document(), None
            document = json.loads(contents, object_pairs_hook=_object)
            normalized = _encode(document)
            return document, normalized

    def _require_inactive(self) -> None:
        if self._document is not None:
            raise RuntimeError("Provider store transactions cannot be re-entered")

    def read(self) -> dict[str, Any]:
        """Return an independent metadata snapshot under the process-shared lock."""
        with self._lock:
            self._require_inactive()
            with self._locked() as directory:
                document, _ = self._load(directory)
                with public_errors():
                    return copy.deepcopy(document)

    @contextmanager
    def transaction(self) -> Iterator[dict[str, Any]]:
        with self._lock:
            self._require_inactive()
            with self._locked() as directory:
                document, saved = self._load(directory)
                self._document = document
                self._directory = directory
                self._saved = saved
                try:
                    yield document
                    self.commit(document)
                finally:
                    self._document = None
                    self._directory = None
                    self._saved = None

    def commit(self, document: dict[str, Any]) -> None:
        """Persist an active transaction before a non-transactional external effect."""
        with self._lock:
            if self._document is not document or self._directory is None:
                raise RuntimeError("Provider store commit requires its active transaction")
            with public_errors():
                contents = _encode(document)
                if contents != self._saved:
                    _io.replace_private(self._directory, contents)
                    self._saved = contents
