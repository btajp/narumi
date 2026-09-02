from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
from narumi.providers.claude.protocol import WorkerRequest
from narumi.providers.claude.runtime import CLI_VERSION
from narumi.providers.claude.worker import _reject_managed_policy, execute

CONNECTION = "conn-0123456789abcdef"
KEY = "synthetic-claude-sdk-key-49713"
MODEL = "claude-fixture-1-20260901"
RUNTIME = {
    "resource_id": "claude-agent-sdk-0-2-144",
    "sdk_version": "0.2.144",
    "cli_version": "2.1.239",
    "cli_sha256": "a" * 64,
    "sdk_source_sha256": "b" * 64,
    "isolation_profile_sha256": "c" * 64,
}


@dataclass
class Text:
    text: str


@dataclass
class Thinking:
    thinking: str
    signature: str


@dataclass
class Tool:
    name: str


@dataclass
class System:
    subtype: str
    data: dict


@dataclass
class Assistant:
    content: list
    model: str = MODEL
    parent_tool_use_id: str | None = None
    error: str | None = None
    stop_reason: str | None = "end_turn"
    usage: dict | None = None

    def __post_init__(self):
        if self.usage is None:
            self.usage = {"input_tokens": 12, "output_tokens": 4}


@dataclass
class Result:
    subtype: str = "success"
    is_error: bool = False
    num_turns: int = 1
    stop_reason: str | None = "end_turn"
    terminal_reason: str | None = "completed"
    result: str | None = "Fixture minutes"
    structured_output: object | None = None
    deferred_tool_use: object | None = None
    permission_denials: list | None = None
    errors: list | None = None
    api_error_status: int | None = None
    total_cost_usd: float | None = 0.01
    usage: dict | None = None
    model_usage: dict | None = None

    def __post_init__(self):
        if self.usage is None:
            self.usage = {"input_tokens": 12, "output_tokens": 4}
        if self.model_usage is None:
            self.model_usage = {
                MODEL: {
                    "inputTokens": 12,
                    "outputTokens": 4,
                    "cacheReadInputTokens": 0,
                    "cacheCreationInputTokens": 0,
                    "webSearchRequests": 0,
                    "costUSD": 0.01,
                    "contextWindow": 200000,
                    "maxOutputTokens": 8192,
                    "canonicalModel": MODEL,
                    "provider": "firstParty",
                }
            }


def init(cwd: Path, **changes):
    data = {
        "type": "system",
        "subtype": "init",
        "apiKeySource": "ANTHROPIC_API_KEY",
        "claude_code_version": CLI_VERSION,
        "cwd": str(cwd),
        "model": MODEL,
        "permissionMode": "dontAsk",
        "agents": [],
        "betas": [],
        "tools": [],
        "mcp_servers": [],
        "slash_commands": [],
        "skills": [],
        "plugins": [],
    }
    data.update(changes)
    return System("init", data)


def fake_sdk(messages, captured):
    class Options:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    async def query(*, prompt, options):
        captured["prompt"] = prompt
        captured["options"] = options
        for message in messages:
            yield message

    return SimpleNamespace(
        query=query,
        options=Options,
        AssistantMessage=Assistant,
        ResultMessage=Result,
        SystemMessage=System,
        TextBlock=Text,
        ThinkingBlock=Thinking,
        evidence=SimpleNamespace(
            cli_path=Path("/fixture/bundled/claude"),
            public=lambda: dict(RUNTIME),
        ),
    )


