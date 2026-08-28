"""Fail-closed configuration, authentication URLs and model metadata boundaries."""

from __future__ import annotations

import copy
import json
import os

import pytest
from narumi.errors import EngineUnavailableError, InvalidArgumentError, ModelUnavailableError
from narumi.providers.codex import _models, _policy


def _config(home, catalog=None):
    expected = copy.deepcopy(_policy.FIXED_CONFIG)
    if catalog:
        expected["model_catalog_json"] = str(catalog)
    return {
        "config": copy.deepcopy(expected),
        "origins": {},
        "layers": [
            {"name": {"type": "system", "file": "/etc/codex/config.toml"}, "config": {}},
            {"name": {"type": "user", "file": str(home / "config.toml")}, "config": {}},
            {"name": {"type": "sessionFlags"}, "config": expected},
        ],
    }


def _raw_model(**changes):
    result = {
        "id": "not-the-runtime-model-id",
        "model": "fixture-model",
        "displayName": "Fixture model",
        "hidden": False,
        "inputModalities": ["text", "image"],
        "supportedReasoningEfforts": [
            {"reasoningEffort": "low", "description": "Low"},
            {"reasoningEffort": "high", "description": "High"},
        ],
        "defaultReasoningEffort": "low",
    }
    result.update(changes)
    return result


def test_fixed_config_and_static_model_produce_no_enabled_tool_capabilities(tmp_path):
    models = _models.fetch_models(lambda *_: {"data": [_raw_model()]})
    safe = _policy.static_catalog(models[0])["models"][0]
    assert safe["slug"] == "fixture-model"
    assert safe["experimental_supported_tools"] == []
    assert safe["shell_type"] == "disabled"
    assert safe["tool_mode"] == "direct"
    assert safe["multi_agent_version"] == "disabled"
    assert safe["input_modalities"] == ["text"]
    assert safe["context_window"] is None
    assert safe["apply_patch_tool_type"] is None
    assert safe["node_repl_disabled"] is True
    assert safe["supports_search_tool"] is False
    assert not any(_policy.FIXED_CONFIG["features"].values())
    runtime_provider = _policy.FIXED_CONFIG["model_providers"][_policy.MODEL_PROVIDER]
    assert runtime_provider["requires_openai_auth"] is True
    assert runtime_provider["request_max_retries"] == runtime_provider["stream_max_retries"] == 0
    assert runtime_provider["supports_websockets"] is False
    assert not {"env_key", "auth", "experimental_bearer_token", "aws"} & runtime_provider.keys()
    arguments = _policy.command(tmp_path / "codex", catalog=tmp_path / "models.json")
    assert "--strict-config" in arguments
    assert f'model_provider="{_policy.MODEL_PROVIDER}"' in arguments
    assert f"model_catalog_json={json.dumps(str(tmp_path / 'models.json'))}" in arguments


@pytest.mark.parametrize("catalog", [None, "models.json"])
def test_only_expected_config_layers_are_accepted(tmp_path, catalog):
    path = tmp_path / catalog if catalog else None
    _policy.verify_configuration(_config(tmp_path, path), tmp_path, catalog=path)


def test_typed_config_projection_keeps_raw_extension_tool_flags_authoritative(tmp_path):
    body = _config(tmp_path)
    config = body["config"]
    config["features"]["network_proxy"] = None
    config["agents"]["max_threads"] = None
    config["history"]["max_bytes"] = None
    config["memories"]["extract_model"] = None
    config["shell_environment_policy"]["set"] = None
    config["model_providers"][_policy.MODEL_PROVIDER]["env_key"] = None
    config["tools"] = {"web_search": None}
    config["model"] = None
    config["mcp_servers"] = {}
    config["project_fallback_doc_filenames"] = []
    _policy.verify_configuration(body, tmp_path)


@pytest.mark.parametrize("value", [True, 0, None])
def test_omitted_public_tool_switch_never_weakens_raw_session_flag_check(tmp_path, value):
    body = _config(tmp_path)
    body["config"]["tools"] = {"web_search": None}
    body["layers"][-1]["config"]["tools"]["update_plan"]["enabled"] = value
    with pytest.raises(EngineUnavailableError):
        _policy.verify_configuration(body, tmp_path)


@pytest.mark.parametrize("location", ["config", "raw"])
def test_boolean_flags_are_not_coerced_from_numbers(tmp_path, location):
    body = _config(tmp_path)
    config = body["config"] if location == "config" else body["layers"][-1]["config"]
    config["features"]["shell_tool"] = 0
    with pytest.raises(EngineUnavailableError):
        _policy.verify_configuration(body, tmp_path)


def test_unknown_effective_feature_is_not_treated_as_a_harmless_projection_default(tmp_path):
    body = _config(tmp_path)
    body["config"]["features"]["future_tool"] = True
    with pytest.raises(EngineUnavailableError):
        _policy.verify_configuration(body, tmp_path)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("mcp_servers", {"untrusted": {"command": "fixture"}}),
        ("plugins", {"external": {"enabled": True}}),
        ("notify", ["fixture-command"]),
        ("model_provider", "other-provider"),
        ("instructions", "ambient instructions"),
        ("model_instructions_file", "/unrelated/file"),
        ("environments", {"remote": "fixture"}),
        ("sqlite_home", "/unrelated"),
    ],
)
def test_configuration_overrides_are_rejected_without_reflecting_content(tmp_path, key, value):
    body = _config(tmp_path)
    body["config"][key] = value
    with pytest.raises(EngineUnavailableError) as caught:
        _policy.verify_configuration(body, tmp_path)
    assert "unrelated" not in str(caught.value.to_payload())
    assert "fixture-command" not in str(caught.value.to_payload())


