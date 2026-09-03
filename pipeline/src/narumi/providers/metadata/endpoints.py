"""Closed endpoint validation shared by connection storage and discovery."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable
from typing import NoReturn
from urllib.parse import urlsplit

from narumi.errors import CancelledError, EngineUnavailableError, InvalidArgumentError
from narumi.providers.metadata.deadline import RequestCancelled, resolve_addresses

ANTHROPIC_ENDPOINT = "https://api.anthropic.com"
CODEX_ENDPOINT = "https://chatgpt.com"
OLLAMA_ENDPOINT = "http://127.0.0.1:11434"
OPENAI_ENDPOINT = "https://api.openai.com"
OPENAI_COMPATIBLE_PROVIDER = "openai-compatible-api"
_DNS_NAME = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)
_PATH_SEGMENT = re.compile(r"[A-Za-z0-9._~-]+\Z")


def validate_endpoint(provider_id: str, value: str) -> str:
    """Return a canonical allowed origin without echoing rejected input."""
    if not isinstance(provider_id, str) or provider_id not in {
        "anthropic-api",
        "claude-agent-sdk",
        "codex-app-server",
        "ollama",
        "openai-api",
        OPENAI_COMPATIBLE_PROVIDER,
    }:
        raise InvalidArgumentError("Unsupported provider", details={"reason": "invalid_provider"})
    if provider_id == OPENAI_COMPATIBLE_PROVIDER:
        return validate_openai_compatible_endpoint(value)
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
        endpoint = {
            "codex-app-server": CODEX_ENDPOINT,
            "openai-api": OPENAI_ENDPOINT,
        }.get(provider_id, ANTHROPIC_ENDPOINT)
        host = urlsplit(endpoint).hostname
        if parsed.scheme != "https" or hostname != host or port not in {None, 443}:
            _invalid_endpoint()
        return endpoint
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


def validate_openai_compatible_endpoint(value: str, *, auth_method: str = "api_key") -> str:
    """Validate a custom API base while preserving its exact safe path prefix."""
    if auth_method not in {"api_key", "none"}:
        _invalid_endpoint()
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 2048
        or value != value.strip()
        or not value.isascii()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(char in value for char in "\\?#%")
    ):
        _invalid_endpoint()
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        _invalid_endpoint()
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port == 0
        or parsed.path.endswith("/")
    ):
        _invalid_endpoint()
    segments = parsed.path.split("/")[1:] if parsed.path else []
    if any(
        not segment or segment in {".", ".."} or _PATH_SEGMENT.fullmatch(segment) is None
        for segment in segments
    ):
        _invalid_endpoint()
    address: ipaddress.IPv4Address | ipaddress.IPv6Address | None
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
        if _DNS_NAME.fullmatch(hostname) is None:
            _invalid_endpoint()
    if address is not None:
        if not address.is_loopback and not _public_address(address):
            _invalid_endpoint()
        canonical_host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    else:
        canonical_host = hostname.lower()
    loopback = address is not None and address.is_loopback
    if parsed.scheme == "http" and not loopback:
        _invalid_endpoint()
    if auth_method == "none" and not loopback:
        _invalid_endpoint()
    default_port = 443 if parsed.scheme == "https" else 80
    authority = canonical_host + (f":{port}" if port is not None and port != default_port else "")
    return f"{parsed.scheme}://{authority}{parsed.path}"


def is_loopback_endpoint(endpoint: str) -> bool:
    """Return true only for a canonical numeric loopback HTTP(S) endpoint."""
    try:
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            return False
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except (TypeError, ValueError):
        return False


def resolve_openai_compatible_addresses(
    endpoint: str,
    *,
    timeout: float = 10.0,
    resolver: Callable[..., list[tuple]] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[str, ...]:
    """Resolve once and reject a remote hostname if any answer is not globally routable."""
    endpoint = validate_openai_compatible_endpoint(endpoint)
    parsed = urlsplit(endpoint)
    hostname = parsed.hostname
    if hostname is None:
        _invalid_endpoint()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    numeric_endpoint = True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        numeric_endpoint = False
        try:
            addresses = resolve_addresses(
                hostname,
                port,
                timeout=timeout,
                resolver=resolver,
                should_cancel=should_cancel,
            )
        except RequestCancelled:
            raise CancelledError(
                "Provider request was cancelled before sending",
                details={"reason": "provider_generation_cancelled"},
            ) from None
        except Exception:
            raise EngineUnavailableError(
                "Provider endpoint DNS resolution failed",
                details={"reason": "metadata_connection_failed"},
            ) from None
    else:
        addresses = (address.compressed,)
    try:
        approved = tuple(ipaddress.ip_address(item) for item in addresses)
    except ValueError:
        _invalid_endpoint()
    if not approved or any(
        (not _public_address(item))
        if not numeric_endpoint
        else (not item.is_loopback and not _public_address(item))
        for item in approved
    ):
        _invalid_endpoint()
    return tuple(item.compressed for item in approved)


def _public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


def _invalid_endpoint() -> NoReturn:
    raise InvalidArgumentError(
        "Provider endpoint is not an allowed origin", details={"reason": "invalid_endpoint"}
    )
