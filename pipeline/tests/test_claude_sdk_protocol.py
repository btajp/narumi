from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from narumi.providers.claude import (
    EXPECTED_CLI_VERSION,
    EXPECTED_SDK_VERSION,
    REQUIRED_CLI_CAPABILITIES,
    runtime_evidence,
    runtime_fingerprint,
)
from narumi.providers.claude.protocol import (
    WorkerRequest,
    WorkerResponse,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
)

CONNECTION = "conn-0123456789abcdef"
MODEL = "claude-fixture-1-20260901"
RUNTIME = {
    "resource_id": "claude-agent-sdk-0-2-144",
    "sdk_version": "0.2.144",
    "cli_version": "2.1.239",
    "cli_sha256": "a" * 64,
    "sdk_source_sha256": "b" * 64,
    "isolation_profile_sha256": "c" * 64,
}
EXECUTION_RUNTIME = {**RUNTIME, "resource_sha256": "d" * 64}


def test_private_protocol_round_trip_and_duplicate_field_rejection():
    request = WorkerRequest(
        CONNECTION, "synthetic-key", MODEL, "Transcript", None, EXECUTION_RUNTIME
    )
    assert decode_request(encode_request(request)) == request
    response = WorkerResponse(
        "Minutes", MODEL, {"input_tokens": 3, "output_tokens": 2}, EXECUTION_RUNTIME
    )
    assert decode_response(encode_response(response)) == response
    with pytest.raises(ValueError):
        decode_response(
            b'{"protocol_version":1,"status":"ok","status":"ok",'
            b'"text":"Minutes","returned_model":"claude-fixture-1-20260901",'
            b'"usage":{"input_tokens":3,"output_tokens":2},'
            b'"runtime_evidence":{}}'
        )


@pytest.mark.parametrize("key", ["", "key with space", "key\nline", "key\x00tail", "\N{SNOWMAN}"])
def test_credentials_must_be_bounded_printable_ascii_without_whitespace(key):
    with pytest.raises(ValueError):
        encode_request(WorkerRequest(CONNECTION, key, MODEL, "Transcript", None))


@pytest.mark.parametrize(
    "usage",
    [
        {},
        {"input_tokens": -1, "output_tokens": 2},
        {"input_tokens": True, "output_tokens": 2},
        {"input_tokens": 1.0, "output_tokens": 2},
        {"input_tokens": 1, "output_tokens": 2, "unknown": 3},
    ],
)
def test_worker_usage_is_closed_and_strict(usage):
    with pytest.raises(ValueError):
        encode_response(WorkerResponse("Minutes", MODEL, usage, RUNTIME))


def test_runtime_evidence_proves_fixed_sdk_cli_and_isolation_capabilities():
    evidence = runtime_evidence()
    assert evidence["sdk_version"] == EXPECTED_SDK_VERSION == "0.2.144"
    assert evidence["cli_version"] == EXPECTED_CLI_VERSION == "2.1.239"
    assert len(evidence["cli_sha256"]) == len(evidence["sdk_source_sha256"]) == 64
    assert len(evidence["isolation_profile_sha256"]) == 64
    assert {
        "--no-session-persistence",
        "--bare",
        "--safe-mode",
        "CLAUDE_CODE_MAX_RETRIES",
    }.issubset(REQUIRED_CLI_CAPABILITIES)
    assert len(runtime_fingerprint(evidence)) == 64
    assert runtime_fingerprint(dict(reversed(list(evidence.items())))) == runtime_fingerprint(
        evidence
    )
    changed = {**evidence, "cli_sha256": "0" * 64}
    assert runtime_fingerprint(changed) != runtime_fingerprint(evidence)
    execution = {**evidence, "resource_sha256": "d" * 64}
    assert runtime_fingerprint(execution) != runtime_fingerprint(evidence)


def test_optional_dependency_is_exactly_pinned():
    project = Path(__file__).parents[1] / "pyproject.toml"
    data = tomllib.loads(project.read_text())
    assert data["project"]["optional-dependencies"]["claude"] == ["claude-agent-sdk==0.2.144"]
