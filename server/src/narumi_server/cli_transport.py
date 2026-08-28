"""Pinned, authenticated local HTTPS without ambient proxies or redirects."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import urllib.parse
import urllib.request
from typing import Any

from narumi.errors import InvalidArgumentError

from narumi_server.secure_transport import ClientTransport, TransportSecurityError

_ENDPOINT_ERROR = "Resident tools require the authenticated numeric-loopback HTTPS endpoint"


def confidential_endpoint(url: str) -> str:
    """Require a numeric loopback address without resolving any caller-supplied hostname."""
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
            parsed.scheme != "https"
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or "%" in parsed.netloc
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ValueError
        address = ipaddress.ip_address(host)
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
    return urllib.parse.urlunsplit(("https", netloc, parsed.path or "/", "", ""))


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Verify the peer certificate before http.client writes any request headers or body."""

    def __init__(self, *args: Any, fingerprint: str, **kwargs: Any) -> None:
        self._fingerprint = fingerprint
        super().__init__(*args, **kwargs)

    def connect(self) -> None:
        super().connect()
        certificate = self.sock.getpeercert(binary_form=True) if self.sock is not None else None
        if certificate is None or not hmac.compare_digest(
            hashlib.sha256(certificate).hexdigest(), self._fingerprint
        ):
            self.close()
            raise TransportSecurityError()


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, credentials: ClientTransport) -> None:
        super().__init__(context=credentials.ssl_context)
        self._fingerprint = credentials.certificate_sha256

    def https_open(self, request: urllib.request.Request) -> Any:
        def connection(*args: Any, **kwargs: Any) -> _PinnedHTTPSConnection:
            return _PinnedHTTPSConnection(*args, fingerprint=self._fingerprint, **kwargs)

        return self.do_open(connection, request, context=self._context)


class ConfidentialHttpTransport:
    """One authenticated endpoint and private opener for the complete MCP session."""

    def __init__(self, credentials: ClientTransport) -> None:
        self.url = confidential_endpoint(credentials.url)
        if self.url != credentials.url:
            raise InvalidArgumentError(_ENDPOINT_ERROR)
        self._client_token = credentials.client_token
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _RejectRedirects(), _PinnedHTTPSHandler(credentials)
        )

    def open(self, request: urllib.request.Request, *, timeout: float) -> Any:
        if request.full_url != self.url:
            raise InvalidArgumentError(_ENDPOINT_ERROR)
        request.add_unredirected_header("Authorization", f"Bearer {self._client_token}")
        return self._opener.open(request, timeout=timeout)
