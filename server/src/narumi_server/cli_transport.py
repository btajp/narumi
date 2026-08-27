"""A proxy- and redirect-free HTTP path for contract inputs marked writeOnly."""

from __future__ import annotations

import ipaddress
import urllib.parse
import urllib.request
from typing import Any

from narumi.errors import InvalidArgumentError

_ENDPOINT_ERROR = "Write-only tool inputs require a plain loopback HTTP server URL"


def confidential_endpoint(url: str) -> str:
    """Validate once and pin localhost to a numeric address, without DNS resolution."""
    if (
        not isinstance(url, str)
        or not url
        or any(ord(char) <= 32 or ord(char) >= 127 for char in url)
        or any(char in url for char in "\\?#")
    ):
        raise InvalidArgumentError(_ENDPOINT_ERROR)
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
        port = parsed.port
        if (
            parsed.scheme != "http"
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or "%" in parsed.netloc
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ValueError
        address = ipaddress.ip_address("127.0.0.1" if host == "localhost" else host)
        if not address.is_loopback or (
            address.version == 6 and address != ipaddress.IPv6Address("::1")
        ):
            raise ValueError
    except ValueError:
        # Parser diagnostics can contain the supplied URL; keep the error value-free.
        raise InvalidArgumentError(_ENDPOINT_ERROR) from None
    netloc = f"[{address}]" if address.version == 6 else str(address)
    if port is not None:
        netloc += f":{port}"
    return urllib.parse.urlunsplit(("http", netloc, parsed.path or "/", "", ""))


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class ConfidentialHttpTransport:
    """One pinned endpoint and one private opener for the complete MCP session."""

    def __init__(self, url: str) -> None:
        self.url = confidential_endpoint(url)
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _RejectRedirects()
        )

    def open(self, request: urllib.request.Request, *, timeout: float) -> Any:
        if request.full_url != self.url:
            raise InvalidArgumentError(_ENDPOINT_ERROR)
        return self._opener.open(request, timeout=timeout)
