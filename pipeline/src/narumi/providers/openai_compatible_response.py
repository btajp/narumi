"""Strict success parsing for the two explicit OpenAI-compatible API surfaces."""

from __future__ import annotations

from typing import Any

from narumi.errors import EngineUnavailableError
from narumi.providers.metadata.validation import check_public_payload

OUTCOME_UNKNOWN = "provider_generation_outcome_unknown"


class _Refusal(Exception):
    pass


def generation_unknown() -> EngineUnavailableError:
    return EngineUnavailableError(
        "The provider generation outcome is unknown; explicitly start a new attempt to resend",
        details={"reason": OUTCOME_UNKNOWN, "outcome_unknown": True},
    )


def parse_response(
    api_surface: str,
    model_id: str,
    response: Any,
    *,
    api_key: str | None,
    exact_text: str | None = None,
) -> tuple[str, str, dict[str, int] | None]:
    try:
        body = _object(response)
        secrets = (api_key, "Bearer " + api_key) if api_key else ()
        check_public_payload(body, secrets=secrets, reject_credentials=False)
        _require(body.get("error") is None)
        returned_model = body.get("model")
        _require(returned_model == model_id)
        if api_surface == "responses":
            text = _responses_text(body)
            usage = _responses_usage(body.get("usage"))
        elif api_surface == "chat_completions":
            text = _chat_text(body)
            usage = _chat_usage(body.get("usage"))
        else:
            raise ValueError
        _require(isinstance(text, str) and bool(text.strip()))
        check_public_payload(text, secrets=secrets, reject_credentials=False)
        if exact_text is not None:
            _require(text == exact_text)
            return text, returned_model, usage or None
        return text.strip(), returned_model, usage or None
    except _Refusal:
        raise EngineUnavailableError(
            "The provider refused this generation request",
            details={"reason": "provider_generation_refused"},
        ) from None
    except Exception:
        raise generation_unknown() from None


def _responses_text(body: dict[str, Any]) -> str:
    _require(
        body.get("object") == "response"
        and body.get("status") == "completed"
        and body.get("incomplete_details") is None
        and (body.get("parallel_tool_calls") is None or body.get("parallel_tool_calls") is False)
    )
    output = body.get("output")
    _require(isinstance(output, list) and len(output) == 1)
    message = _object(output[0])
    _require(
        set(message) <= {"id", "type", "role", "status", "phase", "content"}
        and message.get("type") == "message"
        and message.get("role") == "assistant"
        and message.get("status") == "completed"
        and message.get("phase") in {None, "final_answer"}
    )
    content = message.get("content")
    _require(isinstance(content, list) and bool(content))
    text: list[str] = []
    for raw in content:
        block = _object(raw)
        if block.get("type") == "refusal":
            _require(set(block) <= {"type", "refusal"} and isinstance(block.get("refusal"), str))
            raise _Refusal
        _require(
            set(block) <= {"type", "text", "annotations", "logprobs"}
            and block.get("type") == "output_text"
            and isinstance(block.get("text"), str)
            and block.get("refusal") is None
        )
        text.append(block["text"])
    return "".join(text)


def _chat_text(body: dict[str, Any]) -> str:
    _require(body.get("object") == "chat.completion")
    choices = body.get("choices")
    _require(isinstance(choices, list) and len(choices) == 1)
    choice = _object(choices[0])
    _require(
        set(choice) <= {"index", "finish_reason", "logprobs", "message"}
        and type(choice.get("index")) is int
        and choice.get("index") == 0
        and choice.get("finish_reason") == "stop"
        and choice.get("logprobs") is None
    )
    message = _object(choice.get("message"))
    _require(
        set(message)
        <= {
            "role",
            "content",
            "refusal",
            "tool_calls",
            "function_call",
            "audio",
            "reasoning_content",
            "annotations",
        }
    )
    refusal = message.get("refusal")
    if refusal is not None:
        _require(isinstance(refusal, str))
        raise _Refusal
    _require(
        message.get("role") == "assistant"
        and isinstance(message.get("content"), str)
        and message.get("tool_calls") in (None, [])
        and message.get("function_call") is None
        and message.get("audio") is None
        and message.get("reasoning_content") is None
    )
    return message["content"]


def _responses_usage(raw: Any) -> dict[str, int] | None:
    if raw is None:
        return None
    usage = _object(raw)
    result = _counters(
        usage,
        {
            "input_tokens": "input_tokens",
            "output_tokens": "output_tokens",
            "total_tokens": "total_tokens",
        },
    )
    if usage.get("input_tokens_details") is not None:
        result.update(
            _counters(
                _object(usage["input_tokens_details"]),
                {
                    "cached_tokens": "cached_input_tokens",
                    "cache_write_tokens": "cache_write_input_tokens",
                },
            )
        )
    if usage.get("output_tokens_details") is not None:
        result.update(
            _counters(
                _object(usage["output_tokens_details"]),
                {"reasoning_tokens": "reasoning_output_tokens"},
            )
        )
    return result or None


def _chat_usage(raw: Any) -> dict[str, int] | None:
    if raw is None:
        return None
    usage = _object(raw)
    result = _counters(
        usage,
        {
            "prompt_tokens": "input_tokens",
            "completion_tokens": "output_tokens",
            "total_tokens": "total_tokens",
        },
    )
    if usage.get("prompt_tokens_details") is not None:
        result.update(
            _counters(
                _object(usage["prompt_tokens_details"]),
                {"cached_tokens": "cached_input_tokens"},
            )
        )
    if usage.get("completion_tokens_details") is not None:
        result.update(
            _counters(
                _object(usage["completion_tokens_details"]),
                {"reasoning_tokens": "reasoning_output_tokens"},
            )
        )
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


__all__ = ["OUTCOME_UNKNOWN", "generation_unknown", "parse_response"]
