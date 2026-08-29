"""Packet-local reuse and frozen multi-root publication contract semantics."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

import pytest
from narumi.contracts import ContractSet, load_contracts
from narumi.errors import ContractMismatchError


@pytest.fixture(scope="module")
def contracts() -> ContractSet:
    return load_contracts()


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


def test_packet_local_synthesis_leaf_reuse_is_normative(contracts: ContractSet) -> None:
    selection_description = contracts.schema_for_def("minutes_ensemble_selection")["description"]
    assert "partitioned deterministically by the fixed unit and character limits" in (
        selection_description
    )
    assert "empty document contributes one empty sentinel unit" in selection_description
    assert "partition occurrence is one synthesizer chunk-leaf provider call" in (
        selection_description
    )
    assert "packet's source artifact plus each unique draft_chunk artifact" in selection_description
    assert "may therefore appear in multiple leaf occurrences" in selection_description
    assert "fixed synthesis instructions and common brief" in selection_description
    assert "selected model content conditions" in selection_description
    assert "No wire request, hash, fingerprint or current input binding" in selection_description
    assert "same partition occurrences, leaf identities" in selection_description
    assert "downstream aggregation order" in selection_description

    node_description = contracts.schema_for_def("processing_node")["description"]
    assert "stable deterministic partition occurrence for one packet" in node_description
    assert "input bindings are exactly the packet source artifact" in node_description
    assert "dependency_node_ids are exactly the corresponding generator chunk node IDs" in (
        node_description
    )
    assert "source is an artifact input, not a node dependency" in node_description
    assert "fixed synthesis instructions and common brief" in node_description
    assert "request hash and content fingerprint" in node_description
    assert "No request, hash, fingerprint, input binding or dependency" in node_description

    regenerate_description = contracts["regenerate"].description
    assert "synthesis-leaf execution is packet-local" in regenerate_description
    assert "partitioned deterministically by fixed unit and character limits" in (
        regenerate_description
    )
    assert "wire request may also include fixed synthesis instructions/common brief" in (
        regenerate_description
    )
    assert "preserve leaf identity and downstream aggregation order" in regenerate_description


def test_multiroot_publication_proof_is_normative_without_new_wire_fields(
    contracts: ContractSet,
) -> None:
    provenance = contracts.schema_for_def("published_minutes_ensemble_provenance")
    description = provenance["description"]
    assert "generator artifact IDs are the canonical draft roots" in description
    assert "synthesizer artifact ID is the final synthesis root" in description
    assert "together these multiple roots explain publication coverage" in description
    assert "every raw (source_artifact_id, document_artifact_id) pair" in description
    assert "normalizes both direct IDs" in description
    assert "unchanged ID as an identity mapping" in description
    assert "same normalization for each leaf artifact's immutable direct" in description
    assert "content equality alone never permits an unrelated current artifact" in description
    assert "reconstructs the draft_chunk's canonical claim/question unit stream" in description
    assert "exact disjoint union of those normalized partition occurrences" in description
    assert "every nonempty unit and empty sentinel occurs exactly once" in description
    assert "same source/document pair may legitimately bind multiple leaves" in description
    assert "precisely the unique draft_chunk artifacts contributing units" in description
    assert "Every validated nonempty synthesis leaf" in description
    assert "Only a leaf whose closed artifact validation proves an empty document" in description
    assert "same frozen publication proof exactly" in description
    assert "immutable publication record" in description
    assert "never regenerate it from the mutable run public snapshot" in description
    assert "get_published_provenance(run_id, published_version)" in description
    assert "get_minutes must acquire the same publication fence" in description
    assert "first reconcile an exact manifest candidate" in description

    run_description = contracts.schema_for_def("processing_run")["description"]
    assert "frozen multi-root proof" in run_description
    assert "rather than requiring every canonical draft root" in run_description
    assert "exact disjoint union of these stable normalized partition occurrences" in (
        run_description
    )
    assert "same source/document pair may repeat across leaves" in run_description
    assert "frozen direct dependency mapping to obtain the effective current-run IDs" in (
        run_description
    )
    assert "using the unchanged ID as an identity mapping" in run_description
    assert "content equality alone cannot select a different artifact" in run_description
    assert "Preflight is side-effect-free and freezes" in run_description
    assert "exclusive publication fence" in run_description
    assert "publication-sensitive reads cannot interleave" in run_description
    assert "Stop recovery first reconciles an already-written exact manifest" in run_description

    dependency = contracts.schema_for_def("processing_artifact_dependency_mapping")["description"]
    assert "every changed direct dependency" in dependency
    assert "rejects duplicate, missing, stale or extra direct mappings" in dependency
    assert "never stands for a transitive dependency" in dependency
    assert "otherwise the immutable dependency ID is its identity mapping" in dependency
    assert "matching content projection alone never authorizes substitution" in dependency
    binding = contracts.schema_for_def("processing_artifact_binding")
    assert "authorization_snapshot_id and dependency_mappings" in binding["description"]
    assert "with no missing or extra direct edge" in binding["description"]
    assert "recursively follows those exact direct edges" in binding["description"]

    regenerate_description = contracts["regenerate"].description
    assert "normalized through that binding's frozen direct dependency mappings" in (
        regenerate_description
    )
    assert "using identity mapping for unchanged IDs" in regenerate_description
    assert "content equality alone cannot substitute another artifact" in regenerate_description


@pytest.mark.parametrize(
    ("relative_path", "expected_sha256"),
    [
        (
            "defs/minutes_ensemble.json",
            "389e1d376a8e449be02a688517afe29738fa6ed5e487262d67354bdb158c928e",
        ),
        (
            "defs/processing_artifacts.json",
            "c622a703af0a0e166cf9eea395394a6b3b9f298a7d903ba13b5869dc98a03751",
        ),
        (
            "defs/processing_run_records.json",
            "48006511d5e11a69f0595f25a7cafbbb44017a66eaa4a0e8667912840fb1e50d",
        ),
        (
            "tools/regenerate.json",
            "ec2afffe08fdba29210505eb48326e1c3231c739a4ec52aed370e0583adfe832",
        ),
    ],
)
def test_publication_contract_changes_descriptions_only(
    contracts: ContractSet,
    relative_path: str,
    expected_sha256: str,
) -> None:
    def strip_descriptions(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: strip_descriptions(item) for key, item in value.items() if key != "description"
            }
        if isinstance(value, list):
            return [strip_descriptions(item) for item in value]
        return value

    assert contracts.contract_version == "6.0.0"
    assert contracts.path is not None
    document = json.loads((contracts.path / relative_path).read_text())
    canonical = json.dumps(
        strip_descriptions(document),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    assert hashlib.sha256(canonical).hexdigest() == expected_sha256
