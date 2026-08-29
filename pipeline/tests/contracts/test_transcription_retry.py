"""Confirmed transcription retries and safe unknown-outcome error propagation."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError
from narumi.contracts import ContractSet, load_contracts
from narumi.errors import ContractMismatchError, InvalidArgumentError

TRANSCRIPTION_MODEL = {
    "provider": "openai-api",
    "connection_id": "conn-0123456789ab",
    "connection_revision": 1,
    "model_id": "whisper-1",
}
RETRY = {
    "input_fingerprint": "a" * 64,
    "chunk_fingerprint": "b" * 64,
    "blocked_epoch": 0,
}
UNKNOWN_DETAILS = {
    **RETRY,
    "stage": "transcribe",
    "reason": "transcription_outcome_unknown",
    "outcome_unknown": True,
    "track": "mic",
    "chunk_index": 0,
    "chunk_count": 1,
    "completed_chunks": 0,
    "start_sample": 0,
    "end_sample": 160000,
    "sample_rate": 16000,
}
INVALID_DIGESTS = [
    "",
    "a" * 63,
    "a" * 65,
    "A" * 64,
    "g" * 64,
    "a" * 64 + "\n",
    None,
    True,
    12,
]
INTEGER_BOUNDS = {
    "blocked_epoch": (0, None),
    "chunk_index": (0, 143),
    "chunk_count": (1, 144),
    "completed_chunks": (0, 144),
    "start_sample": (0, 1382399999),
    "end_sample": (1, 1382400000),
}
UNSAFE_FIELDS = ["api_key", "token", "path", "chunk_path", "endpoint", "command", "raw_response"]


@pytest.fixture(scope="module")
def contracts() -> ContractSet:
    return load_contracts()


@pytest.fixture(params=["error_envelope", "get_job_status"])
def error_surface(request: pytest.FixtureRequest) -> str:
    return request.param


def _retry_args(contracts: ContractSet) -> dict[str, Any]:
    args = deepcopy(contracts["regenerate"].input_examples[0])
    config = deepcopy(contracts["get_meeting"].output_examples[0]["config"])
    config.update(transcription_model=deepcopy(TRANSCRIPTION_MODEL), external_send_policy="api_ok")
    args.update(expected_config=config, transcription_retry=deepcopy(RETRY))
    return args


def _validate_error(contracts: ContractSet, surface: str, error: dict[str, Any]) -> None:
    if surface == "error_envelope":
        contracts.validate_error_envelope({"error": error})
        return
    payload = deepcopy(contracts["get_job_status"].output_examples[0])
    payload["job"].update(status="failed", error=error)
    payload["job"].pop("progress", None)
    contracts.validate_output("get_job_status", payload)


def _unknown_error(details: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": "engine_unavailable",
        "message": "Transcription outcome is unknown; explicit confirmation is required.",
        "details": details,
    }


@pytest.mark.parametrize("model_id", ["whisper-1", "gpt-4o-transcribe-diarize"])
@pytest.mark.parametrize("selection_options", [{}, {"parameters": {}, "cache_epoch": 1}])
def test_retry_accepts_confirmed_transcription_snapshots(
    contracts: ContractSet, model_id: str, selection_options: dict[str, Any]
) -> None:
    args = _retry_args(contracts)
    args["expected_config"]["transcription_model"].update(model_id=model_id, **selection_options)
    contracts.validate_input("regenerate", args)
    args["force"] = False
    contracts.validate_input("regenerate", args)


def test_regenerate_without_retry_preserves_legacy_snapshot_omission(
    contracts: ContractSet,
) -> None:
    args = deepcopy(contracts["regenerate"].input_examples[0])
    contracts.validate_input("regenerate", args)
    args["force"] = True
    contracts.validate_input("regenerate", args)


@pytest.mark.parametrize("snapshot", ["omitted", "null", "selection_omitted", "selection_null"])
def test_retry_requires_a_nonnull_transcription_snapshot(
    contracts: ContractSet, snapshot: str
) -> None:
    args = _retry_args(contracts)
    if snapshot == "omitted":
        del args["expected_config"]
    elif snapshot == "null":
        args["expected_config"] = None
    elif snapshot == "selection_omitted":
        del args["expected_config"]["transcription_model"]
    else:
        args["expected_config"]["transcription_model"] = None
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("regenerate", args)


def test_retry_rejects_force_true(contracts: ContractSet) -> None:
    args = _retry_args(contracts)
    args["force"] = True
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("regenerate", args)


@pytest.mark.parametrize("field", list(RETRY))
def test_retry_requires_both_fingerprints_and_the_blocked_epoch(
    contracts: ContractSet, field: str
) -> None:
    args = _retry_args(contracts)
    del args["transcription_retry"][field]
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("regenerate", args)


@pytest.mark.parametrize("field", ["input_fingerprint", "chunk_fingerprint"])
@pytest.mark.parametrize("value", INVALID_DIGESTS)
def test_retry_requires_lowercase_sha256_fingerprints(
    contracts: ContractSet, field: str, value: Any
) -> None:
    args = _retry_args(contracts)
    args["transcription_retry"][field] = value
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("regenerate", args)


@pytest.mark.parametrize("value", [-1, True, False, None, "0", 0.5, [], {}])
def test_retry_rejects_invalid_blocked_epochs(contracts: ContractSet, value: Any) -> None:
    args = _retry_args(contracts)
    args["transcription_retry"]["blocked_epoch"] = value
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("regenerate", args)


@pytest.mark.parametrize("field", UNSAFE_FIELDS)
def test_retry_does_not_accept_secret_or_runtime_fields(contracts: ContractSet, field: str) -> None:
    args = _retry_args(contracts)
    args["transcription_retry"][field] = "fixture-only-value"
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("regenerate", args)


@pytest.mark.parametrize("value", [None, True, "retry", [], 0])
def test_retry_requires_an_object_when_present(contracts: ContractSet, value: Any) -> None:
    args = _retry_args(contracts)
    args["transcription_retry"] = value
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("regenerate", args)


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "codex-app-server"},
        {"model_id": "unverified-model"},
        {"api_key": "fixture-only-value"},
        {"parameters": {"path": "/tmp/fixture-only"}},
    ],
)
def test_retry_snapshot_uses_the_closed_transcription_selection(
    contracts: ContractSet, changes: dict[str, Any]
) -> None:
    args = _retry_args(contracts)
    args["expected_config"]["transcription_model"].update(changes)
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("regenerate", args)


@pytest.mark.parametrize(
    "changes",
    [
        {"end_sample": 1},
        {**TRANSCRIPTION_MODEL, "track": "system", "blocked_epoch": 7},
        {
            **TRANSCRIPTION_MODEL,
            "model_id": "gpt-4o-transcribe-diarize",
            "chunk_index": 143,
            "chunk_count": 144,
            "completed_chunks": 144,
            "start_sample": 1382399999,
            "end_sample": 1382400000,
        },
    ],
)
def test_unknown_outcome_metadata_propagates_on_both_error_surfaces(
    contracts: ContractSet, error_surface: str, changes: dict[str, Any]
) -> None:
    _validate_error(contracts, error_surface, _unknown_error({**UNKNOWN_DETAILS, **changes}))


@pytest.mark.parametrize("field", [key for key in UNKNOWN_DETAILS if key != "reason"])
def test_unknown_outcome_requires_all_receipt_and_chunk_metadata(
    contracts: ContractSet, error_surface: str, field: str
) -> None:
    details = {key: value for key, value in UNKNOWN_DETAILS.items() if key != field}
    with pytest.raises(ContractMismatchError):
        _validate_error(contracts, error_surface, _unknown_error(details))


@pytest.mark.parametrize(
    "changes", [{}, {"reason": None}, {"reason": "another_reason"}, {"reason": 1}]
)
def test_dedicated_unknown_details_definition_requires_its_exact_reason(
    contracts: ContractSet, changes: dict[str, Any]
) -> None:
    validator = Draft202012Validator(
        contracts.schema_for_def("transcription_outcome_unknown_details")
    )
    details = {key: value for key, value in UNKNOWN_DETAILS.items() if key != "reason"}
    details.update(changes)
    with pytest.raises(ValidationError):
        validator.validate(details)


@pytest.mark.parametrize("field", ["input_fingerprint", "chunk_fingerprint"])
@pytest.mark.parametrize("value", INVALID_DIGESTS)
def test_unknown_outcome_rejects_invalid_fingerprints(
    contracts: ContractSet, error_surface: str, field: str, value: Any
) -> None:
    with pytest.raises(ContractMismatchError):
        _validate_error(contracts, error_surface, _unknown_error({**UNKNOWN_DETAILS, field: value}))


@pytest.mark.parametrize(
    ("field", "value"),
    [(field, minimum - 1) for field, (minimum, _) in INTEGER_BOUNDS.items()]
    + [
        (field, maximum + 1)
        for field, (_, maximum) in INTEGER_BOUNDS.items()
        if maximum is not None
    ]
    + [
        ("stage", "generate"),
        ("outcome_unknown", False),
        ("outcome_unknown", 1),
        ("track", "combined"),
        ("sample_rate", 8000),
        ("provider", "anthropic-api"),
        ("model_id", "unverified-model"),
        ("connection_id", "../fixture"),
        ("connection_revision", 0),
    ],
)
def test_unknown_outcome_rejects_unsupported_values_and_out_of_range_metadata(
    contracts: ContractSet, error_surface: str, field: str, value: Any
) -> None:
    with pytest.raises(ContractMismatchError):
        _validate_error(contracts, error_surface, _unknown_error({**UNKNOWN_DETAILS, field: value}))


@pytest.mark.parametrize("field", [*INTEGER_BOUNDS, "sample_rate", "connection_revision"])
@pytest.mark.parametrize("value", [True, None, "1", 0.5])
def test_unknown_outcome_does_not_coerce_integer_metadata(
    contracts: ContractSet, error_surface: str, field: str, value: Any
) -> None:
    with pytest.raises(ContractMismatchError):
        _validate_error(contracts, error_surface, _unknown_error({**UNKNOWN_DETAILS, field: value}))


@pytest.mark.parametrize("field", UNSAFE_FIELDS)
def test_unknown_outcome_does_not_expose_secret_or_runtime_fields(
    contracts: ContractSet, error_surface: str, field: str
) -> None:
    with pytest.raises(ContractMismatchError):
        _validate_error(
            contracts,
            error_surface,
            _unknown_error({**UNKNOWN_DETAILS, field: "fixture-only-value"}),
        )


@pytest.mark.parametrize(
    "details",
    [
        None,
        {},
        {"reason": "another_reason", "extra": {"retryable": False}},
        {"reason": None, "outcome_unknown": False},
        {"stage": "transcribe", "input_fingerprint": "not-a-fingerprint"},
    ],
)
def test_unrelated_error_details_keep_the_existing_open_contract(
    contracts: ContractSet, error_surface: str, details: dict[str, Any] | None
) -> None:
    error: dict[str, Any] = {"code": "internal", "message": "Fixture error"}
    if details is not None:
        error["details"] = details
    _validate_error(contracts, error_surface, error)
