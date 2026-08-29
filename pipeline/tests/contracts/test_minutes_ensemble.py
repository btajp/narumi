"""Minutes-ensemble settings, retry confirmation and public read-contract parity."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from narumi.contracts import ContractSet, load_contracts
from narumi.errors import ContractMismatchError, InvalidArgumentError
from narumi.minutes_ensemble import MinutesEnsembleSelection, MinutesRetry
from narumi.models import MeetingConfig
from narumi.profiles import Profile
from pydantic import ValidationError as ModelValidationError

MEETING_ID = "20260827T030500Z-a1b2c3d4"
REQUEST_ID = "ensemble-contract-request-001"
GENERATOR_1 = {
    "id": "gen-" + "1" * 32,
    "label": "OpenAI案",
    "selection": {
        "provider": "openai-api",
        "connection_id": "conn-111122223333",
        "connection_revision": 1,
        "model_id": "fixture-openai-api-text-model",
        "parameters": {"max_tokens": 4096},
        "cache_epoch": 0,
    },
}
GENERATOR_2 = {
    "id": "gen-" + "2" * 32,
    "label": "Anthropic案",
    "selection": {
        "provider": "anthropic-api",
        "connection_id": "conn-444455556666",
        "connection_revision": 1,
        "model_id": "fixture-anthropic-api-text-model",
        "parameters": {"max_tokens": 4096},
        "cache_epoch": 0,
    },
}
SYNTHESIZER = {
    "provider": "codex-app-server",
    "connection_id": "conn-fedcba987654",
    "connection_revision": 1,
    "model_id": "fixture-text-model",
    "parameters": {"reasoning_effort": "high"},
    "cache_epoch": 0,
}
ENSEMBLE = {
    "generators": [GENERATOR_1, GENERATOR_2],
    "synthesizer": SYNTHESIZER,
}
MINUTES_MODEL = {
    "provider": "codex-app-server",
    "connection_id": "conn-fedcba987654",
    "connection_revision": 1,
    "model_id": "fixture-text-model",
}
MINUTES_RETRY = {
    "run_id": "run-" + "3" * 32,
    "node_id": "node-" + "5" * 64,
    "call_id": "call-" + "8" * 64,
    "blocked_attempt_id": "attempt-" + "a" * 32,
}
TRANSCRIPTION_RETRY = {
    "input_fingerprint": "a" * 64,
    "chunk_fingerprint": "b" * 64,
    "blocked_epoch": 0,
}


@pytest.fixture(scope="module")
def contracts() -> ContractSet:
    return load_contracts()


def _effective_config() -> dict[str, Any]:
    return MeetingConfig.model_validate(
        {"minutes_ensemble": ENSEMBLE, "external_send_policy": "api_ok"}
    ).model_dump(mode="json")


def _retry_args(contracts: ContractSet) -> dict[str, Any]:
    args = deepcopy(contracts["regenerate"].input_examples[0])
    args.update(
        expected_config=_effective_config(),
        minutes_retry=deepcopy(MINUTES_RETRY),
        force=False,
    )
    return args


def test_ensemble_roundtrips_through_meeting_and_profile_models(
    contracts: ContractSet,
) -> None:
    legacy = MeetingConfig()
    assert legacy.minutes_ensemble is None
    selected = MeetingConfig.model_validate(
        {"minutes_ensemble": ENSEMBLE, "external_send_policy": "api_ok"}
    )
    assert selected.minutes_model is None
    assert selected.minutes_ensemble is not None
    assert selected.minutes_ensemble.model_dump() == ENSEMBLE
    assert Profile(name="ensemble", config=selected).config == selected
    contracts.validate_output(
        "set_meeting_config",
        {
            **deepcopy(contracts["set_meeting_config"].output_examples[0]),
            "config": selected.model_dump(mode="json"),
        },
    )


@pytest.mark.parametrize(
    "tool",
    ["set_meeting_config", "set_profile", "start_recording", "import_recording"],
)
def test_ensemble_selection_reaches_all_configuration_entry_points(
    contracts: ContractSet, tool: str
) -> None:
    args = deepcopy(contracts[tool].input_examples[0])
    config = {"minutes_ensemble": ENSEMBLE, "external_send_policy": "api_ok"}
    if tool == "set_meeting_config":
        args.update(config)
    else:
        args["config"] = config
    contracts.validate_input(tool, args)


@pytest.mark.parametrize(
    "tool",
    ["get_meeting", "set_meeting_config", "get_profile", "set_profile", "list_profiles"],
)
def test_ensemble_selection_reaches_all_configuration_outputs(
    contracts: ContractSet, tool: str
) -> None:
    payload = deepcopy(contracts[tool].output_examples[0])
    if tool == "list_profiles":
        config = payload["profiles"][0]["config"]
    elif "profile" in payload:
        config = payload["profile"]["config"]
    else:
        config = payload["config"]
    config.update(minutes_model=None, minutes_ensemble=deepcopy(ENSEMBLE))
    contracts.validate_output(tool, payload)


def test_expected_config_carries_the_full_ensemble_across_cas_and_generation(
    contracts: ContractSet,
) -> None:
    config = _effective_config()
    meeting = deepcopy(contracts["set_meeting_config"].input_examples[0])
    meeting.update(expected_config=config, minutes_ensemble=deepcopy(ENSEMBLE))
    contracts.validate_input("set_meeting_config", meeting)

    profile = deepcopy(contracts["set_profile"].input_examples[0])
    profile.update(expected_config=config, config={"minutes_ensemble": deepcopy(ENSEMBLE)})
    contracts.validate_input("set_profile", profile)

    regenerate = deepcopy(contracts["regenerate"].input_examples[0])
    regenerate.update(expected_config=config, force=False)
    contracts.validate_input("regenerate", regenerate)

    context = deepcopy(contracts["register_context"].input_examples[0])
    context.update(expected_config=config, auto_regenerate=True)
    contracts.validate_input("register_context", context)


def test_minutes_model_and_ensemble_are_mutually_exclusive(
    contracts: ContractSet,
) -> None:
    with pytest.raises(ModelValidationError):
        MeetingConfig.model_validate({"minutes_model": MINUTES_MODEL, "minutes_ensemble": ENSEMBLE})
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input(
            "set_meeting_config",
            {
                "meeting_id": MEETING_ID,
                "request_id": REQUEST_ID,
                "minutes_model": MINUTES_MODEL,
                "minutes_ensemble": ENSEMBLE,
            },
        )
    contracts.validate_input(
        "set_meeting_config",
        {
            "meeting_id": MEETING_ID,
            "request_id": REQUEST_ID,
            "minutes_model": None,
            "minutes_ensemble": ENSEMBLE,
        },
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"generators": [GENERATOR_1]},
        {"generators": [GENERATOR_1, GENERATOR_2, GENERATOR_1, GENERATOR_2, GENERATOR_1]},
        {"generators": [{**GENERATOR_1, "id": "gen-bad"}, GENERATOR_2]},
        {"generators": [{**GENERATOR_1, "label": "   "}, GENERATOR_2]},
        {"generators": [{**GENERATOR_1, "label": "x" * 81}, GENERATOR_2]},
        {
            "generators": [
                {**GENERATOR_1, "selection": {**GENERATOR_1["selection"], "path": "/tmp/x"}},
                GENERATOR_2,
            ]
        },
        {"credential": "fixture-only"},
    ],
)
def test_ensemble_rejects_invalid_identity_cardinality_and_secret_fields(
    contracts: ContractSet, changes: dict[str, Any]
) -> None:
    value = {**ENSEMBLE, **changes}
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input(
            "set_meeting_config",
            {
                "meeting_id": MEETING_ID,
                "request_id": REQUEST_ID,
                "minutes_ensemble": value,
            },
        )
    with pytest.raises(ModelValidationError):
        MinutesEnsembleSelection.model_validate(value)


def test_ensemble_pydantic_rejects_duplicate_generator_ids() -> None:
    value = {
        **ENSEMBLE,
        "generators": [GENERATOR_1, {**GENERATOR_2, "id": GENERATOR_1["id"]}],
    }
    with pytest.raises(ModelValidationError):
        MinutesEnsembleSelection.model_validate(value)


def test_minutes_retry_roundtrips_and_requires_confirmed_ensemble_snapshot(
    contracts: ContractSet,
) -> None:
    assert MinutesRetry.model_validate(MINUTES_RETRY).model_dump() == MINUTES_RETRY
    contracts.validate_input("regenerate", _retry_args(contracts))
    for mutation in ("missing", "null", "ensemble_missing", "ensemble_null"):
        args = _retry_args(contracts)
        if mutation == "missing":
            del args["expected_config"]
        elif mutation == "null":
            args["expected_config"] = None
        elif mutation == "ensemble_missing":
            del args["expected_config"]["minutes_ensemble"]
        else:
            args["expected_config"]["minutes_ensemble"] = None
        with pytest.raises(InvalidArgumentError):
            contracts.validate_input("regenerate", args)


def test_minutes_retry_is_exclusive_with_force_and_transcription_retry(
    contracts: ContractSet,
) -> None:
    args = _retry_args(contracts)
    args["force"] = True
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("regenerate", args)

    args = _retry_args(contracts)
    args["transcription_retry"] = TRANSCRIPTION_RETRY
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("regenerate", args)


@pytest.mark.parametrize("field", list(MINUTES_RETRY))
def test_minutes_retry_requires_all_opaque_identifiers(contracts: ContractSet, field: str) -> None:
    value = {key: item for key, item in MINUTES_RETRY.items() if key != field}
    args = _retry_args(contracts)
    args["minutes_retry"] = value
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("regenerate", args)
    with pytest.raises(ModelValidationError):
        MinutesRetry.model_validate(value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "run-" + "A" * 32),
        ("node_id", "node-" + "0" * 63),
        ("call_id", "../call"),
        ("blocked_attempt_id", "attempt-" + "g" * 32),
        ("path", "/tmp/receipt"),
        ("api_key", "fixture-only"),
    ],
)
def test_minutes_retry_rejects_invalid_or_unsafe_fields(
    contracts: ContractSet, field: str, value: Any
) -> None:
    retry = {**MINUTES_RETRY, field: value}
    args = _retry_args(contracts)
    args["minutes_retry"] = retry
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("regenerate", args)
    with pytest.raises(ModelValidationError):
        MinutesRetry.model_validate(retry)


def test_minutes_ensemble_limits_are_required_capability_bounds(
    contracts: ContractSet,
) -> None:
    payload = deepcopy(contracts["get_server_info"].output_examples[0])
    limits = payload["capabilities"]["minutes_ensemble_limits"]
    assert limits == {
        "max_generators": 4,
        "max_concurrency": 1,
        "max_generation_attempts_per_run": 64,
        "input_modalities": ["text"],
        "max_reduction_depth": 6,
    }
    contracts.validate_output("get_server_info", payload)
    del payload["capabilities"]["minutes_ensemble_limits"]
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_server_info", payload)


def test_required_nullable_minutes_publication_provenance(
    contracts: ContractSet,
) -> None:
    legacy = deepcopy(contracts["get_minutes"].output_examples[0])
    ensemble = deepcopy(contracts["get_minutes"].output_examples[1])
    assert legacy["provenance"] is None
    assert ensemble["provider"] == "ensemble"
    assert ensemble["provenance"]["kind"] == "ensemble"

    missing = deepcopy(legacy)
    del missing["provenance"]
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_minutes", missing)
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_minutes", {**ensemble, "provenance": None})
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_minutes", {**legacy, "provenance": ensemble["provenance"]})


def test_success_job_result_requires_nullable_processing_run_id(
    contracts: ContractSet,
) -> None:
    successes = [
        deepcopy(item)
        for item in contracts["get_job_status"].output_examples
        if item["job"]["status"] == "succeeded"
    ]
    assert {item["job"]["kind"] for item in successes} == {"regenerate", "export"}
    for payload in successes:
        assert "processing_run_id" in payload["job"]["result"]
        contracts.validate_output("get_job_status", payload)
        del payload["job"]["result"]["processing_run_id"]
        with pytest.raises(ContractMismatchError):
            contracts.validate_output("get_job_status", payload)

    export = next(item for item in successes if item["job"]["kind"] == "export")
    export["job"]["result"]["processing_run_id"] = "run-" + "1" * 32
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_job_status", export)

    ensemble = deepcopy(
        next(
            item
            for item in contracts["get_job_status"].output_examples
            if "minutes_ensemble" in item["job"].get("result", {}).get("stages", [])
        )
    )
    ensemble["job"]["result"]["processing_run_id"] = None
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_job_status", ensemble)

    single = deepcopy(ensemble)
    single["job"]["processing_run_id"] = None
    single["job"]["result"]["stages"] = ["generate"]
    contracts.validate_output("get_job_status", single)


@pytest.mark.parametrize("tool", ["get_job_status", "cancel_job"])
def test_every_job_requires_nullable_processing_run_id(contracts: ContractSet, tool: str) -> None:
    for example in contracts[tool].output_examples:
        payload = deepcopy(example)
        assert "processing_run_id" in payload["job"]
        contracts.validate_output(tool, payload)
        del payload["job"]["processing_run_id"]
        with pytest.raises(ContractMismatchError):
            contracts.validate_output(tool, payload)

    known = [
        example["job"]
        for example in contracts[tool].output_examples
        if example["job"]["processing_run_id"] is not None
    ]
    assert known
    for job in known:
        if job.get("result", {}).get("processing_run_id") is not None:
            assert job["processing_run_id"] == job["result"]["processing_run_id"]


def test_processing_run_list_requires_nullable_cursor(
    contracts: ContractSet,
) -> None:
    payload = deepcopy(contracts["list_processing_runs"].output_examples[0])
    assert "next_cursor" in payload
    contracts.validate_output("list_processing_runs", payload)
    payload["next_cursor"] = "opaque_page_2"
    contracts.validate_output("list_processing_runs", payload)
    del payload["next_cursor"]
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("list_processing_runs", payload)


def test_processing_artifact_requires_top_level_reuse_flag(
    contracts: ContractSet,
) -> None:
    for example in contracts["get_processing_artifact"].output_examples:
        payload = deepcopy(example)
        assert payload["reused"] == payload["binding"]["reused"]
        contracts.validate_output("get_processing_artifact", payload)

        missing = deepcopy(payload)
        del missing["reused"]
        with pytest.raises(ContractMismatchError):
            contracts.validate_output("get_processing_artifact", missing)

        wrong_type = deepcopy(payload)
        wrong_type["reused"] = 1
        with pytest.raises(ContractMismatchError):
            contracts.validate_output("get_processing_artifact", wrong_type)

    description = contracts.schema_for_def("processing_artifact_response")["description"]
    assert "reused == binding.reused" in description


def test_ensemble_draft_requires_at_least_one_part(
    contracts: ContractSet,
) -> None:
    payload = deepcopy(contracts["get_processing_artifact"].output_examples[1])
    payload["artifact"]["kind"] = "draft"
    parts = [
        {
            "source_artifact_id": f"artifact-{index:032x}",
            "document_artifact_id": f"artifact-{index + 100:032x}",
        }
        for index in range(1, 66)
    ]
    payload["payload"] = {
        "schema_version": "ensemble-draft-v1",
        "parts": parts[:1],
    }
    contracts.validate_output("get_processing_artifact", payload)
    payload["payload"]["parts"] = []
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_processing_artifact", payload)
    payload["payload"]["parts"] = parts[:64]
    contracts.validate_output("get_processing_artifact", payload)
    payload["payload"]["parts"] = parts
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_processing_artifact", payload)


@pytest.mark.parametrize("kind", ["agenda", "discussion", "decision"])
@pytest.mark.parametrize("field", ["owner", "due"])
def test_non_action_claim_requires_null_assignment_fields(
    contracts: ContractSet, kind: str, field: str
) -> None:
    payload = deepcopy(contracts["get_processing_artifact"].output_examples[0])
    claim = payload["payload"]["claims"][0]
    claim["kind"] = kind
    claim.update(owner=None, due=None)
    contracts.validate_output("get_processing_artifact", payload)

    missing = deepcopy(payload)
    del missing["payload"]["claims"][0][field]
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_processing_artifact", missing)

    claim[field] = "担当"
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_processing_artifact", payload)


def test_action_claim_accepts_nullable_nonblank_assignment_fields(
    contracts: ContractSet,
) -> None:
    payload = deepcopy(contracts["get_processing_artifact"].output_examples[0])
    claim = payload["payload"]["claims"][0]
    claim.update(kind="action", owner="岡村", due="来週")
    contracts.validate_output("get_processing_artifact", payload)
    claim.update(owner=None, due=None)
    contracts.validate_output("get_processing_artifact", payload)

    for field in ("owner", "due"):
        invalid = deepcopy(payload)
        invalid["payload"]["claims"][0][field] = "   "
        with pytest.raises(ContractMismatchError):
            contracts.validate_output("get_processing_artifact", invalid)


def test_ensemble_claim_and_speaker_text_reject_whitespace_only(
    contracts: ContractSet,
) -> None:
    claim = deepcopy(contracts["get_processing_artifact"].output_examples[0])
    claim["payload"]["claims"][0]["text"] = "   "
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_processing_artifact", claim)

    for field in ("speaker_label", "speaker_name"):
        source = deepcopy(contracts["get_processing_artifact"].output_examples[1])
        source["payload"]["evidence"][0][field] = "   "
        with pytest.raises(ContractMismatchError):
            contracts.validate_output("get_processing_artifact", source)
        source["payload"]["evidence"][0][field] = None
        contracts.validate_output("get_processing_artifact", source)


def test_question_kind_controls_alternative_count_and_text(
    contracts: ContractSet,
) -> None:
    payload = deepcopy(contracts["get_processing_artifact"].output_examples[0])
    evidence = deepcopy(payload["payload"]["claims"][0]["evidence"])
    alternatives = [{"text": f"案{index}", "evidence": deepcopy(evidence)} for index in range(1, 6)]
    question = {
        "id": "qu_" + "9" * 64,
        "kind": "conflict",
        "text": "どちらの案を採用するか確認が必要。",
        "alternatives": alternatives[:1],
    }
    payload["payload"].update(claims=[], questions=[question])
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_processing_artifact", payload)

    question["alternatives"] = alternatives[:2]
    contracts.validate_output("get_processing_artifact", payload)
    question["alternatives"] = alternatives[:4]
    contracts.validate_output("get_processing_artifact", payload)
    question["alternatives"] = alternatives
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_processing_artifact", payload)

    question.update(kind="missing_context", alternatives=[])
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_processing_artifact", payload)
    question["alternatives"] = alternatives[:1]
    contracts.validate_output("get_processing_artifact", payload)
    question["alternatives"] = alternatives[:4]
    contracts.validate_output("get_processing_artifact", payload)
    question["alternatives"] = alternatives
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_processing_artifact", payload)

    question["alternatives"] = alternatives[:1]
    question["text"] = "   "
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_processing_artifact", payload)
    question["text"] = "追加情報が必要。"
    question["alternatives"][0]["text"] = "   "
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_processing_artifact", payload)


def test_processing_reads_require_slots_and_nullable_retry_lineage(
    contracts: ContractSet,
) -> None:
    run = deepcopy(contracts["get_processing_run"].output_examples[0])
    assert run["run"]["canonical_slots"]
    assert all("slot_id" in node and "retry_lineage" in node for node in run["run"]["nodes"])
    contracts.validate_output("get_processing_run", run)
    missing_slots = deepcopy(run)
    del missing_slots["run"]["canonical_slots"]
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_processing_run", missing_slots)
    missing_slot_id = deepcopy(run)
    del missing_slot_id["run"]["nodes"][0]["slot_id"]
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_processing_run", missing_slot_id)
    missing_lineage = deepcopy(run)
    del missing_lineage["run"]["nodes"][0]["retry_lineage"]
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_processing_run", missing_lineage)

    generated, derived = map(deepcopy, contracts["get_processing_artifact"].output_examples)
    assert generated["artifact"]["generation"]["retry_lineage"] is not None
    assert generated["binding"]["retry_lineage"] is not None
    assert derived["artifact"]["generation"] is None
    assert derived["binding"]["retry_lineage"] is None
    missing_generation_lineage = deepcopy(generated)
    del missing_generation_lineage["artifact"]["generation"]["retry_lineage"]
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_processing_artifact", missing_generation_lineage)
    del generated["binding"]["retry_lineage"]
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_processing_artifact", generated)
    derived["binding"]["retry_lineage"] = deepcopy(
        contracts["get_processing_artifact"].output_examples[0]["binding"]["retry_lineage"]
    )
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_processing_artifact", derived)
