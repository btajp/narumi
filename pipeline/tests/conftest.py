"""Tests must never load a user's saved Gaia credentials or contact their server."""

import pytest


@pytest.fixture(autouse=True)
def isolated_gaia_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("NARUMI_HOME", str(tmp_path / "narumi-home"))
    monkeypatch.delenv("NARUMI_GAIA_URL", raising=False)
    monkeypatch.delenv("NARUMI_GAIA_API_KEY", raising=False)
