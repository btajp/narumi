"""A single ephemeral, text-only turn whose completion is explicitly observed."""

from __future__ import annotations

import time
from typing import Any

from narumi.errors import CancelledError, EngineUnavailableError, NarumiError
from narumi.providers.codex._policy import BASE_INSTRUCTIONS, MODEL_PROVIDER
from narumi.providers.codex._rpc import MAX_MESSAGE_BYTES, unavailable
from narumi.providers.codex._runtime import SUPPORTED_VERSION
from narumi.providers.codex._session import CodexSession

GENERATION_TIMEOUT = 300.0


def thread_parameters(
    session: CodexSession, model: dict[str, Any], parameters: dict[str, Any], system: str | None
) -> dict[str, Any]:
    effort = (
        parameters.get("reasoning_effort")
        or (model["parameter_schema"]["properties"]["reasoning_effort"]["default"])
    )
    return {
        "model": model["model_id"],
        "modelProvider": MODEL_PROVIDER,
        "allowProviderModelFallback": False,
        "cwd": str(session.cwd),
        "ephemeral": True,
        "approvalPolicy": "never",
        "sandbox": "read-only",
        "baseInstructions": system if system is not None else BASE_INSTRUCTIONS,
        "developerInstructions": "",
        "config": {"model_reasoning_effort": effort},
        "environments": [],
        "dynamicTools": [],
        "selectedCapabilityRoots": [],
        "runtimeWorkspaceRoots": [],
    }


def verify_thread(body: dict[str, Any], expected: dict[str, Any]) -> str:
    thread, sandbox = body.get("thread"), body.get("sandbox")
    if not isinstance(thread, dict) or not isinstance(sandbox, dict):
        raise unavailable("codex_thread_isolation_unverified")
    identifier = thread.get("id")
    if not isinstance(identifier, str) or not 1 <= len(identifier) <= 256:
        raise unavailable("codex_thread_isolation_unverified")
    if (
        body.get("model") != expected["model"]
        or body.get("modelProvider") != MODEL_PROVIDER
        or body.get("cwd") != expected["cwd"]
        or body.get("approvalPolicy") != "never"
        or body.get("instructionSources") != []
        or body.get("runtimeWorkspaceRoots") != []
        or body.get("reasoningEffort") != expected["config"]["model_reasoning_effort"]
        or sandbox.get("type") != "readOnly"
        or sandbox.get("networkAccess", False) is not False
        or thread.get("modelProvider") != MODEL_PROVIDER
        or thread.get("cwd") != expected["cwd"]
        or thread.get("ephemeral") is not True
        or thread.get("cliVersion") != SUPPORTED_VERSION
        or thread.get("path") is not None
        or thread.get("turns") != []
        or thread.get("projectId") is not None
        or thread.get("parentThreadId") is not None
    ):
        raise unavailable("codex_thread_isolation_unverified")
    return identifier


def generate(
    session: CodexSession,
    model: dict[str, Any],
    parameters: dict[str, Any],
    prompt: str,
    *,
    system: str | None,
) -> str:
    session.verify_configuration()
    expected = thread_parameters(session, model, parameters, system)
    started = session.call("thread/start", expected)
    thread_id = verify_thread(started, expected)
    session.verify_empty_capabilities(thread_id)
    session.verify_configuration()
    sent = False
    turn_id: str | None = None

    def mark_sent() -> None:
        nonlocal sent
        sent = True
        session.generation_attempted = True

    try:
        body = session.call(
            "turn/start",
            {
                "threadId": thread_id,
                "model": model["model_id"],
                "effort": expected["config"]["model_reasoning_effort"],
                "input": [{"type": "text", "text": prompt}],
                "environments": [],
                "runtimeWorkspaceRoots": [],
            },
            on_sent=mark_sent,
        )
        turn = body.get("turn")
        if not isinstance(turn, dict):
            raise unavailable("codex_invalid_turn")
        turn_id = turn.get("id")
        if not isinstance(turn_id, str) or not 1 <= len(turn_id) <= 256:
            turn_id = None
            raise unavailable("codex_invalid_turn")
        if turn.get("status") not in {"inProgress", "completed"}:
            raise unavailable("codex_invalid_turn")
        return _collect(session, thread_id, turn_id)
    except CancelledError:
        interrupted = sent and turn_id is not None and _interrupt(session, thread_id, turn_id)
        raise CancelledError(
            "Codex generation was cancelled",
            details={
                "reason": "codex_generation_cancelled",
                "outcome_unknown": bool(sent and not interrupted),
            },
        ) from None
    except NarumiError as error:
        if sent and error.details.get("reason") != "codex_generation_interrupted":
            raise unavailable("codex_generation_outcome_unknown") from None
        raise
    except Exception:
        reason = "codex_generation_outcome_unknown" if sent else "codex_generation_failed"
        raise unavailable(reason) from None


