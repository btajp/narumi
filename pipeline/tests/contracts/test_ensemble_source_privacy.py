"""Closed public identifier contracts for ensemble source documents."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from narumi.contracts import ContractSet, load_contracts


@pytest.fixture(scope="module")
def contracts() -> ContractSet:
    return load_contracts()


@pytest.fixture(scope="module")
def binding_validator(contracts: ContractSet) -> Draft202012Validator:
    return Draft202012Validator(contracts.schema_for_def("ensemble_source_binding"))


def _binding(**changes: Any) -> dict[str, Any]:
    value = {
        "segment_index": 0,
        "segment_id": "segment-" + "a" * 64,
        "segment_text_sha256": "b" * 64,
        "sources": ["merged", "mic", "system", "own-mic", "own-system"],
    }
    value.update(changes)
    return value


@pytest.mark.parametrize(
    "segment_id",
    [
        "segment-" + "a" * 64,
        "m-0",
        "m-42",
        "merged-segment-0003",
    ],
)
def test_source_binding_accepts_only_opaque_or_closed_legacy_segment_ids(
    binding_validator: Draft202012Validator,
    segment_id: str,
) -> None:
    binding_validator.validate(_binding(segment_id=segment_id))


@pytest.mark.parametrize(
    "segment_id",
    [
        "/Users/alice/private/merged.json",
        "../private/merged.json",
        "file:///Users/alice/private/merged.json",
        "segment-api_key_live_secret",
        "segment-token-deadbeef",
        "segment-sk-proj-private",
        "segment-public-safe",
        "segment-" + "A" * 64,
    ],
)
def test_source_binding_rejects_paths_credentials_tokens_and_arbitrary_segment_labels(
    binding_validator: Draft202012Validator,
    segment_id: str,
) -> None:
    with pytest.raises(JsonSchemaValidationError):
        binding_validator.validate(_binding(segment_id=segment_id))


@pytest.mark.parametrize(
    "source",
    [
        "merged",
        "mic",
        "system",
        "own-mic",
        "own-system",
        "ext-ctx-deadbeef",
        "ext-ctx-0123456789abcdef0123456789abcdef",
        "source-" + "c" * 64,
    ],
)
def test_source_binding_accepts_only_closed_public_source_labels(
    binding_validator: Draft202012Validator,
    source: str,
) -> None:
    binding_validator.validate(_binding(sources=[source]))


@pytest.mark.parametrize(
    "source",
    [
        "/tmp/private/system.wav",
        "../private/system.wav",
        "file:///tmp/private/system.wav",
        "api_key_live_secret",
        "credential-deadbeef",
        "token-deadbeef",
        "Bearer private-token",
        "sk-proj-private",
        "system-secret",
        "own-mic:0",
        "source-" + "D" * 64,
    ],
)
def test_source_binding_rejects_paths_credentials_tokens_and_unknown_source_labels(
    binding_validator: Draft202012Validator,
    source: str,
) -> None:
    with pytest.raises(JsonSchemaValidationError):
        binding_validator.validate(_binding(sources=[source]))


def test_source_binding_descriptions_forbid_private_runtime_values(
    contracts: ContractSet,
) -> None:
    schema = contracts.schema_for_def("ensemble_source_binding")
    segment_description = schema["properties"]["segment_id"]["description"]
    sources_description = schema["properties"]["sources"]["description"]
    binding_description = schema["description"]

    assert "opaque segment SHA-256 label" in segment_description
    assert "Never expose a raw path" in segment_description
    assert "arbitrary caller label" in segment_description
    assert "Closed public source labels only" in sources_description
    assert "raw paths, URIs, credentials and tokens are forbidden" in sources_description
    assert "opaque hashes only" in binding_description


def test_processing_artifact_contract_example_uses_a_closed_source_binding(
    contracts: ContractSet,
) -> None:
    payload = deepcopy(contracts["get_processing_artifact"].output_examples[1])
    assert payload["payload"]["evidence"][0]["source_binding"] == {
        "segment_index": 3,
        "segment_id": "merged-segment-0003",
        "segment_text_sha256": "1" * 64,
        "sources": ["own-system"],
    }
    contracts.validate_output("get_processing_artifact", payload)
