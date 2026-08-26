from pathlib import Path

import pytest
from narumi.errors import (
    EngineUnavailableError,
    InvalidArgumentError,
    NotFoundError,
    PolicyViolationError,
)
from narumi.llm import (
    PROVIDER_PROFILES,
    CapabilityProfile,
    FakeProvider,
    LLMProvider,
    NoneProvider,
    available_providers,
    check_policy,
    get_provider,
    provider_names,
    provider_profile,
    select_provider,
)
from narumi.llm.claude_agent import ClaudeAgentSDKProvider
from narumi.llm.ollama import OllamaProvider
from narumi.models import ExternalSendPolicy, MeetingConfig

LOCAL = CapabilityProfile(
    vision=False, context_window=8000, cost_class="local", data_destination="local", tool_use=False
)
SUBSCRIPTION = CapabilityProfile(
    vision=True,
    context_window=200_000,
    cost_class="subscription",
    data_destination="anthropic",
    tool_use=True,
)
API = CapabilityProfile(
    vision=True,
    context_window=200_000,
    cost_class="api",
    data_destination="anthropic",
    tool_use=True,
)

MATRIX = [
    (ExternalSendPolicy.LOCAL_ONLY, LOCAL, True),
    (ExternalSendPolicy.LOCAL_ONLY, SUBSCRIPTION, False),
    (ExternalSendPolicy.LOCAL_ONLY, API, False),
    (ExternalSendPolicy.SUBSCRIPTION_OK, LOCAL, True),
    (ExternalSendPolicy.SUBSCRIPTION_OK, SUBSCRIPTION, True),
    (ExternalSendPolicy.SUBSCRIPTION_OK, API, False),
    (ExternalSendPolicy.API_OK, LOCAL, True),
    (ExternalSendPolicy.API_OK, SUBSCRIPTION, True),
    (ExternalSendPolicy.API_OK, API, True),
]


@pytest.mark.parametrize(("policy", "profile", "allowed"), MATRIX)
def test_policy_matrix(policy, profile, allowed):
    if allowed:
        check_policy(profile, policy, provider="p")
    else:
        with pytest.raises(PolicyViolationError) as info:
            check_policy(profile, policy, provider="p")
        err = info.value
        assert err.code == "policy_violation"
        assert err.details == {
            "provider": "p",
            "data_destination": profile.data_destination,
            "cost_class": profile.cost_class,
            "policy": policy.value,
        }
        assert "anthropic" in err.message and policy.value in err.message


def test_policy_accepts_string_policy():
    check_policy(LOCAL, "local_only")  # type: ignore[arg-type]
    with pytest.raises(PolicyViolationError):
        check_policy(API, "subscription_ok")  # type: ignore[arg-type]


# ------------------------------------------------------------------ registry
def test_registry_names_and_profiles():
    assert provider_names() == ["none", "fake", "claude-agent-sdk", "anthropic-api", "ollama"]
    assert set(PROVIDER_PROFILES) == set(provider_names())
    assert provider_profile("none").data_destination == "local"
    assert provider_profile("fake").cost_class == "local"
    assert provider_profile("claude-agent-sdk") == CapabilityProfile(
        vision=True,
        context_window=200_000,
        cost_class="subscription",
        data_destination="anthropic",
        tool_use=True,
        max_output_tokens=8192,
    )
    assert provider_profile("anthropic-api").cost_class == "api"
    assert provider_profile("anthropic-api").data_destination == "anthropic"
    assert provider_profile("ollama").data_destination == "local"
    with pytest.raises(NotFoundError):
        provider_profile("nope")
    with pytest.raises(NotFoundError):
        get_provider("nope")


def test_available_providers_always_has_local_ones():
    names = available_providers()
    assert names[:2] == ["none", "fake"]
    assert "ollama" in names
    assert set(names) <= set(provider_names())


def test_get_provider_none_and_fake():
    none = get_provider("none")
    assert isinstance(none, NoneProvider) and isinstance(none, LLMProvider)
    with pytest.raises(InvalidArgumentError, match="llm_provider is none"):
        none.complete("hi")
    fake = get_provider("fake")
    assert isinstance(fake, FakeProvider) and isinstance(fake, LLMProvider)