def _collect(session: CodexSession, thread_id: str, turn_id: str) -> str:
    if session.rpc is None:
        raise unavailable("codex_process_closed")
    deadline = time.monotonic() + GENERATION_TIMEOUT
    messages: dict[str, tuple[str | None, str]] = {}
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise unavailable("codex_rpc_timeout")
        notification = session.rpc.wait_for(lambda _: True, timeout=remaining)
        method, params = notification["method"], notification.get("params", {})
        if method in {"model/rerouted", "modelRerouted", "configWarning", "warning"}:
            raise unavailable("codex_runtime_configuration_changed")
        if params.get("threadId") != thread_id:
            continue
        if method == "error":
            raise unavailable("codex_generation_error")
        if method == "turn/completed":
            turn = params.get("turn")
            if not isinstance(turn, dict) or turn.get("id") != turn_id:
                continue
            status = turn.get("status")
            if status == "interrupted":
                raise EngineUnavailableError(
                    "Codex generation was interrupted",
                    details={"reason": "codex_generation_interrupted"},
                )
            if status == "failed":
                raise unavailable("codex_generation_failed")
            if status != "completed" or turn.get("error") is not None:
                raise unavailable("codex_invalid_completion")
            items = turn.get("items")
            if not isinstance(items, list):
                raise unavailable("codex_invalid_completion")
            for item in items:
                _item(item, messages)
            return _final_text(messages)
        if params.get("turnId") != turn_id:
            continue
        if method in {"item/started", "item/completed"}:
            item = params.get("item")
            if not isinstance(item, dict) or item.get("type") not in {
                "userMessage",
                "agentMessage",
                "reasoning",
            }:
                raise unavailable("codex_unexpected_tool_activity")
            if method == "item/completed":
                _item(item, messages)


def _item(item: Any, messages: dict[str, tuple[str | None, str]]) -> None:
    if not isinstance(item, dict) or item.get("type") not in {
        "userMessage",
        "agentMessage",
        "reasoning",
    }:
        raise unavailable("codex_unexpected_tool_activity")
    if item["type"] != "agentMessage":
        return
    identifier, text, phase = item.get("id"), item.get("text"), item.get("phase")
    if (
        not isinstance(identifier, str)
        or not 1 <= len(identifier) <= 256
        or not isinstance(text, str)
        or len(text.encode("utf-8")) > MAX_MESSAGE_BYTES
        or phase not in {None, "commentary", "final_answer"}
        or len(messages) >= 1000
    ):
        raise unavailable("codex_invalid_generation_output")
    previous = messages.get(identifier)
    if previous is not None and previous != (phase, text):
        raise unavailable("codex_inconsistent_generation_output")
    messages[identifier] = phase, text


def _final_text(messages: dict[str, tuple[str | None, str]]) -> str:
    final = [text for phase, text in messages.values() if phase == "final_answer"]
    if not final:
        final = [text for phase, text in messages.values() if phase is None]
    if len(final) != 1 or not final[0].strip():
        raise unavailable("codex_final_output_unverified")
    return final[0]


def _interrupt(session: CodexSession, thread_id: str, turn_id: str) -> bool:
    if session.rpc is None:
        return False
    try:
        with session.rpc.cooperative_cleanup():
            session.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=2)
            completed = session.rpc.wait_for(
                lambda message: (
                    message.get("method") == "turn/completed"
                    and message.get("params", {}).get("threadId") == thread_id
                    and message.get("params", {}).get("turn", {}).get("id") == turn_id
                ),
                timeout=2,
            )
            return completed["params"]["turn"].get("status") == "interrupted"
    except Exception:
        return False
