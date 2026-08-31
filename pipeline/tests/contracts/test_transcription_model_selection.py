"""API transcription selections share one closed contract across public surfaces."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from narumi.contracts import ContractSet, load_contracts
from narumi.errors import ContractMismatchError, InvalidArgumentError
from narumi.models import MeetingConfig
from pydantic import ValidationError as ModelValidationError

MEETING_ID = "20260827T030500Z-a1b2c3d4"
REQUEST_ID = "asr-selection-contract-request"
SELECTION = {
    "provider": "openai-api",
    "connection_id": "conn-111122223333",
    "connection_revision": 1,
    "model_id": "whisper-1",
}
MODELS = ["whisper-1", "gpt-4o-transcribe-diarize"]


@pytest.fixture(scope="module")
def contracts() -> ContractSet:
    return load_contracts()


@pytest.mark.parametrize("model_id", MODELS)
@pytest.mark.parametrize("options", [{}, {"parameters": {}, "cache_epoch": 3}])
def test_transcription_model_is_shared_across_meeting_and_profile_tools(
    contracts: ContractSet, model_id: str, options: dict[str, Any]
) -> None:
    config = {
        "transcription_model": {**SELECTION, "model_id": model_id, **options},
        "transcription_engine": "mlx-whisper",
        "external_send_policy": "api_ok",
        "language": "auto",
        "vocab_hints": ["narumi"],
    }
    for tool in ("set_meeting_config", "set_profile", "start_recording", "import_recording"):
        args = deepcopy(contracts[tool].input_examples[0])
        if tool == "set_meeting_config":
            args.update(config)
        else:
            args["config"] = config
        contracts.validate_input(tool, args)
    for tool in (
        "get_meeting",
        "set_meeting_config",
        "get_profile",
        "set_profile",
        "list_profiles",
    ):
        payload = deepcopy(contracts[tool].output_examples[0])
        if tool == "list_profiles":
            payload["profiles"][0]["config"].update(config)
        elif "profile" in payload:
            payload["profile"]["config"].update(config)
        else:
            payload["config"].update(config)
        contracts.validate_output(tool, payload)


def test_transcription_selection_preserves_local_engine_and_default_roundtrip(
    contracts: ContractSet,
) -> None:
    legacy = MeetingConfig(transcription_engine="mlx-whisper")
    assert legacy.transcription_model is None
    chosen = MeetingConfig.model_validate(
        {
            "transcription_engine": "mlx-whisper",
            "transcription_model": SELECTION,
            "external_send_policy": "api_ok",
            "language": "auto",
        }
    )
    assert chosen.transcription_engine == "mlx-whisper"
    assert chosen.transcription_model is not None
    assert chosen.transcription_model.model_dump() == {
        **SELECTION,
        "parameters": {},
        "cache_epoch": 0,
    }
    payload = deepcopy(contracts["set_meeting_config"].output_examples[0])
    payload["config"] = chosen.model_dump()
    contracts.validate_output("set_meeting_config", payload)


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "codex-app-server"},
        {"provider": "anthropic-api"},
        {"provider": "ollama"},
        {"provider": "unknown"},
        {"connection_id": "../connection"},
        {"connection_revision": 0},
        {"connection_revision": True},
        {"connection_revision": "1"},
        {"model_id": "gpt-4o-transcribe"},
        {"model_id": "gpt-4o-mini-transcribe"},
        {"model_id": ""},
        {"model_id": None},
        {"parameters": None},
        {"parameters": []},
        {"parameters": True},
        {"cache_epoch": -1},
        {"cache_epoch": True},
        {"cache_epoch": "0"},
        {"api_key": "example-not-a-real-key"},
        {"endpoint": "https://unapproved.example"},
        {"path": "/tmp/other-audio.wav"},
        {"transcription_retry": {"blocked_epoch": 0}},
    ],
)
def test_transcription_selection_rejects_unknown_or_unsafe_configuration(
    contracts: ContractSet, changes: dict[str, Any]
) -> None:
    selection = {**SELECTION, **changes}
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input(
            "set_meeting_config",
            {"meeting_id": MEETING_ID, "request_id": REQUEST_ID, "transcription_model": selection},
        )
    with pytest.raises(ModelValidationError):
        MeetingConfig.model_validate({"transcription_model": selection})


@pytest.mark.parametrize("field", list(SELECTION))
def test_transcription_selection_requires_explicit_identity(
    contracts: ContractSet, field: str
) -> None:
    selection = {key: value for key, value in SELECTION.items() if key != field}
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input(
            "set_meeting_config",
            {"meeting_id": MEETING_ID, "request_id": REQUEST_ID, "transcription_model": selection},
        )
    with pytest.raises(ModelValidationError):
        MeetingConfig.model_validate({"transcription_model": selection})


@pytest.mark.parametrize("model_id", MODELS)
@pytest.mark.parametrize(
    "parameter",
    [
        "prompt",
        "vocab_hints",
        "known_speaker_names",
        "known_speaker_references",
        "language",
        "response_format",
        "timestamp_granularities",
        "temperature",
        "chunking_strategy",
        "max_tokens",
        "reasoning_effort",
        "store",
        "tools",
        "api_key",
        "path",
    ],
)
def test_initial_transcription_parameters_are_empty_and_closed(
    contracts: ContractSet, model_id: str, parameter: str
) -> None:
    selection = {
        **SELECTION,
        "model_id": model_id,
        "parameters": {parameter: "unsupported-fixture-value"},
    }
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input(
            "set_meeting_config",
            {"meeting_id": MEETING_ID, "request_id": REQUEST_ID, "transcription_model": selection},
        )


@pytest.mark.parametrize("language", ["auto", "ja", "en"])
def test_api_transcription_uses_the_shared_language_setting(
    contracts: ContractSet, language: str
) -> None:
    contracts.validate_input(
        "set_meeting_config",
        {
            "meeting_id": MEETING_ID,
            "request_id": REQUEST_ID,
            "transcription_model": SELECTION,
            "language": language,
        },
    )


@pytest.mark.parametrize("language", ["ja-JP", "JA", "jpn", "", "ja\n", "auto\n", None, 1, True])
@pytest.mark.parametrize("tool", ["set_meeting_config", "set_profile"])
def test_api_transcription_language_rejects_non_iso_syntax(
    contracts: ContractSet, tool: str, language: Any
) -> None:
    args = deepcopy(contracts[tool].input_examples[0])
    config = {"transcription_model": SELECTION, "language": language}
    if tool == "set_meeting_config":
        args.update(config)
    else:
        args["config"] = config
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input(tool, args)


@pytest.mark.parametrize("config", [{}, {"transcription_model": None}])
def test_local_transcription_omission_and_clear_preserve_legacy_language(
    contracts: ContractSet, config: dict[str, Any]
) -> None:
    args = deepcopy(contracts["set_profile"].input_examples[0])
    args["config"] = {**config, "transcription_engine": "mlx-whisper", "language": "ja-JP"}
    contracts.validate_input("set_profile", args)
    contracts.validate_input(
        "set_meeting_config",
        {"meeting_id": MEETING_ID, "request_id": REQUEST_ID, **args["config"]},
    )


@pytest.mark.parametrize("model_id", MODELS)
@pytest.mark.parametrize("tool", ["regenerate", "register_context"])
def test_api_transcription_config_snapshot_reaches_generation_entry_points(
    contracts: ContractSet, model_id: str, tool: str
) -> None:
    args = deepcopy(contracts[tool].input_examples[0])
    args["expected_config"] = {
        "transcription_model": {**SELECTION, "model_id": model_id},
        "external_send_policy": "api_ok",
        "language": "auto",
    }
    contracts.validate_input(tool, args)
    if tool == "regenerate":
        with pytest.raises(InvalidArgumentError):
            contracts.validate_input(tool, {**args, "force": True})


def _set_server_transport_shape(payload: dict[str, Any], transports: list[str]) -> None:
    resident = "streamable-http" in transports
    caps = payload["capabilities"]
    caps["transports"] = transports
    caps["workflow"] = {
        "provider_connections": resident,
        "provider_models": resident,
        "stage_model_selection": resident,
        "ensemble_generation": False,
    }
    caps["minutes_model_providers"] = (
        ["codex-app-server", "anthropic-api", "ollama", "openai-api"] if resident else []
    )
    caps["transcription_model_providers"] = ["openai-api"] if resident else []
    payload["secure_transport"] = {
        "mode": "pinned_tls" if resident else "stdio" if "stdio" in transports else "unavailable",
        "tls_required": resident,
        "client_auth_required": resident,
    }


@pytest.mark.parametrize(
    "transports", [["streamable-http"], ["streamable-http", "stdio"], ["stdio"], []]
)
def test_transcription_capability_is_required_and_transport_specific(
    contracts: ContractSet, transports: list[str]
) -> None:
    payload = deepcopy(contracts["get_server_info"].output_examples[0])
    _set_server_transport_shape(payload, transports)
    caps = payload["capabilities"]
    contracts.validate_output("get_server_info", payload)
    caps["transcription_model_providers"] = (
        [] if "streamable-http" in transports else ["openai-api"]
    )
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_server_info", payload)
    del caps["transcription_model_providers"]
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_server_info", payload)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("workflow", "provider_connections", False),
        ("workflow", "provider_models", False),
        ("workflow", "stage_model_selection", False),
        ("workflow", "ensemble_generation", True),
        ("capabilities", "minutes_model_providers", []),
        ("secure_transport", "mode", "stdio"),
        ("secure_transport", "tls_required", False),
        ("secure_transport", "client_auth_required", False),
    ],
)
def test_resident_capability_shape_is_closed(
    contracts: ContractSet, section: str, field: str, value: Any
) -> None:
    payload = deepcopy(contracts["get_server_info"].output_examples[0])
    if section == "workflow":
        target = payload["capabilities"]["workflow"]
    elif section == "secure_transport":
        target = payload[section]
    else:
        target = payload["capabilities"]
    target[field] = value
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_server_info", payload)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("workflow", "provider_connections", True),
        ("workflow", "provider_models", True),
        ("workflow", "stage_model_selection", True),
        ("workflow", "ensemble_generation", True),
        ("capabilities", "minutes_model_providers", ["openai-api"]),
        ("capabilities", "transcription_model_providers", ["openai-api"]),
        ("secure_transport", "mode", "pinned_tls"),
        ("secure_transport", "tls_required", True),
        ("secure_transport", "client_auth_required", True),
    ],
)
def test_stdio_capability_shape_is_closed(
    contracts: ContractSet, section: str, field: str, value: Any
) -> None:
    payload = deepcopy(contracts["get_server_info"].output_examples[1])
    if section == "workflow":
        target = payload["capabilities"]["workflow"]
    elif section == "secure_transport":
        target = payload[section]
    else:
        target = payload["capabilities"]
    target[field] = value
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_server_info", payload)


@pytest.mark.parametrize(
    "providers",
    [["codex-app-server"], ["openai-api", "openai-api"], ["unknown"], "openai-api", None],
)
def test_transcription_provider_capabilities_are_closed(
    contracts: ContractSet, providers: Any
) -> None:
    payload = deepcopy(contracts["get_server_info"].output_examples[0])
    payload["capabilities"]["transcription_model_providers"] = providers
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_server_info", payload)


@pytest.mark.parametrize("roles", [["llm", "transcription"], ["transcription", "llm"]])
def test_openai_provider_discloses_both_supported_roles(
    contracts: ContractSet, roles: list[str]
) -> None:
    payload = deepcopy(contracts["list_providers"].output_examples[0])
    provider = next(item for item in payload["providers"] if item["provider_id"] == "openai-api")
    provider["roles"] = roles
    contracts.validate_output("list_providers", payload)


@pytest.mark.parametrize("tool", ["set_meeting_config", "set_profile"])
@pytest.mark.parametrize("selection", [None, SELECTION])
def test_configuration_saves_accept_optional_full_config_snapshots(
    contracts: ContractSet, tool: str, selection: dict[str, Any] | None
) -> None:
    current = MeetingConfig.model_validate(
        {
            "transcription_model": selection,
            "external_send_policy": "local_only" if selection is None else "api_ok",
        }
    ).model_dump(mode="json")
    replacement = None if selection is None else {**selection, "cache_epoch": 1}
    args = deepcopy(contracts[tool].input_examples[0])
    if tool == "set_meeting_config":
        args["transcription_model"] = replacement
    else:
        args["config"] = {"transcription_model": replacement}
    args["expected_config"] = current
    contracts.validate_input(tool, args)
    del args["expected_config"]
    contracts.validate_input(tool, args)


@pytest.mark.parametrize("tool", ["set_meeting_config", "set_profile"])
@pytest.mark.parametrize(
    "expected_config",
    [
        None,
        [],
        "latest",
        {"api_key": "example-not-a-real-key"},
        {"language": True},
        {"transcription_model": {**SELECTION, "cache_epoch": -1}},
        {"transcription_model": {**SELECTION, "parameters": {"prompt": "unsupported"}}},
    ],
)
def test_configuration_saves_validate_compare_and_set_snapshots(
    contracts: ContractSet, tool: str, expected_config: Any
) -> None:
    args = deepcopy(contracts[tool].input_examples[0])
    args["expected_config"] = expected_config
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input(tool, args)
