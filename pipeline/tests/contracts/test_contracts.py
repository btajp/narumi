"""Contract tests: manifest ↔ files, schema validity, examples, loader inlining, validation."""

from __future__ import annotations

import json
import shutil
import typing
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from narumi.bundle.manifest import MeetingStatus, TrackRecord
from narumi.bundle.session import MEETING_ID_RE
from narumi.config import contracts_dir
from narumi.contracts import ANNOTATION_KEYS, ContractSet, ToolContract, load_contracts
from narumi.errors import (
    ContractMismatchError,
    ErrorCode,
    InvalidArgumentError,
    NotFoundError,
    PolicyViolationError,
)
from narumi.models import ExternalSendPolicy, MeetingConfig

CONTRACTS_DIR = contracts_dir()
TOOL_FILE_KEYS = (
    "name",
    "title",
    "description",
    "annotations",
    "inputSchema",
    "outputSchema",
    "examples",
)
EXPECTED_TOOLS = {
    "get_server_info",
    "start_recording",
    "stop_recording",
    "get_recording_status",
    "import_recording",
    "list_meetings",
    "search_transcripts",
    "get_meeting",
    "get_transcript",
    "get_minutes",
    "register_context",
    "regenerate",
    "set_meeting_config",
    "export_minutes",
    "list_export_destinations",
    "get_job_status",
    "cancel_job",
    "discard_tracks",
    "delete_meeting",
    "list_profiles",
    "get_profile",
    "set_profile",
    "delete_profile",
    "rebuild_catalog",
}
MEETING_ID = "20260827T030500Z-a1b2c3d4"
REQUEST_ID = "6f1c2a1e-9b7d-4c2e-8f0a-1a2b3c4d5e6f"


def _manifest() -> dict[str, Any]:
    return json.loads((CONTRACTS_DIR / "manifest.json").read_text(encoding="utf-8"))


def _common_defs() -> dict[str, Any]:
    doc = json.loads((CONTRACTS_DIR / "defs" / "common.json").read_text(encoding="utf-8"))
    return doc["$defs"]


def _tool_files() -> dict[str, dict[str, Any]]:
    files = sorted((CONTRACTS_DIR / "tools").glob("*.json"))
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in files}


