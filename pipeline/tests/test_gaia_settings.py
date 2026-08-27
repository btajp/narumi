"""Gaia settings precedence and partial updates, using only temporary roots and fake keys."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from narumi.errors import InvalidArgumentError, NarumiError
from narumi.gaia.settings import (
    ENV_GAIA_API_KEY,
    ENV_GAIA_URL,
    GAIA_CONNECTION_FILE,
    GaiaConnectionStore,
    get_default_gaia_client,
)

URL = "http://127.0.0.1:4111/mcp"
OTHER_URL = "http://127.0.0.1:4222/mcp"
KEY = "fake-settings-secret-348651"


@pytest.fixture
def store(tmp_path: Path) -> GaiaConnectionStore:
    return GaiaConnectionStore(tmp_path / "private" / GAIA_CONNECTION_FILE, environ={})


def stored_key(store: GaiaConnectionStore) -> str | None:
    return json.loads(store.path.read_text())["api_key"]


def test_missing_settings_are_unconfigured_without_creating_files(store: GaiaConnectionStore):
    assert store.get() == {"url": None, "has_api_key": False, "source": "unconfigured"}
    assert store.client() is None
    assert not store.path.parent.exists()


def test_environment_is_used_only_until_a_saved_file_exists(tmp_path: Path):
    environ = {ENV_GAIA_URL: URL, ENV_GAIA_API_KEY: KEY}
    store = GaiaConnectionStore(tmp_path / GAIA_CONNECTION_FILE, environ=environ)
    assert store.get() == {"url": URL, "has_api_key": True, "source": "environment"}
    assert store.set(url=URL) == {"url": URL, "has_api_key": True, "source": "saved"}
    assert stored_key(store) == KEY
    environ[ENV_GAIA_URL] = f"https://example.com/{KEY}"
    environ[ENV_GAIA_API_KEY] = "invalid\ncredential"
    assert store.get() == {"url": URL, "has_api_key": True, "source": "saved"}


def test_saved_null_url_disables_environment_and_clears_key(tmp_path: Path):
    store = GaiaConnectionStore(
        tmp_path / GAIA_CONNECTION_FILE, environ={ENV_GAIA_URL: URL, ENV_GAIA_API_KEY: KEY}
    )
    assert store.set(url=None, api_key="replacement-key") == {
        "url": None,
        "has_api_key": False,
        "source": "saved",
    }
    assert stored_key(store) is None
    assert store.client() is None
    assert GaiaConnectionStore(store.path, environ={ENV_GAIA_URL: OTHER_URL}).get() == store.get()


def test_same_url_preserves_key_and_url_change_clears_it(store: GaiaConnectionStore):
    store.set(url=URL, api_key=KEY)
    assert store.set(url=URL)["has_api_key"] is True
    assert stored_key(store) == KEY
    assert store.set(url=OTHER_URL) == {
        "url": OTHER_URL,
        "has_api_key": False,
        "source": "saved",
    }
    assert stored_key(store) is None
    store.set(url=URL, api_key="replacement-key")
    assert stored_key(store) == "replacement-key"


def test_key_only_replacement_and_clear_preserve_url(store: GaiaConnectionStore):
    store.set(url=URL, api_key=KEY)
    assert store.set(api_key="replacement-key")["url"] == URL
    assert stored_key(store) == "replacement-key"
    assert store.set(api_key=None) == {"url": URL, "has_api_key": False, "source": "saved"}
    assert stored_key(store) is None


def test_canonical_url_equivalence_does_not_clear_key(store: GaiaConnectionStore):
    store.set(url="http://localhost:4111/mcp", api_key=KEY)
    assert store.set(url=URL) == {"url": URL, "has_api_key": True, "source": "saved"}
    assert stored_key(store) == KEY


@pytest.mark.parametrize("arguments", [{}, {"api_key": KEY}, {"api_key": None}])
def test_empty_update_and_key_without_url_are_rejected(
    store: GaiaConnectionStore, arguments: dict[str, Any]
):
    with pytest.raises(InvalidArgumentError):
        store.set(**arguments)
    assert not store.path.exists()


@pytest.mark.parametrize(
    "key", ["", " ", "token value", "token\n", "\rtoken", "token\t", "token\x00", "鍵", KEY * 500]
)
def test_invalid_credentials_are_not_printed_or_saved(store: GaiaConnectionStore, key: str):
    with pytest.raises(InvalidArgumentError) as error:
        store.set(url=URL, api_key=key)
    assert KEY not in str(error.value)
    assert not store.path.exists()


@pytest.mark.parametrize("key", [True, 42, [KEY], {KEY: KEY}])
def test_wrong_type_credentials_are_rejected_without_raw_values(
    store: GaiaConnectionStore, key: Any
):
    with pytest.raises(InvalidArgumentError) as error:
        store.set(url=URL, api_key=key)
    assert KEY not in str(error.value)
    assert KEY not in repr(error.value.to_payload())


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:4111/mcp",
        "http://example.com/mcp",
        "http://192.168.0.10/mcp",
        f"http://narumi:{KEY}@127.0.0.1/mcp",
        f"http://127.0.0.1/mcp?api_key={KEY}",
        f"http://127.0.0.1/mcp#{KEY}",
        "http://127.0.0.1/mcp\nother",
        "http://127.0.0.1/mcp name",
        f"http://127.0.0.1/{'x' * 2048}",
        {KEY: KEY},
    ],
)
def test_unsafe_urls_are_rejected_without_echo(store: GaiaConnectionStore, url: Any):
    with pytest.raises(InvalidArgumentError) as error:
        store.set(url=url, api_key=KEY)
    assert KEY not in str(error.value)
    assert not store.path.exists()


def test_credential_cannot_be_revealed_through_url_on_change(store: GaiaConnectionStore):
    store.set(url=URL, api_key=KEY)
    with pytest.raises(InvalidArgumentError):
        store.set(url=f"http://127.0.0.1/{KEY}")
    assert store.get()["url"] == URL


@pytest.mark.parametrize(
    "environment",
    [
        {ENV_GAIA_URL: f"http://{KEY}@example.com/mcp"},
        {ENV_GAIA_URL: URL, ENV_GAIA_API_KEY: f"{KEY}\n"},
    ],
)
def test_invalid_environment_is_generic_and_can_be_overridden(
    tmp_path: Path, environment: dict[str, str]
):
    store = GaiaConnectionStore(tmp_path / GAIA_CONNECTION_FILE, environ=environment)
    with pytest.raises(InvalidArgumentError) as error:
        store.get()
    assert str(error.value) == "Gaia environment connection settings are invalid"
    assert KEY not in repr(error.value.to_payload())
    assert store.set(url=None) == {"url": None, "has_api_key": False, "source": "saved"}


@pytest.mark.parametrize(
    "contents",
    [
        '{"api_key": "' + KEY,
        json.dumps({"version": KEY, "url": URL, "api_key": KEY}),
        json.dumps({"version": True, "url": URL, "api_key": KEY}),
        json.dumps({"version": 2, "url": URL, "api_key": KEY}),
        json.dumps({"version": 1, "url": URL, "api_key": {KEY: KEY}}),
        json.dumps({"version": 1, "url": None, "api_key": KEY}),
        json.dumps({"version": 1, "url": URL, "api_key": KEY, KEY: KEY}),
        json.dumps([KEY]),
        KEY * 1000,
    ],
)
def test_corrupt_saved_file_never_falls_back_or_exposes_values(tmp_path: Path, contents: str):
    path = tmp_path / GAIA_CONNECTION_FILE
    path.write_text(contents)
    store = GaiaConnectionStore(path, environ={ENV_GAIA_URL: OTHER_URL})
    for operation in (store.get, lambda: store.set(url=None)):
        with pytest.raises(NarumiError) as error:
            operation()
        assert error.value.code == "internal"
        assert KEY not in str(error.value)
        assert error.value.__suppress_context__ is True
    assert path.read_text() == contents


def test_client_factory_uses_private_effective_key_and_timeout(
    store: GaiaConnectionStore, monkeypatch: pytest.MonkeyPatch
):
    received: list[tuple[str, str | None, float]] = []

    class FakeClient:
        def __init__(self, url: str, *, api_key: str | None = None, timeout: float = 30):
            received.append((url, api_key, timeout))

    monkeypatch.setattr("narumi.gaia.settings.GaiaClient", FakeClient)
    store.set(url=URL, api_key=KEY)
    assert isinstance(store.client(timeout=3), FakeClient)
    assert received == [(URL, KEY, 3)]
    assert KEY not in repr(store)
    assert KEY not in repr(store._effective())
    assert KEY not in json.dumps(store.get())


def test_default_client_resolves_saved_config_only_in_isolated_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("NARUMI_HOME", str(tmp_path))
    monkeypatch.setenv(ENV_GAIA_URL, OTHER_URL)
    monkeypatch.setenv(ENV_GAIA_API_KEY, "fake-environment-key")
    store = GaiaConnectionStore(tmp_path / GAIA_CONNECTION_FILE, environ={})
    store.set(url=URL, api_key=KEY)
    received: list[tuple[str, str | None, float]] = []
    monkeypatch.setattr(
        "narumi.gaia.settings.GaiaClient",
        lambda url, *, api_key=None, timeout=30: received.append((url, api_key, timeout)),
    )
    get_default_gaia_client(timeout=4)
    assert received == [(URL, KEY, 4)]