@pytest.mark.parametrize(
    "kind",
    ["mdm", "enterpriseManaged", "project", "packagedDefaults", "legacyManagedConfigTomlFromMdm"],
)
def test_even_empty_unexpected_layers_are_not_adopted(tmp_path, kind):
    body = _config(tmp_path)
    body["layers"].append({"name": {"type": kind}, "config": {}})
    with pytest.raises(EngineUnavailableError):
        _policy.verify_configuration(body, tmp_path)


def test_missing_duplicate_or_tampered_session_flags_fail_closed(tmp_path):
    for mutation in (
        lambda layers: layers.pop(),
        lambda layers: layers.append(copy.deepcopy(layers[-1])),
        lambda layers: layers[-1]["config"].update(notify=["fixture"]),
    ):
        body = _config(tmp_path)
        mutation(body["layers"])
        with pytest.raises(EngineUnavailableError):
            _policy.verify_configuration(body, tmp_path)


def test_host_configuration_presence_is_rejected_without_opening_contents(monkeypatch):
    actual = os.lstat

    def stat(path, *args, **kwargs):
        if path == "/etc/codex":
            return object()
        return actual(path, *args, **kwargs)

    monkeypatch.setattr(_policy.os, "lstat", stat)
    with pytest.raises(EngineUnavailableError) as caught:
        _policy.host_preflight()
    assert caught.value.details["reason"] == "codex_host_configuration_present"


@pytest.mark.parametrize("code", ["ABCD-EFGH", "abcd-1234", "A" * 32])
def test_official_device_authorization_page_and_code_are_accepted_together(code):
    value = _policy.DEVICE_AUTHORIZATION_URL
    assert _policy.device_authorization(value, code) == (value, code)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "https://auth.openai.com/oauth/authorize",
        "http://auth.openai.com/codex/device",
        "https://auth.openai.com.evil.invalid/codex/device",
        "https://user@auth.openai.com/codex/device",
        "https://auth.openai.com:443/codex/device",
        "https://auth.openai.com/codex/device/",
        "https://auth.openai.com/codex/device#secret",
        "https://auth.openai.com/codex/device?user_code=ABCD-EFGH",
        " https://auth.openai.com/codex/device",
        "https://auth.openai.com/codex/device\n",
    ],
)
def test_only_the_exact_device_page_is_accepted(value):
    with pytest.raises(EngineUnavailableError) as caught:
        _policy.device_authorization(value, "ABCD-EFGH")
    assert caught.value.details == {"reason": "codex_authorization_code_rejected"}
    assert "ABCD-EFGH" not in str(caught.value.to_payload())


@pytest.mark.parametrize(
    "code", [None, "", "A" * 33, "ABCD EFGH", "ABCD_EFGH", "ＡＢＣＤ", "ABCD\nEFGH"]
)
def test_device_codes_are_bounded_ascii_display_values_only(code):
    with pytest.raises(EngineUnavailableError) as caught:
        _policy.device_authorization(_policy.DEVICE_AUTHORIZATION_URL, code)
    assert caught.value.details == {"reason": "codex_authorization_code_rejected"}


def test_model_projection_is_text_only_unknown_prices_and_exact_runtime_id():
    model = _models.fetch_models(lambda *_: {"data": [_raw_model()]})[0]
    assert model["model_id"] == "fixture-model"
    assert model["source"] == "runtime"
    assert model["input_modalities"] == model["output_modalities"] == ["text"]
    assert model["context_window"] is None and model["max_output_tokens"] is None
    assert model["billing"]["kind"] == "subscription"
    assert model["billing"]["input_usd_per_million_tokens"] is None
    assert _models.select_model([model], "fixture-model", {"reasoning_effort": "high"}) == model
    with pytest.raises(ModelUnavailableError):
        _models.select_model([model], "not-the-runtime-model-id", {})
    with pytest.raises(InvalidArgumentError):
        _models.select_model([model], "fixture-model", {"reasoning_effort": "ultra"})
    with pytest.raises(InvalidArgumentError):
        _models.select_model([model], "fixture-model", {"command": "fixture"})


def test_missing_text_capability_is_not_assumed_available():
    model = _models.fetch_models(lambda *_: {"data": [_raw_model(inputModalities=["image"])]})[0]
    assert model["availability"] == "unverified"
    with pytest.raises(ModelUnavailableError):
        _models.select_model([model], "fixture-model", {})


@pytest.mark.parametrize(
    "changes",
    [
        {"model": "bad\nmodel"},
        {"supportedReasoningEfforts": []},
        {"defaultReasoningEffort": "missing"},
        {"supportedReasoningEfforts": [{"reasoningEffort": "bad effort"}]},
        {"hidden": "false"},
    ],
)
def test_malformed_model_catalog_is_rejected(changes):
    with pytest.raises(EngineUnavailableError):
        _models.fetch_models(lambda *_: {"data": [_raw_model(**changes)]})


def test_duplicate_models_and_repeated_pagination_fail_closed():
    with pytest.raises(EngineUnavailableError):
        _models.fetch_models(lambda *_: {"data": [_raw_model(), _raw_model()]})
    with pytest.raises(EngineUnavailableError):
        _models.fetch_models(lambda *_: {"data": [_raw_model(hidden=True)], "nextCursor": "next"})
