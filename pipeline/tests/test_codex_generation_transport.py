"""Generation events are not success until the matching terminal turn is verified."""

from __future__ import annotations

import copy
from collections import deque
from contextlib import nullcontext

import pytest
from narumi.errors import CancelledError, EngineUnavailableError
from narumi.providers.codex import _generation, _models, _policy
from narumi.providers.codex._rpc import unavailable
from narumi.providers.codex._runtime import SUPPORTED_VERSION


def _model():
    raw = {
        "id": "catalog-row-1",
        "model": "fixture-model",
        "displayName": "Fixture model",
        "hidden": False,
        "inputModalities": ["text", "image"],
        "supportedReasoningEfforts": [{"reasoningEffort": "low", "description": "Low"}],
        "defaultReasoningEffort": "low",
    }
    return _models.fetch_models(lambda *_: {"data": [raw], "nextCursor": None})[0]


def _answer(text="Fixture minutes", *, identifier="message-1", phase="final_answer"):
    return {"type": "agentMessage", "id": identifier, "phase": phase, "text": text}


def _item(item=None, *, thread="thread-1", turn="turn-1"):
    return {
        "method": "item/completed",
        "params": {"threadId": thread, "turnId": turn, "item": item or _answer()},
    }


def _completed(*, thread="thread-1", turn="turn-1", status="completed", items=None):
    return {
        "method": "turn/completed",
        "params": {
            "threadId": thread,
            "turn": {"id": turn, "status": status, "items": [] if items is None else items},
        },
    }


class FakeSession:
    def __init__(self, cwd, notifications=None):
        self.cwd = cwd
        self.rpc = self
        self.events = deque(notifications if notifications is not None else [_item(), _completed()])
        self.calls = []
        self.thread_mutation = lambda _: None
        self.before_send = None
        self.after_send = None
        self.interrupt_confirmed = True
        self.configuration_error = None

    def verify_configuration(self):
        self.calls.append(("verify_configuration", {}))
        if self.configuration_error:
            raise self.configuration_error

    def verify_empty_capabilities(self, thread_id):
        self.calls.append(("verify_empty_capabilities", {"threadId": thread_id}))

    def call(self, method, params, *, on_sent=None, **_):
        self.calls.append((method, copy.deepcopy(params)))
        if method == "thread/start":
            result = {
                "model": params["model"],
                "modelProvider": _policy.MODEL_PROVIDER,
                "cwd": params["cwd"],
                "approvalPolicy": "never",
                "instructionSources": [],
                "runtimeWorkspaceRoots": [],
                "reasoningEffort": params["config"]["model_reasoning_effort"],
                "sandbox": {"type": "readOnly", "networkAccess": False},
                "thread": {
                    "id": "thread-1",
                    "modelProvider": _policy.MODEL_PROVIDER,
                    "cwd": params["cwd"],
                    "ephemeral": True,
                    "cliVersion": SUPPORTED_VERSION,
                    "path": None,
                    "turns": [],
                    "projectId": None,
                },
            }
            self.thread_mutation(result)
            return result
        if method == "turn/start":
            if self.before_send is not None:
                raise self.before_send
            on_sent()
            if self.after_send is not None:
                raise self.after_send
            return {"turn": {"id": "turn-1", "status": "inProgress", "items": []}}
        if method == "turn/interrupt":
            if self.interrupt_confirmed:
                self.events.append(_completed(status="interrupted"))
            return {}
        raise AssertionError(method)

    def wait_for(self, predicate, **_):
        while self.events:
            event = self.events.popleft()
            if isinstance(event, Exception):
                raise event
            if predicate(event):
                return event
        raise unavailable("codex_rpc_timeout")

    def cooperative_cleanup(self):
        return nullcontext()


def _generate(session):
    return _generation.generate(session, _model(), {}, "Synthetic meeting text", system=None)


def test_text_generation_uses_verified_exact_model_and_no_ambient_capabilities(tmp_path):
    session = FakeSession(tmp_path)
    assert _generate(session) == "Fixture minutes"
    calls = dict(session.calls)
    thread = calls["thread/start"]
    assert thread["model"] == "fixture-model"
    assert thread["modelProvider"] == _policy.MODEL_PROVIDER
    assert thread["allowProviderModelFallback"] is False
    assert (
        thread["environments"] == thread["dynamicTools"] == thread["selectedCapabilityRoots"] == []
    )
    assert thread["ephemeral"] is True
    assert thread["approvalPolicy"] == "never"
    assert thread["sandbox"] == "read-only"
    turn = calls["turn/start"]
    assert turn["input"] == [{"type": "text", "text": "Synthetic meeting text"}]
    assert turn["model"] == "fixture-model" and turn["effort"] == "low"
    assert "max_tokens" not in turn
    assert [name for name, _ in session.calls][-2:] == ["verify_configuration", "turn/start"]


