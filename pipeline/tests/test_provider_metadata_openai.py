"""OpenAI discovery and capability gates use only fake HTTP and fixture keys."""

from __future__ import annotations

import builtins
import json
import traceback
from datetime import timedelta, timezone

import pytest
from jsonschema import Draft202012Validator
from narumi.errors import (
    AuthenticationRequiredError,
    EngineUnavailableError,
    InvalidArgumentError,
    ModelUnavailableError,
)
from narumi.providers.metadata import MetadataClient
from narumi.providers.metadata.openai_capabilities import (
    confirmed_resolved_model_ids,
    model_capabilities,
    reasoning_payload,
)

from pipeline.tests.test_provider_metadata import KEY, NOW, FakeHTTP, client, validate_models

API = "https://api.openai.com"


def openai_model(model_id="gpt-5.4", **changes):
    return {
        "id": model_id,
        "object": "model",
        "created": 1_700_000_000,
        "owned_by": "openai",
        **changes,
    }


def page(*models, **changes):
    return {"object": "list", "data": list(models), **changes}


def test_openai_uses_explicit_bearer_and_returns_text_only_verified_capabilities():
    metadata, http = client(page(openai_model()))
    models = metadata.fetch("openai-api", API, KEY)
    validate_models(models)
    model = models[0]
    assert model["model_id"] == "gpt-5.4"
    assert model["availability"] == "available" and model["reason"] is None
    assert model["input_modalities"] == model["output_modalities"] == ["text"]
    assert model["context_window"] == 1_050_000
    assert model["max_output_tokens"] == 128_000
    assert model["parameter_schema"]["properties"] == {
        "max_tokens": {"type": "integer", "minimum": 1, "maximum": 32768, "default": 4096},
        "reasoning_effort": {
            "type": "string",
            "enum": ["none", "low", "medium", "high", "xhigh"],
            "default": "none",
        },
    }
    assert model["resolved_revision"] is None
    assert model["source"] == "provider_api" and model["billing"]["kind"] == "api"
    assert all(value is None for key, value in model["billing"].items() if key != "kind")
    assert http.calls == [
        {
            "method": "GET",
            "url": API + "/v1/models",
            "headers": {"Authorization": "Bearer " + KEY},
            "payload": None,
            "timeout": 10.0,
        }
    ]


@pytest.mark.parametrize("tier", ["sol", "terra", "luna"])
def test_gpt_56_has_only_its_verified_reasoning_options(tier):
    model_id = "gpt-5.6-" + tier
    metadata, _ = client(page(openai_model(model_id)))
    models = metadata.fetch("openai-api", API, KEY)
    validate_models(models)
    schema = models[0]["parameter_schema"]
    assert schema["properties"]["reasoning_effort"]["default"] == "medium"
    validator = Draft202012Validator(schema)
    for effort in ("none", "low", "medium", "high", "xhigh", "max"):
        validator.validate({"reasoning_effort": effort})
        assert reasoning_payload(model_id, effort) == {
            "effort": effort,
            "mode": "standard",
            "context": "current_turn",
        }
    assert not validator.is_valid({"reasoning_effort": "ultra"})
    assert not validator.is_valid({"reasoning_mode": "pro"})
    assert reasoning_payload(model_id, None) == {
        "effort": "medium",
        "mode": "standard",
        "context": "current_turn",
    }
    assert confirmed_resolved_model_ids(model_id) == {model_id}


@pytest.mark.parametrize("model_id", ["gpt-4.1", "gpt-4.1-mini"])
def test_nonreasoning_models_do_not_advertise_or_send_reasoning(model_id):
    metadata, _ = client(page(openai_model(model_id)))
    models = metadata.fetch("openai-api", API, KEY)
    validate_models(models)
    model = models[0]
    assert model["availability"] == "available"
    assert model["context_window"] == 1_047_576
    assert model["max_output_tokens"] == 32_768
    assert set(model["parameter_schema"]["properties"]) == {"max_tokens"}
    assert reasoning_payload(model_id, None) is None
    for effort in ("none", "low", "medium", "high"):
        with pytest.raises(InvalidArgumentError):
            reasoning_payload(model_id, effort)


@pytest.mark.parametrize(
    ("alias", "snapshot"),
    [
        ("gpt-5.4", "gpt-5.4-2026-03-05"),
        ("gpt-4.1", "gpt-4.1-2025-04-14"),
        ("gpt-4.1-mini", "gpt-4.1-mini-2025-04-14"),
    ],
)
def test_alias_responses_allow_only_the_exact_verified_snapshot(alias, snapshot):
    metadata, _ = client(page(openai_model(snapshot)))
    models = metadata.fetch("openai-api", API, KEY)
    validate_models(models)
    assert len(models) == 1 and models[0]["model_id"] == snapshot
    assert models[0]["availability"] == "available"
    assert models[0]["resolved_revision"] == snapshot
    assert confirmed_resolved_model_ids(alias) == {alias, snapshot}
    assert confirmed_resolved_model_ids(snapshot) == {snapshot}


