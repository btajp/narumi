"""Server-side validation and reference protection for ensemble model selections."""

from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

import pytest
from narumi.models import MeetingConfig
from narumi.providers.generation import MinutesResolver
from narumi_server.app import dispatch
from test_provider_tools import codex_context as provider_codex_context
from test_provider_tools import result, selected_bundle

from pipeline.tests.provider_fakes import prepared_codex_connection


@pytest.fixture(name="codex_context")
def _codex_context(request):
    _ = provider_codex_context
    return request.getfixturevalue("provider_codex_context")


def ensemble_config(base: MeetingConfig, selections: list[dict] | None = None) -> MeetingConfig:
    selected = selections or [base.minutes_model.model_dump(mode="json")] * 3
    payload = base.model_dump(mode="json")
    payload.update(
        minutes_model=None,
        minutes_ensemble={
            "generators": [
                {"id": f"gen-{index:032x}", "label": f"案 {index}", "selection": item}
                for index, item in enumerate(selected[:2], start=1)
            ],
            "synthesizer": selected[2],
        },
    )
    return MeetingConfig.model_validate(payload)


@pytest.mark.parametrize("member", [0, 1, 2])
def test_every_ensemble_selection_is_validated_before_profile_save(codex_context, member):
    ctx, backend, base = codex_context
    selections = [deepcopy(base.minutes_model.model_dump(mode="json")) for _ in range(3)]
    selections[member]["connection_revision"] += 1
    rejected = dispatch(
        ctx,
        "set_profile",
        {
            "name": "ensemble",
            "config": ensemble_config(base, selections).model_dump(mode="json"),
            "request_id": str(uuid4()),
        },
    )
    assert rejected.is_error and rejected.payload["error"]["code"] == "configuration_conflict"
    assert ctx.profiles.peek("ensemble") is None and backend.calls == []


@pytest.mark.parametrize("surface", ["profile", "unindexed_meeting"])
@pytest.mark.parametrize("member", [0, 1, 2])
def test_ensemble_connections_cannot_be_deleted_while_files_reference_them(
    codex_context, surface, member
):
    ctx, _, base = codex_context
    records = [
        prepared_codex_connection(ctx.providers, request_id=f"ensemble-reference-{index}")
        for index in (1, 2)
    ]
    records.insert(0, {"connection_id": base.minutes_model.connection_id, "revision": 1})
    selections = [
        {
            **base.minutes_model.model_dump(mode="json"),
            "connection_id": record["connection_id"],
            "connection_revision": record["revision"],
        }
        for record in records
    ]
    config = ensemble_config(base, selections)
    if surface == "profile":
        result(
            ctx,
            "set_profile",
            {
                "name": "ensemble",
                "config": config.model_dump(mode="json"),
                "request_id": str(uuid4()),
            },
        )
    else:
        bundle = selected_bundle(ctx, config)
        ctx.catalog.delete_meeting(bundle.meeting_id)
    target = selections[member]
    args = {
        "connection_id": target["connection_id"],
        "expected_revision": target["connection_revision"],
        "confirm": True,
        "request_id": str(uuid4()),
    }
    rejected = dispatch(ctx, "delete_provider_connection", args)
    assert rejected.is_error and rejected.payload["error"]["code"] == "busy"
    if surface == "profile":
        result(
            ctx,
            "set_profile",
            {
                "name": "ensemble",
                "config": {"minutes_ensemble": None},
                "request_id": str(uuid4()),
            },
        )
    else:
        result(
            ctx,
            "set_meeting_config",
            {
                "meeting_id": bundle.meeting_id,
                "minutes_ensemble": None,
                "request_id": str(uuid4()),
            },
        )
    result(ctx, "delete_provider_connection", {**args, "request_id": str(uuid4())})


def test_ensemble_selection_uses_one_provider_snapshot(codex_context, monkeypatch):
    ctx, backend, base = codex_context
    entries = []
    original = MinutesResolver.validate_selection_in_transaction

    def observe(self, selection, policy, document):
        entries.append(id(document))
        return original(self, selection, policy, document)

    monkeypatch.setattr(MinutesResolver, "validate_selection_in_transaction", observe)
    saved = result(
        ctx,
        "set_profile",
        {
            "name": "ensemble",
            "config": ensemble_config(base).model_dump(mode="json"),
            "request_id": str(uuid4()),
        },
    )["profile"]
    assert saved["config"]["minutes_ensemble"] is not None
    assert len(entries) == 3 and len(set(entries)) == 1 and backend.calls == []