@pytest.mark.parametrize(
    ("target", "key", "value"),
    [
        (None, "model", "substituted-model"),
        (None, "modelProvider", "other-provider"),
        (None, "cwd", "/unrelated"),
        (None, "approvalPolicy", "on-request"),
        (None, "instructionSources", ["/unrelated/AGENTS.md"]),
        (None, "runtimeWorkspaceRoots", ["/unrelated"]),
        (None, "reasoningEffort", "unsupported"),
        ("sandbox", "type", "dangerFullAccess"),
        ("sandbox", "networkAccess", True),
        ("thread", "modelProvider", "other-provider"),
        ("thread", "cwd", "/unrelated"),
        ("thread", "ephemeral", False),
        ("thread", "cliVersion", "unverified-version"),
        ("thread", "path", "/unrelated/history.jsonl"),
        ("thread", "turns", [{"id": "old-turn"}]),
        ("thread", "projectId", "old-project"),
        ("thread", "parentThreadId", "old-thread"),
    ],
)
def test_effective_thread_mismatch_stops_before_sending_meeting(tmp_path, target, key, value):
    session = FakeSession(tmp_path)

    def mutate(result):
        destination = result if target is None else result[target]
        destination[key] = value

    session.thread_mutation = mutate
    with pytest.raises(EngineUnavailableError) as caught:
        _generate(session)
    assert caught.value.details["reason"] == "codex_thread_isolation_unverified"
    assert not any(method == "turn/start" for method, _ in session.calls)


def test_wrong_thread_or_turn_completion_never_completes_selected_turn(tmp_path):
    session = FakeSession(
        tmp_path,
        [
            _item(_answer("Foreign"), thread="foreign"),
            _completed(thread="foreign"),
            _completed(turn="old-turn"),
            _item(),
            _completed(),
        ],
    )
    assert _generate(session) == "Fixture minutes"


@pytest.mark.parametrize("events", [[_item()], [_completed()], [_item(), _completed(status="odd")]])
def test_missing_verified_terminal_result_remains_unknown(tmp_path, events):
    with pytest.raises(EngineUnavailableError) as caught:
        _generate(FakeSession(tmp_path, events))
    assert caught.value.details["reason"] == "codex_generation_outcome_unknown"


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_only_confirmed_interrupt_is_known_after_a_failed_turn(tmp_path, status):
    with pytest.raises(EngineUnavailableError) as caught:
        _generate(FakeSession(tmp_path, [_item(), _completed(status=status)]))
    expected = "interrupted" if status == "interrupted" else "outcome_unknown"
    assert caught.value.details["reason"] == f"codex_generation_{expected}"


@pytest.mark.parametrize(
    "reason",
    ["codex_rpc_timeout", "codex_process_eof", "codex_pipe_unavailable", "codex_rpc_failed"],
)
def test_failed_turn_start_response_after_send_is_unknown_without_resend(tmp_path, reason):
    session = FakeSession(tmp_path)
    session.after_send = unavailable(reason)
    with pytest.raises(EngineUnavailableError) as caught:
        _generate(session)
    assert caught.value.details["reason"] == "codex_generation_outcome_unknown"
    assert sum(method == "turn/start" for method, _ in session.calls) == 1


def test_pre_send_transport_failure_is_not_unknown(tmp_path):
    session = FakeSession(tmp_path)
    session.before_send = unavailable("codex_request_limit")
    with pytest.raises(EngineUnavailableError) as caught:
        _generate(session)
    assert caught.value.details["reason"] == "codex_request_limit"


@pytest.mark.parametrize("confirmed", [True, False])
def test_cancel_after_send_requires_matching_interrupted_acknowledgement(tmp_path, confirmed):
    session = FakeSession(tmp_path, [CancelledError("fixture cancellation")])
    session.interrupt_confirmed = confirmed
    with pytest.raises(CancelledError) as caught:
        _generate(session)
    assert caught.value.details["outcome_unknown"] is not confirmed
    assert sum(method == "turn/interrupt" for method, _ in session.calls) == 1


def test_cancel_before_sending_does_not_interrupt_or_mark_unknown(tmp_path):
    session = FakeSession(tmp_path)
    session.before_send = CancelledError("fixture cancellation")
    with pytest.raises(CancelledError) as caught:
        _generate(session)
    assert caught.value.details["outcome_unknown"] is False
    assert not any(method == "turn/interrupt" for method, _ in session.calls)


@pytest.mark.parametrize(
    "item_type", ["commandExecution", "fileChange", "mcpToolCall", "webSearch"]
)
def test_unexpected_tool_activity_fails_closed(tmp_path, item_type):
    session = FakeSession(tmp_path, [_item({"type": item_type, "id": "tool-1"}), _completed()])
    with pytest.raises(EngineUnavailableError) as caught:
        _generate(session)
    assert caught.value.details["reason"] == "codex_generation_outcome_unknown"


def test_final_answer_is_preferred_and_duplicate_delivery_is_idempotent(tmp_path):
    session = FakeSession(
        tmp_path,
        [
            _item(_answer("Working", identifier="comment", phase="commentary")),
            _item(),
            _completed(items=[_answer()]),
        ],
    )
    assert _generate(session) == "Fixture minutes"


def test_single_phase_null_final_is_compatible(tmp_path):
    assert _generate(FakeSession(tmp_path, [_completed(items=[_answer(phase=None)])])) == (
        "Fixture minutes"
    )


def test_two_final_answers_are_not_silently_selected(tmp_path):
    session = FakeSession(tmp_path, [_item(), _item(_answer(identifier="message-2")), _completed()])
    with pytest.raises(EngineUnavailableError):
        _generate(session)


def test_upstream_error_text_is_not_reflected(tmp_path):
    secret = "fixture-private-upstream-value"
    session = FakeSession(
        tmp_path,
        [{"method": "error", "params": {"threadId": "thread-1", "message": secret}}],
    )
    with pytest.raises(EngineUnavailableError) as caught:
        _generate(session)
    assert caught.value.details["reason"] == "codex_generation_outcome_unknown"
    assert secret not in str(caught.value.to_payload())