def _iter_refs(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                yield value
            else:
                yield from _iter_refs(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_refs(item)


def _assert_object_schema(schema: dict[str, Any], where: str) -> None:
    assert schema.get("type") == "object", where
    assert schema.get("additionalProperties") is False, f"{where}: additionalProperties"
    assert isinstance(schema.get("required"), list), f"{where}: required"
    assert isinstance(schema.get("properties"), dict), f"{where}: properties"


@pytest.fixture(scope="module")
def contracts() -> ContractSet:
    return load_contracts()


@pytest.fixture
def contracts_copy(tmp_path: Path) -> Path:
    target = tmp_path / "contracts"
    shutil.copytree(CONTRACTS_DIR, target)
    return target


# ----------------------------------------------------------------------------- manifest ↔ files
def test_manifest_lists_exactly_the_v1_tools() -> None:
    manifest = _manifest()
    assert manifest["name"] == "narumi"
    assert manifest["tools"] == sorted(manifest["tools"], key=manifest["tools"].index)
    assert set(manifest["tools"]) == EXPECTED_TOOLS
    assert len(manifest["tools"]) == len(EXPECTED_TOOLS)


def test_manifest_matches_tool_files() -> None:
    manifest = _manifest()
    files = _tool_files()
    assert set(files) == set(manifest["tools"])
    for stem, doc in files.items():
        assert doc["name"] == stem
    for rel in manifest["defs"]:
        assert (CONTRACTS_DIR / rel).is_file()


def test_manifest_version_is_semver() -> None:
    version = _manifest()["contract_version"]
    major, minor, patch = version.split(".")
    assert all(part.isdigit() for part in (major, minor, patch))


# ----------------------------------------------------------------------------- raw file shape
@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_tool_file_has_required_keys_and_annotations(name: str) -> None:
    doc = _tool_files()[name]
    for key in TOOL_FILE_KEYS:
        assert key in doc, f"{name}: missing {key}"
    assert doc["title"].strip() and doc["description"].strip()
    annotations = doc["annotations"]
    assert set(annotations) == set(ANNOTATION_KEYS)
    for key in ANNOTATION_KEYS:
        assert isinstance(annotations[key], bool), f"{name}: {key}"
    assert annotations["openWorldHint"] is False
    if annotations["readOnlyHint"]:
        assert annotations["destructiveHint"] is False
    # every description tells the client that an error envelope may come back
    assert "error_envelope" in doc["description"]
    assert "isError=true" in doc["description"]
    _assert_object_schema(doc["inputSchema"], f"{name}#/inputSchema")
    _assert_object_schema(doc["outputSchema"], f"{name}#/outputSchema")
    assert len(doc["examples"]["input"]) >= 2, f"{name}: need >= 2 input examples"
    assert len(doc["examples"]["output"]) >= 1, f"{name}: need >= 1 output example"


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_raw_schemas_are_valid_2020_12(name: str) -> None:
    doc = _tool_files()[name]
    Draft202012Validator.check_schema(doc["inputSchema"])
    Draft202012Validator.check_schema(doc["outputSchema"])


def test_common_defs_are_valid_2020_12() -> None:
    doc = json.loads((CONTRACTS_DIR / "defs" / "common.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(doc)
    for schema in doc["$defs"].values():
        Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_raw_refs_use_the_shared_defs_convention(name: str) -> None:
    doc = _tool_files()[name]
    defs = _common_defs()
    prefix = "../defs/common.json#/$defs/"
    refs = list(_iter_refs(doc["inputSchema"])) + list(_iter_refs(doc["outputSchema"]))
    for ref in refs:
        assert ref.startswith(prefix), f"{name}: unexpected $ref {ref}"
        assert ref[len(prefix) :] in defs, f"{name}: unresolved $ref {ref}"


# ----------------------------------------------------------------------------- loader
def test_loader_exposes_manifest_metadata(contracts: ContractSet) -> None:
    manifest = _manifest()
    assert contracts.name == manifest["name"]
    assert contracts.contract_version == manifest["contract_version"]
    assert contracts.tool_names() == manifest["tools"]
    assert contracts.path == CONTRACTS_DIR
    assert set(contracts.defs) == set(_common_defs())
    assert len(contracts) == len(EXPECTED_TOOLS)
    assert "get_meeting" in contracts
    assert isinstance(contracts["get_meeting"], ToolContract)
    assert contracts.get("nope") is None


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_loader_inlines_external_refs(contracts: ContractSet, name: str) -> None:
    tool = contracts[name]
    assert tool.output_schema is not None
    for schema in (tool.input_schema, tool.output_schema):
        defs = schema.get("$defs", {})
        for ref in _iter_refs(schema):
            assert ref.startswith("#/$defs/"), f"{name}: external $ref survived: {ref}"
            assert ref[len("#/$defs/") :] in defs, f"{name}: dangling {ref}"
        Draft202012Validator.check_schema(schema)
        _assert_object_schema(schema, name)
    # the raw file and the loaded contract agree on the top-level keys
    raw = _tool_files()[name]
    assert set(tool.input_schema["properties"]) == set(raw["inputSchema"]["properties"])
    assert set(tool.output_schema["properties"]) == set(raw["outputSchema"]["properties"])


def test_inlined_schemas_do_not_share_mutable_state(contracts: ContractSet) -> None:
    a = contracts["get_meeting"].input_schema["$defs"]["meeting_id"]
    b = contracts["get_transcript"].input_schema["$defs"]["meeting_id"]
    assert a == b and a is not b


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_examples_validate(contracts: ContractSet, name: str) -> None:
    tool = contracts[name]
    assert len(tool.input_examples) >= 2
    assert len(tool.output_examples) >= 1
    for example in tool.input_examples:
        contracts.validate_input(name, example)
    for example in tool.output_examples:
        contracts.validate_output(name, example)


def test_check_examples_passes(contracts: ContractSet) -> None:
    contracts.check_examples()


def test_tool_definition_has_mcp_shape(contracts: ContractSet) -> None:
    definition = contracts["regenerate"].tool_definition()
    assert set(definition) == {
        "name",
        "title",
        "description",
        "inputSchema",
        "outputSchema",
        "annotations",
    }
    assert definition["name"] == "regenerate"
    assert definition["annotations"] == contracts["regenerate"].annotations
    definition["inputSchema"]["properties"].clear()
    assert contracts["regenerate"].input_schema["properties"], "must be a copy"


# ----------------------------------------------------------------------------- defs ↔ python
def test_error_code_enum_matches_errors_module(contracts: ContractSet) -> None:
    # cancel_job introduced the ``cancelled`` code; ``narumi.errors.ErrorCode`` must carry it too.
    assert "cancelled" in contracts.defs["error_code"]["enum"]
    assert set(contracts.defs["error_code"]["enum"]) == {e.value for e in ErrorCode}


def test_shared_defs_match_internal_models(contracts: ContractSet) -> None:
    defs = contracts.defs
    assert defs["meeting_id"]["pattern"] == MEETING_ID_RE.pattern
    assert set(defs["meeting_config"]["properties"]) == set(MeetingConfig.model_fields)
    assert set(defs["external_send_policy"]["enum"]) == {p.value for p in ExternalSendPolicy}
    assert set(defs["track_status"]["properties"]) == set(TrackRecord.model_fields)
    assert set(defs["meeting_summary"]["properties"]["status"]["enum"]) == set(
        typing.get_args(MeetingStatus)
    )


def test_error_envelope_schema_accepts_narumi_error_payloads(contracts: ContractSet) -> None:
    schema = contracts.error_envelope_schema()
    for ref in _iter_refs(schema):
        assert ref.startswith("#/$defs/")
    Draft202012Validator.check_schema(schema)
    contracts.validate_error_envelope(
        NotFoundError("missing", details={"meeting_id": "x"}).to_payload()
    )
    contracts.validate_error_envelope(PolicyViolationError("nope").to_payload())
    with pytest.raises(ContractMismatchError):
        contracts.validate_error_envelope({"error": {"code": "boom", "message": "x"}})
    with pytest.raises(ContractMismatchError):
        contracts.validate_error_envelope({"error": {"code": "internal"}})


def test_schema_for_def_is_self_contained(contracts: ContractSet) -> None:
    schema = contracts.schema_for_def("job")
    assert set(schema["$defs"]) >= {"job_id", "meeting_id", "job_kind", "job_status", "error"}
    Draft202012Validator(schema).validate(
        {
            "job_id": "job-0123456789ab",
            "kind": "process",
            "status": "queued",
            "created_at": "2026-08-27T03:05:00Z",
            "updated_at": "2026-08-27T03:05:00Z",
        }
    )
    with pytest.raises(ContractMismatchError):
        contracts.schema_for_def("does_not_exist")


# ----------------------------------------------------------------------------- validate_input
def test_validate_input_rejects_bad_payload(contracts: ContractSet) -> None:
    with pytest.raises(InvalidArgumentError) as info:
        contracts.validate_input("get_meeting", {"meeting_id": "not-an-id", "bogus": 1})
    err = info.value
    assert err.code == ErrorCode.INVALID_ARGUMENT
    assert err.details["tool"] == "get_meeting"
    errors = err.details["errors"]
    assert isinstance(errors, list) and errors
    paths = {item["path"] for item in errors}
    assert "$.meeting_id" in paths
    assert "$" in paths  # additionalProperties violation is reported at the root
    for item in errors:
        assert set(item) == {"path", "message", "validator"}
    payload = err.to_payload()
    contracts.validate_error_envelope(payload)


def test_validate_input_missing_required(contracts: ContractSet) -> None:
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("regenerate", {"meeting_id": MEETING_ID})
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("get_job_status", {})


def test_validate_input_unknown_tool(contracts: ContractSet) -> None:
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("does_not_exist", {})
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("does_not_exist", {})


def test_validate_input_accepts_none_for_no_arg_tools(contracts: ContractSet) -> None:
    contracts.validate_input("get_server_info", None)
    contracts.validate_input("list_export_destinations", {})
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("get_job_status", None)


def test_validate_input_enforces_date_time_format(contracts: ContractSet) -> None:
    contracts.validate_input("list_meetings", {"range": {"from": "2026-08-27T03:05:00Z"}})
    contracts.validate_input("list_meetings", {"range": {"from": "2026-08-27T03:05:00+09:00"}})
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("list_meetings", {"range": {"from": "yesterday"}})
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("list_meetings", {"range": {"from": "2026-08-27"}})
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("list_meetings", {"range": {"to": "2026-13-01T00:00:00Z"}})


def test_validate_input_scope_forms(contracts: ContractSet) -> None:
    contracts.validate_input("list_meetings", {})
    contracts.validate_input("list_meetings", {"scope": "cloudnative"})
    contracts.validate_input("list_meetings", {"scope": ["cloudnative", "btcon"]})
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("list_meetings", {"scope": []})
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("list_meetings", {"scope": ["a", "a"]})
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("list_meetings", {"limit": 0})
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("list_meetings", {"limit": 501})


def test_register_context_requires_exactly_one_payload(contracts: ContractSet) -> None:
    base = {"meeting_id": MEETING_ID, "source_type": "text", "request_id": REQUEST_ID}
    contracts.validate_input("register_context", {**base, "content": "hello"})
    contracts.validate_input("register_context", {**base, "url": "https://example.com/x"})
    contracts.validate_input("register_context", {**base, "file_path": "/tmp/x.md"})
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("register_context", base)
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input(
            "register_context", {**base, "content": "a", "url": "https://example.com/x"}
        )
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("register_context", {**base, "url": "ftp://example.com/x"})


def test_get_transcript_source_pattern(contracts: ContractSet) -> None:
    for source in ("merged", "own-mic", "own-system", "ext-ctx-0123abcd"):
        contracts.validate_input("get_transcript", {"meeting_id": MEETING_ID, "source": source})
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("get_transcript", {"meeting_id": MEETING_ID, "source": "mic"})


def test_set_meeting_config_nullable_fields(contracts: ContractSet) -> None:
    base = {"meeting_id": MEETING_ID, "request_id": REQUEST_ID}
    contracts.validate_input("set_meeting_config", {**base, "self_name": None, "new_scope": None})
    contracts.validate_input("set_meeting_config", {**base, "external_send_policy": "api_ok"})
    # ``scope`` is the read selector (string | string[]), ``new_scope`` the value to store
    contracts.validate_input("set_meeting_config", {**base, "scope": ["a", "b"], "new_scope": "a"})
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("set_meeting_config", {**base, "external_send_policy": "yolo"})
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("set_meeting_config", {**base, "new_scope": ["a", "b"]})
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("set_meeting_config", {**base, "scope": None})


def test_import_recording_requires_mic_or_system(contracts: ContractSet) -> None:
    base = {"meeting_name": "取り込み", "request_id": REQUEST_ID}
    contracts.validate_input("import_recording", {**base, "mic_path": "/tmp/mic.m4a"})
    contracts.validate_input("import_recording", {**base, "system_path": "/tmp/system.m4a"})
    contracts.validate_input(
        "import_recording",
        {**base, "mic_path": "/tmp/mic.m4a", "system_path": "/tmp/system.m4a"},
    )
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("import_recording", base)
    with pytest.raises(InvalidArgumentError):  # screen alone is not enough
        contracts.validate_input("import_recording", {**base, "screen_path": "/tmp/screen.mp4"})
    with pytest.raises(InvalidArgumentError):  # paths must be absolute
        contracts.validate_input("import_recording", {**base, "mic_path": "mic.m4a"})


def test_discard_tracks_track_list(contracts: ContractSet) -> None:
    base = {"meeting_id": MEETING_ID, "request_id": REQUEST_ID}
    contracts.validate_input("discard_tracks", {**base, "tracks": ["screen"]})
    contracts.validate_input("discard_tracks", {**base, "tracks": ["screen", "mic", "system"]})
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("discard_tracks", {**base, "tracks": []})
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("discard_tracks", {**base, "tracks": ["mic", "mic"]})
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("discard_tracks", {**base, "tracks": ["video"]})


def test_delete_meeting_requires_literal_confirm(contracts: ContractSet) -> None:
    base = {"meeting_id": MEETING_ID, "request_id": REQUEST_ID}
    contracts.validate_input("delete_meeting", {**base, "confirm": True})
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("delete_meeting", base)
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("delete_meeting", {**base, "confirm": False})


def test_destructive_tools_are_flagged(contracts: ContractSet) -> None:
    assert contracts["discard_tracks"].annotations["destructiveHint"] is True
    assert contracts["delete_meeting"].annotations["destructiveHint"] is True


def test_search_transcripts_query_and_limit(contracts: ContractSet) -> None:
    contracts.validate_input("search_transcripts", {"query": "議事録"})
    contracts.validate_input(
        "search_transcripts", {"query": "x", "scope": ["a", "b"], "limit": 200}
    )
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("search_transcripts", {"query": ""})
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("search_transcripts", {"query": "x", "limit": 0})
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("search_transcripts", {"query": "x", "limit": 201})


def test_get_minutes_version_bounds(contracts: ContractSet) -> None:
    contracts.validate_input("get_minutes", {"meeting_id": MEETING_ID})
    contracts.validate_input("get_minutes", {"meeting_id": MEETING_ID, "version": 3})
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("get_minutes", {"meeting_id": MEETING_ID, "version": 0})


def test_set_profile_nullable_defaults(contracts: ContractSet) -> None:
    base = {"name": "customer-meetings", "request_id": REQUEST_ID}
    contracts.validate_input("set_profile", {**base, "scope": None, "engagement": None})
    contracts.validate_input(
        "set_profile",
        {**base, "scope": "cloudnative", "export_destinations": ["markdown"], "make_default": True},
    )
    with pytest.raises(InvalidArgumentError):  # profile scope is a single value, not a selector
        contracts.validate_input("set_profile", {**base, "scope": ["a", "b"]})
    with pytest.raises(InvalidArgumentError):
        contracts.validate_input("set_profile", {**base, "export_destinations": ["md", "md"]})


def test_profile_def_and_meeting_summary_active_job(contracts: ContractSet) -> None:
    assert set(contracts.defs["profile"]["required"]) == {
        "name",
        "config",
        "scope",
        "engagement",
        "export_destinations",
        "is_default",
    }
    summary_props = contracts.defs["meeting_summary"]
    assert "active_job" in summary_props["properties"]
    assert "active_job" not in summary_props["required"]
    # examples exercise active_job as both null and an object
    meetings = contracts["list_meetings"].output_examples[0]["meetings"]
    assert any(m.get("active_job") is None for m in meetings)
    assert any(isinstance(m.get("active_job"), dict) for m in meetings)


# ----------------------------------------------------------------------------- validate_output
def test_validate_output_rejects_contract_violation(contracts: ContractSet) -> None:
    with pytest.raises(ContractMismatchError) as info:
        contracts.validate_output("get_job_status", {"job": {"job_id": "job-0123456789ab"}})
    assert info.value.code == ErrorCode.CONTRACT_MISMATCH
    assert info.value.details["tool"] == "get_job_status"
    assert info.value.details["errors"]


def test_export_minutes_output_is_exactly_one_of_result_or_job(contracts: ContractSet) -> None:
    result = {
        "destination": "markdown",
        "ref": "/tmp/out.md",
        "minutes_version": 1,
        "at": "2026-08-27T04:15:00Z",
    }
    contracts.validate_output("export_minutes", {"result": result})
    contracts.validate_output("export_minutes", {"job_id": "job-0123456789ab"})
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("export_minutes", {})
    with pytest.raises(ContractMismatchError):
        contracts.validate_output(
            "export_minutes", {"result": result, "job_id": "job-0123456789ab"}
        )


def test_get_meeting_output_artifact_keys_follow_convention(contracts: ContractSet) -> None:
    example = contracts["get_meeting"].output_examples[0]
    good = {**example, "artifacts": ["preprocess/audio/mic", "minutes/v1"]}
    contracts.validate_output("get_meeting", good)
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_meeting", {**example, "artifacts": ["no-slash"]})
    with pytest.raises(ContractMismatchError):
        contracts.validate_output("get_meeting", {**example, "artifacts": ["a/b", "a/b"]})


# ----------------------------------------------------------------------------- read vs write
def test_read_only_tools_have_no_request_id_and_write_tools_require_it(
    contracts: ContractSet,
) -> None:
    read_only = {name for name, tool in contracts.tools.items() if tool.read_only}
    write = set(contracts.tools) - read_only
    assert read_only == {
        "get_server_info",
        "get_recording_status",
        "list_meetings",
        "search_transcripts",
        "get_meeting",
        "get_transcript",
        "get_minutes",
        "list_export_destinations",
        "get_job_status",
        "list_profiles",
        "get_profile",
    }
    for name in read_only:
        assert "request_id" not in contracts[name].input_schema["properties"], name
        assert contracts[name].annotations["idempotentHint"] is True, name
    for name in write:
        schema = contracts[name].input_schema
        assert "request_id" in schema["properties"], name
        assert "request_id" in schema["required"], name
        assert contracts[name].annotations["idempotentHint"] is True, name
        assert contracts[name].annotations["readOnlyHint"] is False, name


# ----------------------------------------------------------------------------- loader errors
def test_missing_tool_file_raises(contracts_copy: Path) -> None:
    (contracts_copy / "tools" / "regenerate.json").unlink()
    with pytest.raises(ContractMismatchError) as info:
        load_contracts(contracts_copy)
    assert info.value.details["missing_files"] == ["regenerate"]


def test_unlisted_tool_file_raises(contracts_copy: Path) -> None:
    src = contracts_copy / "tools" / "regenerate.json"
    extra = json.loads(src.read_text(encoding="utf-8"))
    extra["name"] = "extra_tool"
    (contracts_copy / "tools" / "extra_tool.json").write_text(json.dumps(extra), encoding="utf-8")
    with pytest.raises(ContractMismatchError) as info:
        load_contracts(contracts_copy)
    assert info.value.details["unlisted_files"] == ["extra_tool"]


def test_tool_name_mismatch_raises(contracts_copy: Path) -> None:
    path = contracts_copy / "tools" / "get_job_status.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["name"] = "job_status"
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ContractMismatchError, match="does not match the file name"):
        load_contracts(contracts_copy)


def test_unresolved_ref_raises(contracts_copy: Path) -> None:
    path = contracts_copy / "tools" / "get_job_status.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["inputSchema"]["properties"]["job_id"] = {"$ref": "../defs/common.json#/$defs/nope"}
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ContractMismatchError, match="unresolved"):
        load_contracts(contracts_copy)


def test_ref_to_unknown_file_raises(contracts_copy: Path) -> None:
    path = contracts_copy / "tools" / "get_job_status.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["inputSchema"]["properties"]["job_id"] = {"$ref": "../defs/other.json#/$defs/job_id"}
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ContractMismatchError, match="manifest.defs"):
        load_contracts(contracts_copy)


def test_invalid_schema_raises(contracts_copy: Path) -> None:
    path = contracts_copy / "tools" / "get_job_status.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["outputSchema"]["properties"]["job"] = {"type": "objekt"}
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ContractMismatchError, match="invalid JSON Schema"):
        load_contracts(contracts_copy)


def test_invalid_json_raises(contracts_copy: Path) -> None:
    (contracts_copy / "tools" / "regenerate.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ContractMismatchError, match="invalid JSON"):
        load_contracts(contracts_copy)


def test_missing_contracts_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(ContractMismatchError):
        load_contracts(tmp_path / "nowhere")


def test_default_dir_honours_env_override(
    monkeypatch: pytest.MonkeyPatch, contracts_copy: Path
) -> None:
    monkeypatch.setenv("NARUMI_CONTRACTS_DIR", str(contracts_copy))
    assert load_contracts().path == contracts_copy
