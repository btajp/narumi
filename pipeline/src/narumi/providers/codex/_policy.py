"""Fixed isolation policy for the audited Codex App Server 0.150.1 protocol."""

from __future__ import annotations

import ctypes
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from narumi.providers.codex._rpc import unavailable

FEATURES_OFF = (
    "shell_tool",
    "view_image",
    "hooks",
    "code_mode",
    "code_mode_only",
    "code_mode_host",
    "unified_exec",
    "shell_snapshot",
    "shell_snapshot_v2",
    "shell_zsh_fork",
    "exec_permission_approvals",
    "request_permissions_tool",
    "web_search_request",
    "web_search_cached",
    "standalone_web_search",
    "memories",
    "external_agent_memory_import",
    "chronicle",
    "multi_agent",
    "multi_agent_v2",
    "apps",
    "plugins",
    "tool_suggest",
    "recommended_plugins",
    "image_generation",
    "skill_search",
    "goals",
    "current_time_reminder",
    "deferred_executor",
    "token_budget",
    "psp",
    "guardianv2",
    "guardian_approval",
    "unbounded_connection_retries",
    "auth_elicitation",
    "mentions_v2",
    "remote_plugin",
    "remote_control",
    "mcp_2026_07_28",
)
MODEL_PROVIDER = "narumi_codex"
DEVICE_AUTHORIZATION_URL = "https://auth.openai.com/codex/device"
BASE_INSTRUCTIONS = (
    "Generate meeting minutes using only the supplied text. "
    "Do not invoke tools or inspect files, websites, account history, or other conversations. "
    "Return the requested text without adding unsupported facts."
)
FIXED_CONFIG: dict[str, Any] = {
    "model_provider": MODEL_PROVIDER,
    "model_providers": {
        MODEL_PROVIDER: {
            "name": "OpenAI",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "wire_api": "responses",
            "requires_openai_auth": True,
            "request_max_retries": 0,
            "stream_max_retries": 0,
            "supports_websockets": False,
            "supports_standalone_web_search": False,
        }
    },
    "forced_login_method": "chatgpt",
    "openai_base_url": "https://chatgpt.com/backend-api/codex",
    "chatgpt_base_url": "https://chatgpt.com/backend-api/",
    "cli_auth_credentials_store": "file",
    "approval_policy": "never",
    "sandbox_mode": "read-only",
    "web_search": "disabled",
    "notify": [],
    "project_doc_max_bytes": 0,
    "project_root_markers": [],
    "developer_instructions": "",
    "instructions": "",
    "features": {name: False for name in FEATURES_OFF},
    "agents": {"enabled": False},
    "tools": {
        "update_plan": {"enabled": False},
        "experimental_request_user_input": {"enabled": False},
    },
    "orchestrator": {"skills": {"enabled": False}, "mcp": {"enabled": False}},
    "skills": {"include_instructions": False, "bundled": {"enabled": False}},
    "memories": {"generate_memories": False, "use_memories": False},
    "history": {"persistence": "none"},
    "analytics": {"enabled": False},
    "include_environment_context": False,
    "include_collaboration_mode_instructions": False,
    "include_apps_instructions": False,
    "include_permissions_instructions": False,
    "allow_login_shell": False,
    "file_opener": "none",
    "mcp_oauth_credentials_store": "file",
    "hide_agent_reasoning": True,
    "background_terminal_max_timeout": 1000,
    "shell_environment_policy": {
        "inherit": "none",
        "ignore_default_excludes": False,
        "experimental_use_profile": False,
    },
}


def _flatten(value: dict[str, Any], prefix: str = "") -> list[tuple[str, Any]]:
    result = []
    for key, item in value.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict):
            result.extend(_flatten(item, name))
        else:
            result.append((name, item))
    return result


def command(executable: Path, *, catalog: Path | None = None) -> list[str]:
    values = dict(FIXED_CONFIG)
    if catalog is not None:
        values["model_catalog_json"] = str(catalog)
    args = [str(executable), "app-server", "--listen", "stdio://", "--strict-config"]
    for key, value in _flatten(values):
        args.extend(["-c", f"{key}={json.dumps(value, ensure_ascii=False)}"])
    return args


