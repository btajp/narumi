"""Validate text-only HTTP replies without retaining provider diagnostics or state."""

from __future__ import annotations

from typing import Any

from narumi.errors import EngineUnavailableError
from narumi.providers.metadata.ollama import local_selector
from narumi.providers.metadata.openai_capabilities import confirmed_resolved_model_ids
from narumi.providers.metadata.validation import check_public_payload

OUTCOME_UNKNOWN = "provider_generation_outcome_unknown"


def generation_unknown() -> EngineUnavailableError:
    return EngineUnavailableError(
        "The provider generation outcome is unknown; explicitly start a new attempt to resend",
        details={"reason": OUTCOME_UNKNOWN, "outcome_unknown": True},
    )


class _Refusal(Exception):
    pass


def parse_response(
    provider_id: str,
    model_id: str,
    response: Any,
    *,
    api_key: str | None,
) -> tuple[str, str, dict[str, int] | None]:
    """Only verified completed text and observed integer counters cross this boundary."""
    try:
        body = _object(response)
        secrets = (api_key, "Bearer " + api_key) if api_key else ()
        check_public_payload(body, secrets=secrets, reject_credentials=False)
        if body.get("error") is not None:
            raise ValueError
        returned_model = body.get("model")
        if provider_id == "openai-api":
            allowed_models = confirmed_resolved_model_ids(model_id)
            _require(isinstance(returned_model, str) and returned_model in allowed_models)
            text = _openai_text(body)
            usage = _api_usage(body.get("usage"), openai=True)
        elif provider_id == "anthropic-api":
            _require(returned_model == model_id)
            text = _anthropic_text(body)
            usage = _api_usage(body.get("usage"), openai=False)
        elif provider_id == "ollama":
            # GenerateHandler returns the supplied selector, including :local.
            _require(returned_model == local_selector(model_id))
            text = _ollama_text(body)
            usage = _counters(
                body, {"prompt_eval_count": "input_tokens", "eval_count": "output_tokens"}
            )
        else:
            raise ValueError
        _require(isinstance(text, str) and bool(text.strip()))
        return text.strip(), returned_model, usage or None
    except _Refusal:
        raise EngineUnavailableError(
            "The provider refused this generation request",
            details={"reason": "provider_generation_refused"},
        ) from None
    except Exception:
        # A syntactically valid HTTP 200 is not proof of a completed, usable reply.
        # Never include its contents, identifiers or nested exception diagnostics.
        raise generation_unknown() from None


def _openai_text(body: dict[str, Any]) -> str:
    _require(
        body.get("object") == "response"
        and body.get("status") == "completed"
        and body.get("incomplete_details") is None
    )
    output = body.get("output")
    _require(isinstance(output, list) and bool(output))
    final_text: list[str] = []
    unphased_text: list[str] = []
    has_final_answer = False
    refused = False
    for raw in output:
        item = _object(raw)
        if item.get("type") == "reasoning":
            _require(item.get("status") in {None, "completed"})
            summary = item.get("summary", [])
            _require(isinstance(summary, list))
            for block in summary:
                block = _object(block)
                _require(block.get("type") == "summary_text" and isinstance(block.get("text"), str))
            continue
        _require(
            item.get("type") == "message"
            and item.get("role") == "assistant"
            and item.get("status") == "completed"
        )
        phase = item.get("phase")
        _require(phase in (None, "commentary", "final_answer"))
        content = item.get("content")
        _require(isinstance(content, list) and bool(content))
        message_text: list[str] = []
        for raw_content in content:
            block = _object(raw_content)
            if block.get("type") == "refusal":
                _require(isinstance(block.get("refusal"), str))
                refused = True
            else:
                _require(block.get("type") == "output_text" and isinstance(block.get("text"), str))
                message_text.append(block["text"])
        if phase == "final_answer":
            has_final_answer = True
            final_text.extend(message_text)
        elif phase is None:
            unphased_text.extend(message_text)
    if refused:
        raise _Refusal
    return "".join(final_text if has_final_answer else unphased_text)


def _anthropic_text(body: dict[str, Any]) -> str:
    _require(body.get("type") == "message" and body.get("role") == "assistant")
    content = body.get("content")
    _require(isinstance(content, list) and bool(content))
    text: list[str] = []
    for raw in content:
        block = _object(raw)
        kind = block.get("type")
        if kind == "text":
            _require(isinstance(block.get("text"), str))
            text.append(block["text"])
        elif kind == "thinking":
            _require(isinstance(block.get("thinking"), str))
        elif kind == "redacted_thinking":
            _require(isinstance(block.get("data"), str))
        else:
            raise ValueError
    if body.get("stop_reason") == "refusal":
        raise _Refusal
    # A token-limit stop is a partial document, not a successful minutes artifact.
    _require(body.get("stop_reason") == "end_turn" and body.get("stop_sequence") is None)
    return "".join(text)


def _ollama_text(body: dict[str, Any]) -> str:
    _require(body.get("done") is True and body.get("done_reason") == "stop")
    _require(all(body.get(field) in (None, "") for field in ("remote_host", "remote_model")))
    _require(body.get("tool_calls") in (None, []) and body.get("image") in (None, ""))
    _require(body.get("thinking") is None or isinstance(body["thinking"], str))
    return body.get("response")


def _api_usage(raw: Any, *, openai: bool) -> dict[str, int] | None:
    if raw is None:
        return None
    usage = _object(raw)
    names = {"input_tokens": "input_tokens", "output_tokens": "output_tokens"}
    if openai:
        names["total_tokens"] = "total_tokens"
    else:
        names.update(
            cache_read_input_tokens="cached_input_tokens",
            cache_creation_input_tokens="cache_write_input_tokens",
        )
    result = _counters(usage, names)
    if openai:
        for field, fields in (
            (
                "input_tokens_details",
                {
                    "cached_tokens": "cached_input_tokens",
                    "cache_write_tokens": "cache_write_input_tokens",
                },
            ),
            ("output_tokens_details", {"reasoning_tokens": "reasoning_output_tokens"}),
        ):
            if usage.get(field) is not None:
                result.update(_counters(_object(usage[field]), fields))
    return result or None


def _counters(value: dict[str, Any], names: dict[str, str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for source, target in names.items():
        if value.get(source) is None:
            continue
        counter = value[source]
        _require(type(counter) is int and 0 <= counter <= 2**53 - 1)
        result[target] = counter
    return result


def _object(value: Any) -> dict[str, Any]:
    _require(isinstance(value, dict))
    return value


def _require(condition: bool) -> None:
    if not condition:
        raise ValueError
