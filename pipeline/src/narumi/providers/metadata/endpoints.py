"""Closed endpoint validation shared by connection storage and discovery."""

from __future__ import annotations

import ipaddress
from typing import NoReturn
from urllib.parse import urlsplit

from narumi.errors import InvalidArgumentError

ANTHROPIC_ENDPOINT = "https://api.anthropic.com"
OLLAMA_ENDPOINT = "http://127.0.0.1:11434"


def validate_endpoint(provider_id: str, value: str) -> str:
    """Return a canonical allowed origin without echoing rejected input."""
    if not isinstance(provider_id, str) or provider_id not in {
        "anthropic-api",
        "claude-agent-sdk",
        "ollama",
    }:
        raise InvalidArgumentError("Unsupported provider", details={"reason": "invalid_provider"})
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 2048
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(char in value for char in "\\?#")
    ):
        _invalid_endpoint()
    try:
        parsed = urlsplit(value)
        port = parsed.port
        hostname = parsed.hostname
    except ValueError:
        _invalid_endpoint()
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or port == 0
    ):
        _invalid_endpoint()
    if provider_id != "ollama":
        if parsed.scheme != "https" or hostname != "api.anthropic.com" or port not in {None, 443}:
            _invalid_endpoint()
        return ANTHROPIC_ENDPOINT
    if "%" in hostname:
        _invalid_endpoint()
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        _invalid_endpoint()
    if not address.is_loopback or (
        address.version == 6 and address != ipaddress.IPv6Address("::1")
    ):
        _invalid_endpoint()
    host = f"[{address.compressed}]" if address.version == 6 else str(address)
    return f"{parsed.scheme}://{host}" + (f":{port}" if port is not None else "")


def _invalid_endpoint() -> NoReturn:
    raise InvalidArgumentError(
        "Provider endpoint is not an allowed origin", details={"reason": "invalid_endpoint"}
    )