def host_preflight() -> None:
    """Check presence only; never load an existing system or managed configuration."""
    try:
        os.lstat("/etc/codex")
    except FileNotFoundError:
        pass
    except OSError:
        raise unavailable("codex_host_configuration_unverified") from None
    else:
        raise unavailable("codex_host_configuration_present")
    if sys.platform != "darwin":
        return
    try:
        core = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        core.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        core.CFStringCreateWithCString.restype = ctypes.c_void_p
        core.CFPreferencesCopyAppValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        core.CFPreferencesCopyAppValue.restype = ctypes.c_void_p
        core.CFRelease.argtypes = [ctypes.c_void_p]
        core.CFRelease.restype = None
        domain = core.CFStringCreateWithCString(None, b"com.openai.codex", 0x08000100)
        if not domain:
            raise ValueError("managed preference inspection unavailable")
        try:
            for name in (b"config_toml_base64", b"requirements_toml_base64"):
                key = core.CFStringCreateWithCString(None, name, 0x08000100)
                if not key:
                    raise ValueError("managed preference inspection unavailable")
                try:
                    value = core.CFPreferencesCopyAppValue(key, domain)
                    if value:
                        core.CFRelease(value)
                        raise unavailable("codex_managed_configuration_present")
                finally:
                    core.CFRelease(key)
        finally:
            core.CFRelease(domain)
    except (OSError, AttributeError, ValueError):
        raise unavailable("codex_host_configuration_unverified") from None


def verify_configuration(
    body: dict[str, Any], codex_home: Path, *, catalog: Path | None = None
) -> None:
    expected = dict(FIXED_CONFIG)
    if catalog is not None:
        expected["model_catalog_json"] = str(catalog)
    config, layers = body.get("config"), body.get("layers")
    if not isinstance(config, dict) or not isinstance(layers, list) or not layers:
        raise unavailable("codex_configuration_unverified")
    projected = _without_nulls(config)
    for key, value in expected.items():
        actual = projected.get(key)
        # The 0.150.1 public ToolsV2 projection omits these extension switches.
        # Their actual values are still required verbatim in sessionFlags below.
        if key == "tools" and actual == {}:
            continue
        if not _same_json(actual, value):
            raise unavailable("codex_configuration_mismatch")
    for key, value in projected.items():
        if key not in expected and value not in ({}, []):
            raise unavailable("codex_unexpected_configuration")
    saw_flags = False
    for layer in layers:
        if not isinstance(layer, dict) or not isinstance(layer.get("name"), dict):
            raise unavailable("codex_configuration_unverified")
        source = layer["name"]
        kind, layer_config = source.get("type"), layer.get("config")
        if kind == "sessionFlags":
            if saw_flags or not _same_json(layer_config, expected) or layer.get("disabledReason"):
                raise unavailable("codex_configuration_mismatch")
            saw_flags = True
        elif kind == "user":
            if (
                source.get("file") != str(codex_home / "config.toml")
                or source.get("profile") is not None
                or layer_config != {}
            ):
                raise unavailable("codex_user_configuration_present")
        elif kind == "system" and layer_config == {}:
            continue
        else:
            raise unavailable("codex_inherited_configuration_present")
    if not saw_flags:
        raise unavailable("codex_configuration_unverified")


def _without_nulls(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _without_nulls(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_without_nulls(item) for item in value]
    return value


def _same_json(actual: Any, expected: Any) -> bool:
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            _same_json(actual[key], value) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            _same_json(left, right) for left, right in zip(actual, expected, strict=True)
        )
    return actual == expected


def device_authorization(value: Any, user_code: Any) -> tuple[str, str]:
    """Return only the official device page and bounded opaque display code."""
    if (
        not isinstance(value, str)
        or value != DEVICE_AUTHORIZATION_URL
        or not isinstance(user_code, str)
        or re.fullmatch(r"[A-Za-z0-9-]{1,32}", user_code) is None
    ):
        raise unavailable("codex_authorization_code_rejected")
    return value, user_code


def static_catalog(model: dict[str, Any]) -> dict[str, Any]:
    parameter = model["parameter_schema"]["properties"]["reasoning_effort"]
    return {
        "models": [
            {
                "slug": model["model_id"],
                "display_name": model["display_name"],
                "description": "Text-only meeting minutes generation",
                "base_instructions": BASE_INSTRUCTIONS,
                "supported_reasoning_levels": [
                    {"effort": effort, "description": effort} for effort in parameter["enum"]
                ],
                "default_reasoning_level": parameter["default"],
                "shell_type": "disabled",
                "visibility": "list",
                "supported_in_api": False,
                "priority": 0,
                "support_verbosity": False,
                "truncation_policy": {"mode": "tokens", "limit": 10000},
                "experimental_supported_tools": [],
                "input_modalities": ["text"],
                "include_skills_usage_instructions": False,
                "include_plugin_usage_instructions": False,
                "include_apps_usage_instructions": False,
                "supports_reasoning_summary_parameter": False,
                "apply_patch_tool_type": None,
                "context_window": None,
                "max_context_window": None,
                "auto_compact_token_limit": None,
                "supports_search_tool": False,
                "use_responses_lite": False,
                "node_repl_disabled": True,
                "tool_mode": "direct",
                "multi_agent_version": "disabled",
            }
        ]
    }