def test_select_provider_enforces_policy_without_instantiating(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = MeetingConfig(llm_provider="anthropic-api", external_send_policy="local_only")
    with pytest.raises(PolicyViolationError) as info:
        select_provider(config)
    assert info.value.details["provider"] == "anthropic-api"
    with pytest.raises(PolicyViolationError):
        select_provider(
            MeetingConfig(llm_provider="anthropic-api", external_send_policy="subscription_ok")
        )
    with pytest.raises(PolicyViolationError):
        select_provider(
            MeetingConfig(llm_provider="claude-agent-sdk", external_send_policy="local_only")
        )
    # allowed by policy but no key → engine_unavailable, not policy_violation
    with pytest.raises(EngineUnavailableError):
        select_provider(MeetingConfig(llm_provider="anthropic-api", external_send_policy="api_ok"))


def test_select_provider_local_defaults():
    assert select_provider(MeetingConfig()).name == "none"
    assert select_provider(MeetingConfig(llm_provider="fake")).name == "fake"
    assert select_provider(MeetingConfig(llm_provider="ollama")).name == "ollama"


@pytest.mark.skipif("claude-agent-sdk" not in available_providers(), reason="sdk not installed")
def test_claude_agent_sdk_provider_rejects_images(tmp_path: Path):
    provider = select_provider(
        MeetingConfig(llm_provider="claude-agent-sdk", external_send_policy="subscription_ok")
    )
    assert isinstance(provider, ClaudeAgentSDKProvider)
    image = tmp_path / "x.png"
    image.write_bytes(b"")
    with pytest.raises(InvalidArgumentError):
        provider.complete("describe", images=[image])


# ------------------------------------------------------------------ fake
def test_fake_provider_is_deterministic_and_records_calls():
    line1 = "- [00:00:00] **岡村**: こんにちは。"
    line2 = "- [00:00:05] **other（未特定）**: はい。"
    prompt = (
        "指示です。\n\n## アジェンダ\n## 決定事項\n\n"
        f"<transcript>\n{line1}\n{line2}\n</transcript>\n"
    )
    a, b = FakeProvider(), FakeProvider()
    out1 = a.complete(prompt, system="sys", max_tokens=10)
    out2 = b.complete(prompt, system="sys", max_tokens=10)
    assert out1 == out2
    excerpt = f"{line1} {line2}"
    assert out1 == f"## アジェンダ\n- （fake）{excerpt}\n## 決定事項\n- （fake）{excerpt}\n"
    assert len(a.calls) == 1
    assert a.calls[0].prompt == prompt and a.calls[0].system == "sys"
    assert a.calls[0].max_tokens == 10 and a.calls[0].images == ()


def test_fake_provider_without_headers_returns_excerpt():
    fake = FakeProvider()
    long_text = "あ" * 100
    assert fake.complete(f"<transcript>\n{long_text}\n</transcript>") == "（fake）" + "あ" * 60
    assert fake.complete("plain prompt") == "（fake）plain prompt"


def test_fake_provider_ignores_headers_inside_data_blocks():
    fake = FakeProvider()
    prompt = "## 議論サマリ\n<summaries>\n## アジェンダ\n- x\n</summaries>\n"
    assert fake.complete(prompt) == "## 議論サマリ\n- （fake）## アジェンダ - x\n"


# ------------------------------------------------------------------ ollama
def test_ollama_connection_error_is_engine_unavailable():
    provider = OllamaProvider(host="http://127.0.0.1:9", timeout=1.0)
    with pytest.raises(EngineUnavailableError) as info:
        provider.complete("hello")
    assert info.value.details["provider"] == "ollama"


def test_ollama_rejects_invalid_host(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "ftp://x")
    with pytest.raises(InvalidArgumentError):
        OllamaProvider()
    monkeypatch.setenv("OLLAMA_HOST", "http://localhost:11434/")
    monkeypatch.setenv("NARUMI_OLLAMA_MODEL", "m:1")
    provider = OllamaProvider()
    assert provider.host == "http://localhost:11434" and provider.model == "m:1"