@pytest.mark.parametrize(
    "model_id",
    [
        "gpt-5.6",
        "gpt-5.6-sol-2099-01-01",
        "gpt-5.4-2099-01-01",
        "gpt-4.1-mini-2099-01-01",
        "ft:gpt-4.1:fixture:custom:id",
        "gpt-4.1-nano",
        "text-embedding-fixture",
        "gpt-unknown",
    ],
)
def test_unknown_fine_tuned_and_unconfirmed_snapshot_capabilities_are_not_guessed(model_id):
    metadata, _ = client(
        page(
            openai_model(
                model_id,
                max_input_tokens=1_000_000,
                max_tokens=65536,
                capabilities={"text": True, "reasoning": True},
                display_name="Untrusted label",
            )
        )
    )
    models = metadata.fetch("openai-api", API, KEY)
    validate_models(models)
    assert len(models) == 1
    model = models[0]
    assert model["model_id"] == model_id and model["display_name"] == model_id
    assert model["availability"] == "unverified"
    assert model["reason"] == "model_capabilities_unavailable"
    assert model["input_modalities"] == model["output_modalities"] == model["roles"] == []
    assert model["context_window"] is model["max_output_tokens"] is None
    assert model["resolved_revision"] is None
    assert model["parameter_schema"]["properties"] == {}
    assert model_capabilities(model_id) is None
    assert confirmed_resolved_model_ids(model_id) == frozenset()


def test_models_not_returned_by_api_are_never_added_from_capability_table():
    metadata, _ = client(page(openai_model("gpt-4.1-mini")))
    models = metadata.fetch("openai-api", API, KEY)
    assert [model["model_id"] for model in models] == ["gpt-4.1-mini"]
    metadata, _ = client(page())
    assert metadata.fetch("openai-api", API, KEY) == []


def test_unknown_model_cannot_produce_a_reasoning_payload():
    with pytest.raises(ModelUnavailableError):
        reasoning_payload("gpt-5.4-2099-01-01", "high")


def test_default_effort_and_closed_schema_do_not_change_between_model_families():
    assert reasoning_payload("gpt-5.4", None) == {"effort": "none"}
    assert reasoning_payload("gpt-5.4-2026-03-05", "high") == {"effort": "high"}
    with pytest.raises(InvalidArgumentError):
        reasoning_payload("gpt-5.4", "max")
    metadata, _ = client(page(openai_model()))
    schema = metadata.fetch("openai-api", API, KEY)[0]["parameter_schema"]
    validator = Draft202012Validator(schema)
    validator.validate({})
    validator.validate({"reasoning_effort": "high", "max_tokens": 32768})
    for parameters in (
        {"max_tokens": 32769},
        {"max_tokens": True},
        {"max_tokens": 0},
        {"temperature": 0},
        {"reasoning_mode": "pro"},
        {"tools": []},
        {"api_key": KEY},
        {"model": "gpt-4.1"},
    ):
        assert not validator.is_valid(parameters)


def test_openai_metadata_does_not_use_sdk_or_ambient_credentials(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://fixture-unapproved.invalid")
    monkeypatch.setenv("OPENAI_ORG_ID", "fixture-ambient-organization")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "fixture-ambient-project")
    original_import = builtins.__import__

    def no_sdk(name, *args, **kwargs):
        assert name != "openai" and not name.startswith("openai."), "SDK was imported"
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_sdk)
    metadata, http = client(page(openai_model()))
    with pytest.raises(AuthenticationRequiredError):
        metadata.fetch("openai-api", API, None)
    assert http.calls == []
    metadata.fetch("openai-api", API, KEY)
    assert http.calls[0]["url"] == API + "/v1/models"
    assert http.calls[0]["headers"] == {"Authorization": "Bearer " + KEY}


@pytest.mark.parametrize(
    "key", [True, 123, "contains space", "bad\r\nheader", "非ascii", "x" * 4097]
)
def test_invalid_credentials_are_rejected_before_transport(key):
    metadata, http = client()
    with pytest.raises(InvalidArgumentError):
        metadata.fetch("openai-api", API, key)
    assert http.calls == []


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://api.openai.com",
        "https://api.openai.com/v1",
        "https://api.openai.com:8443",
        "https://api.openai.com.attacker.invalid",
        "https://" + KEY + "@api.openai.com",
        "https://api.openai.com?key=" + KEY,
        "https://api.anthropic.com",
    ],
)
def test_openai_key_is_never_sent_to_an_unapproved_endpoint(endpoint):
    metadata, http = client()
    with pytest.raises(InvalidArgumentError) as failure:
        metadata.fetch("openai-api", endpoint, KEY)
    assert KEY not in str(failure.value.to_payload())
    assert http.calls == []