@pytest.mark.asyncio
async def test_worker_uses_only_fixed_isolated_sdk_options(tmp_path, monkeypatch):
    captured = {}
    ambient = {
        "ANTHROPIC_API_KEY": "ambient-key-must-not-win",
        "CLAUDE_CONFIG_DIR": "/ambient/config",
        "CLAUDE_CODE_OAUTH_TOKEN": "ambient-oauth",
    }
    for key, value in ambient.items():
        monkeypatch.setenv(key, value)
    sdk = fake_sdk(
        [
            init(tmp_path),
            Assistant([Thinking("private", "signature"), Text("Fixture minutes")]),
            Result(),
        ],
        captured,
    )
    response = await execute(
        WorkerRequest(
            CONNECTION,
            KEY,
            MODEL,
            "Synthetic transcript",
            "Synthetic instructions",
            RUNTIME,
        ),
        tmp_path,
        sdk=sdk,
    )
    assert response.text == "Fixture minutes" and response.returned_model == MODEL
    assert response.usage == {"input_tokens": 12, "output_tokens": 4}
    assert "Synthetic transcript" in captured["prompt"]
    assert "Synthetic instructions" in captured["prompt"]
    assert "Synthetic instructions" not in captured["system_prompt"]
    assert "Synthetic transcript" not in captured["system_prompt"]
    assert captured["tools"] == captured["allowed_tools"] == []
    assert captured["mcp_servers"] == captured["hooks"] == captured["agents"] == {}
    assert captured["plugins"] == captured["skills"] == captured["setting_sources"] == []
    assert captured["strict_mcp_config"] is True
    assert captured["permission_mode"] == "dontAsk" and captured["max_turns"] == 1
    assert captured["fallback_model"] is None and captured["session_store"] is None
    assert captured["continue_conversation"] is captured["enable_file_checkpointing"] is False
    assert captured["extra_args"]["no-session-persistence"] is None
    assert captured["extra_args"]["bare"] is captured["extra_args"]["safe-mode"] is None
    assert captured["env"]["ANTHROPIC_API_KEY"] == KEY
    assert captured["env"]["CLAUDE_CODE_MAX_RETRIES"] == "0"
    assert captured["env"]["CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK"] == "1"
    assert captured["env"]["ANTHROPIC_BASE_URL"] == "https://api.anthropic.com"
    assert all(value not in repr(captured) for value in ambient.values())
    real_options = ClaudeAgentOptions(
        **{key: value for key, value in captured.items() if key not in {"prompt", "options"}}
    )
    command = SubprocessCLITransport(captured["prompt"], real_options)._build_command()
    assert "Synthetic instructions" not in command
    assert "Synthetic transcript" not in command
    assert KEY not in command


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "messages",
    [
        lambda cwd: [init(cwd, tools=["Read"]), Assistant([Text("Fixture minutes")]), Result()],
        lambda cwd: [init(cwd), Assistant([Tool("Read")]), Result()],
        lambda cwd: [init(cwd), Assistant([Text("Fixture minutes")], model="other"), Result()],
        lambda cwd: [init(cwd), Assistant([Text("Fixture minutes")]), Result(), Result()],
        lambda cwd: [init(cwd), Result()],
    ],
)
async def test_worker_rejects_capabilities_model_changes_and_invalid_streams(tmp_path, messages):
    captured = {}
    with pytest.raises(ValueError):
        await execute(
            WorkerRequest(CONNECTION, KEY, MODEL, "Synthetic transcript", None, RUNTIME),
            tmp_path,
            sdk=fake_sdk(messages(tmp_path), captured),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"terminal_reason": "max_turns"},
        {"permission_denials": ["Read"]},
        {"is_error": True},
        {"stop_reason": "max_tokens"},
        {"model_usage": {"other": {}}},
        {"usage": {"input_tokens": -1, "output_tokens": 2}},
    ],
)
async def test_worker_rejects_nonterminal_refused_or_invalid_usage(tmp_path, change):
    captured = {}
    result = Result()
    for key, value in change.items():
        setattr(result, key, value)
    with pytest.raises(ValueError):
        await execute(
            WorkerRequest(CONNECTION, KEY, MODEL, "Synthetic transcript", None, RUNTIME),
            tmp_path,
            sdk=fake_sdk([init(tmp_path), Assistant([Text("Fixture minutes")]), result], captured),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("assistant_usage", "result_usage", "model_change"),
    [
        (
            {"input_tokens": 12, "output_tokens": 4},
            {"input_tokens": 13, "output_tokens": 4},
            None,
        ),
        (
            {"input_tokens": 12, "output_tokens": 4},
            {
                "input_tokens": 12,
                "output_tokens": 4,
                "server_tool_use": {"web_fetch_requests": 1},
            },
            None,
        ),
        ({"input_tokens": 12, "output_tokens": 4}, None, {"provider": "thirdParty"}),
        ({"input_tokens": 12, "output_tokens": 4}, None, {"contextWindow": 0}),
    ],
)
async def test_worker_rejects_inconsistent_usage_and_hidden_provider_activity(
    tmp_path,
    assistant_usage,
    result_usage,
    model_change,
):
    captured = {}
    result = Result()
    if result_usage is not None:
        result.usage = result_usage
    if model_change is not None:
        result.model_usage[MODEL].update(model_change)
    with pytest.raises(ValueError):
        await execute(
            WorkerRequest(CONNECTION, KEY, MODEL, "Synthetic transcript", None, RUNTIME),
            tmp_path,
            sdk=fake_sdk(
                [
                    init(tmp_path),
                    Assistant([Text("Fixture minutes")], usage=assistant_usage),
                    result,
                ],
                captured,
            ),
        )


def test_worker_fails_closed_when_managed_policy_is_present(tmp_path):
    policy = tmp_path / "managed-settings.json"
    policy.write_text("{}")
    with pytest.raises(RuntimeError, match="managed policy"):
        _reject_managed_policy((policy,))