@pytest.mark.parametrize(
    "body",
    [
        page(openai_model(), unused=KEY),
        page(openai_model(), unused={"nested": "prefix " + KEY + " suffix"}),
        page(openai_model(), unused={KEY: "value"}),
        page(openai_model(), unused="Bearer " + KEY),
        page(openai_model(id=KEY)),
        page(openai_model(owned_by="Bearer fixture-unrelated-secret")),
    ],
)
def test_raw_key_and_bearer_reflections_cannot_enter_public_results(body, caplog):
    metadata, _ = client(body)
    with pytest.raises(EngineUnavailableError) as failure:
        metadata.fetch("openai-api", API, KEY)
    assert KEY not in json.dumps(failure.value.to_payload())
    assert KEY not in "".join(traceback.format_exception(failure.value))
    assert KEY not in caplog.text
    assert failure.value.details["reason"] == "unsafe_metadata"


@pytest.mark.parametrize(
    "body",
    [
        [],
        {},
        {"object": "list", "data": "not-a-list"},
        page(openai_model(), object="model"),
        page(openai_model(), openai_model()),
        page(openai_model(object="not-model")),
        page(openai_model(id="bad\nidentifier")),
        page(openai_model(created=True)),
        page(openai_model(created="1700000000")),
        page(openai_model(created=-1)),
        page(openai_model(created=None)),
        page(openai_model(owned_by=None)),
        page(openai_model(owned_by=" leading space")),
        page("model"),
    ],
)
def test_malformed_openai_catalogs_fail_closed(body):
    metadata, http = client(body)
    with pytest.raises(EngineUnavailableError):
        metadata.fetch("openai-api", API, KEY)
    assert len(http.calls) == 1


def test_oversized_openai_catalog_is_rejected_without_follow_up_requests():
    metadata, http = client(page(*(openai_model(f"fixture-{i}") for i in range(201))))
    with pytest.raises(EngineUnavailableError) as failure:
        metadata.fetch("openai-api", API, KEY)
    assert failure.value.details["reason"] == "metadata_catalog_limit"
    assert len(http.calls) == 1


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_shutdown_date_is_preserved_and_the_utc_date_gates_availability(delta):
    expires_on = (NOW.date() + timedelta(days=delta)).isoformat()
    metadata, _ = client(page(openai_model(shutdown_date=expires_on)))
    models = metadata.fetch("openai-api", API, KEY)
    validate_models(models)
    assert models[0]["availability_expires_on"] == expires_on
    assert models[0]["availability"] == ("available" if delta > 0 else "retired")
    assert models[0]["reason"] == (None if delta > 0 else "model_retired")


def test_shutdown_comparison_uses_utc_even_if_the_injected_clock_uses_another_timezone():
    expires_on = NOW.date().isoformat()
    http = FakeHTTP([page(openai_model(shutdown_date=expires_on))])
    clock = NOW.astimezone(timezone(timedelta(hours=-12)))
    assert clock.date() < NOW.date()
    metadata = MetadataClient(http=http, now=lambda: clock)
    model = metadata.fetch("openai-api", API, KEY)[0]
    assert model["availability"] == "retired"
    assert model["availability_expires_on"] == expires_on


@pytest.mark.parametrize("changes", [{}, {"shutdown_date": None}])
def test_absent_shutdown_date_does_not_invent_an_expiry(changes):
    metadata, _ = client(page(openai_model(**changes)))
    models = metadata.fetch("openai-api", API, KEY)
    validate_models(models)
    assert models[0]["availability"] == "available"
    assert models[0]["availability_expires_on"] is None


@pytest.mark.parametrize("expires_on", ["2026-08-27", "2026-08-29"])
def test_unknown_model_keeps_confirmed_shutdown_date_without_inventing_capabilities(expires_on):
    metadata, _ = client(page(openai_model("unknown-model", shutdown_date=expires_on)))
    models = metadata.fetch("openai-api", API, KEY)
    validate_models(models)
    assert models[0]["availability_expires_on"] == expires_on
    assert models[0]["availability"] != "available"
    assert models[0]["max_output_tokens"] is models[0]["context_window"] is None


@pytest.mark.parametrize(
    "expires_on",
    [
        0,
        1_900_000_000,
        True,
        [],
        {},
        "",
        "2026-02-29",
        "0000-01-01",
        "2026-13-01",
        "2026-8-29",
        "20260829",
        "2026-W35-6",
        "2026-08-29T00:00:00Z",
        "2026-08-29 ",
        " 2026-08-29",
        "２０２６-０８-２９",
    ],
)
def test_invalid_shutdown_dates_fail_closed(expires_on):
    metadata, _ = client(page(openai_model(shutdown_date=expires_on)))
    with pytest.raises(EngineUnavailableError):
        metadata.fetch("openai-api", API, KEY)
