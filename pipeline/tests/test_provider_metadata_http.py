"""Security checks run against an injected HTTP opener, never a real provider."""

from __future__ import annotations

import http.client
import io
import json
import ssl
import traceback
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest
from narumi.errors import (
    AuthenticationRequiredError,
    CancelledError,
    EngineUnavailableError,
    InvalidArgumentError,
)
from narumi.providers.metadata import MetadataClient
from narumi.providers.metadata.deadline import DeadlineHTTPSHandler
from narumi.providers.metadata.http import MAX_RESPONSE_BYTES, JSONHTTPClient, RejectRedirects
from narumi.providers.metadata.tls import tls_context

ORIGIN = "https://api.anthropic.com"
URL = ORIGIN + "/v1/models?limit=100"
KEY = "fixture-private-api-key-not-real"


class Response(io.BytesIO):
    def __init__(self, body=b"{}", *, url=URL, status=200, headers=None):
        super().__init__(body)
        self.url, self.status = url, status
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.read_sizes = []

    def geturl(self):
        return self.url

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)

    def read1(self, size=-1):
        return self.read(size)


class Opener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def open(self, request, *, timeout):
        self.calls.append((request, timeout))
        assert self.responses, "unexpected HTTP request"
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def request_response(response, *, headers=None, **options):
    opener = Opener(response)
    return JSONHTTPClient(opener=opener).request("GET", URL, headers=headers, **options)


def test_metadata_http_has_no_proxy_or_redirect_handler_inheritance(monkeypatch):
    calls = []
    real_builder = urllib.request.build_opener

    def build(*handlers):
        calls.extend(handlers)
        return real_builder(*handlers)

    def forbidden(*args, **kwargs):
        raise AssertionError("ambient proxies or global urlopen were used")

    monkeypatch.setenv("HTTPS_PROXY", "http://private-proxy.invalid:8888")
    monkeypatch.setenv("HTTP_PROXY", "http://private-proxy.invalid:8888")
    monkeypatch.setenv("ALL_PROXY", "http://private-proxy.invalid:8888")
    monkeypatch.setattr(urllib.request, "getproxies", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(urllib.request, "build_opener", build)
    client = JSONHTTPClient()
    assert next(handler for handler in calls if isinstance(handler, urllib.request.ProxyHandler))
    assert calls[0].proxies == {}
    assert any(isinstance(handler, RejectRedirects) for handler in client._opener.handlers)


def test_tls_never_enables_environment_session_key_logging(monkeypatch, tmp_path):
    keylog = tmp_path / "fixture-keylog"
    monkeypatch.setenv("SSLKEYLOGFILE", str(keylog))
    assignments = []
    original = ssl.SSLContext.keylog_filename
    monkeypatch.setattr(
        ssl.SSLContext,
        "keylog_filename",
        property(
            lambda context: original.__get__(context),
            lambda context, value: assignments.append(value),
        ),
    )
    client = JSONHTTPClient()
    context = next(
        handler._context
        for handler in client._opener.handlers
        if isinstance(handler, urllib.request.HTTPSHandler)
    )
    assert context.keylog_filename is None
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert not assignments and not keylog.exists()


def test_tls_uses_installation_trust_roots_without_environment_overrides(monkeypatch, tmp_path):
    platform_ca = tmp_path / "platform-ca.pem"
    platform_ca.write_text("fixture trust location")
    untrusted_ca = tmp_path / "ambient-ca.pem"
    monkeypatch.setenv("SSL_CERT_FILE", str(untrusted_ca))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "ambient-dir"))
    monkeypatch.setattr(
        ssl,
        "get_default_verify_paths",
        lambda: SimpleNamespace(
            openssl_cafile=str(platform_ca), openssl_capath=str(tmp_path / "absent")
        ),
    )
    loads = []
    monkeypatch.setattr(
        ssl.SSLContext, "load_verify_locations", lambda context, **kwargs: loads.append(kwargs)
    )
    tls_context()
    assert loads == [{"cafile": str(platform_ca), "capath": None}]


def test_missing_installation_trust_roots_do_not_enable_an_unverified_tls_context(monkeypatch):
    monkeypatch.setattr(
        ssl,
        "get_default_verify_paths",
        lambda: SimpleNamespace(openssl_cafile=None, openssl_capath=""),
    )
    context = tls_context()
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert context.cert_store_stats()["x509_ca"] == 0


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirects_never_forward_credentials(status):
    request = urllib.request.Request(URL, headers={"x-api-key": KEY})
    with pytest.raises(EngineUnavailableError) as failure:
        RejectRedirects().redirect_request(
            request, None, status, KEY, {}, "https://external.invalid/" + KEY
        )
    assert failure.value.details["reason"] == "redirect_rejected"
    assert KEY not in str(failure.value.to_payload())


def test_redirect_cleanup_failure_never_exposes_upstream_exception():
    class FailingClose:
        attempted = False

        def close(self):
            self.attempted = True
            raise RuntimeError(KEY)

    response = FailingClose()
    with pytest.raises(EngineUnavailableError) as failure:
        RejectRedirects().redirect_request(
            urllib.request.Request(URL), response, 307, KEY, {}, "https://external.invalid/"
        )
    assert response.attempted
    assert failure.value.details == {"reason": "redirect_rejected"}
    assert failure.value.__suppress_context__
    assert KEY not in "".join(traceback.format_exception(failure.value))


def test_secret_is_sent_only_in_explicit_header_and_not_error_or_model_result(monkeypatch):
    response = Response(
        json.dumps(
            {
                "data": [
                    {
                        "id": "fixture-model",
                        "display_name": "Fixture model",
                        "type": "model",
                        "capabilities": {"image_input": {"supported": False}},
                    }
                ],
                "has_more": False,
            }
        ).encode()
    )
    opener = Opener(response)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-wrong-key")
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "Authorization: fixture-wrong-header")
    models = MetadataClient(http=JSONHTTPClient(opener=opener)).fetch("anthropic-api", ORIGIN, KEY)
    request, timeout = opener.calls[0]
    assert request.get_method() == "GET"
    assert request.data is None and KEY not in request.full_url
    assert request.get_header("X-api-key") == KEY
    assert request.get_header("Authorization") is None
    assert request.get_header("Cookie") is None
    assert request.get_header("Anthropic-version") == "2023-06-01"
    assert timeout <= 10
    assert KEY not in str(models)
    assert response.read_sizes[0] == MAX_RESPONSE_BYTES + 1
    assert max(response.read_sizes) <= MAX_RESPONSE_BYTES + 1


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_http_errors_do_not_expose_provider_payload_or_exception_text(status):
    failure_type = AuthenticationRequiredError if status in {401, 403} else EngineUnavailableError
    error = urllib.error.HTTPError(
        URL + KEY, status, KEY, {"x-api-key": KEY}, io.BytesIO(KEY.encode())
    )
    with pytest.raises(failure_type) as failure:
        request_response(error, headers={"x-api-key": KEY})
    assert KEY not in str(failure.value.to_payload())
    assert failure.value.__suppress_context__ is True
    assert error.fp.closed


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError(KEY),
        TimeoutError(KEY),
        ConnectionError(KEY),
        ValueError(KEY),
        http.client.BadStatusLine(KEY),
        http.client.IncompleteRead(KEY.encode()),
    ],
)
def test_connection_failures_have_fixed_public_errors(error):
    with pytest.raises(EngineUnavailableError) as failure:
        request_response(error)
    assert KEY not in str(failure.value.to_payload())


@pytest.mark.parametrize(
    "body",
    [
        b'{"unexpected": NaN}',
        b'{"unexpected": Infinity}',
        b'{"unexpected": 1e9999}',
        b'{"same": 1, "same": 2}',
        b"[]",
        b"not json",
        b"\xff",
        b"[" * 1200 + b"]" * 1200,
    ],
)
def test_invalid_json_is_rejected_without_returning_partial_metadata(body):
    with pytest.raises(EngineUnavailableError):
        request_response(Response(body))


def test_declared_oversized_response_is_rejected_before_read():
    response = Response(headers={"Content-Length": str(MAX_RESPONSE_BYTES + 1)})
    with pytest.raises(EngineUnavailableError) as failure:
        request_response(response)
    assert failure.value.details["reason"] == "metadata_size_limit"
    assert not response.read_sizes


def test_undeclared_oversized_response_reads_only_the_limit():
    response = Response(b" " * (MAX_RESPONSE_BYTES + 20))
    with pytest.raises(EngineUnavailableError) as failure:
        request_response(response)
    assert failure.value.details["reason"] == "metadata_size_limit"
    assert response.read_sizes == [MAX_RESPONSE_BYTES + 1]


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Type": "text/html"},
        {"Content-Encoding": "gzip"},
        {"Content-Length": "invalid"},
        {"Content-Length": "-1"},
    ],
)
def test_unexpected_response_headers_fail_closed(headers):
    with pytest.raises(EngineUnavailableError):
        request_response(Response(headers=headers))


def test_json_secret_reflections_are_rejected_even_when_escaped_or_in_unused_fields():
    body = ('{"unused":"\\u0066' + KEY[1:] + '"}').encode()
    with pytest.raises(EngineUnavailableError) as failure:
        request_response(Response(body), headers={"x-api-key": KEY})
    assert failure.value.details["reason"] == "unsafe_metadata"
    assert KEY not in str(failure.value.to_payload())


def test_excessive_json_structure_is_rejected_within_the_byte_limit():
    body = json.dumps({"unused": [None] * 20_001}).encode()
    assert len(body) < MAX_RESPONSE_BYTES
    with pytest.raises(EngineUnavailableError) as failure:
        request_response(Response(body))
    assert failure.value.details["reason"] == "metadata_structure_limit"


def test_generation_discards_ollama_context_without_metadata_structure_limits():
    body = json.dumps({"context": list(range(25_000)), "response": "Fixture summary"}).encode()
    opener = Opener(Response(body))
    result = JSONHTTPClient(opener=opener).request("POST", URL, response_kind="generation")
    assert result == {"response": "Fixture summary"}


def test_response_url_mismatch_never_returns_redirected_metadata():
    with pytest.raises(EngineUnavailableError) as failure:
        request_response(Response(url="https://external.invalid/"))
    assert failure.value.details["reason"] == "redirect_rejected"


@pytest.mark.parametrize("api_key", ["bad\r\nheader", "has space", "非ascii", "a" * 4097])
def test_invalid_credential_never_reaches_http(api_key):
    opener = Opener()
    client = MetadataClient(http=JSONHTTPClient(opener=opener))
    with pytest.raises(InvalidArgumentError) as failure:
        client.fetch("anthropic-api", ORIGIN, api_key)
    assert api_key not in str(failure.value.to_payload())
    assert not opener.calls


def test_local_metadata_does_not_accept_an_api_key():
    opener = Opener()
    with pytest.raises(InvalidArgumentError):
        MetadataClient(http=JSONHTTPClient(opener=opener)).fetch(
            "ollama", "http://127.0.0.1:11434", KEY
        )
    assert not opener.calls


@pytest.mark.parametrize("status", [400, 401, 403, 404, 405, 413, 415, 422])
@pytest.mark.parametrize("raised", [True, False])
def test_generation_http_refusal_is_known_and_never_reads_error_body(status, raised):
    class ForbiddenBody(Response):
        def read(self, size=-1):
            pytest.fail("HTTP error body was read")

    body = ForbiddenBody(KEY.encode(), status=status)
    response = urllib.error.HTTPError(URL, status, KEY, {}, body) if raised else body
    error_type = AuthenticationRequiredError if status in {401, 403} else EngineUnavailableError
    opener = Opener(response, Response())
    with pytest.raises(error_type) as failure:
        JSONHTTPClient(opener=opener).request("POST", URL, response_kind="generation")
    assert failure.value.details == (
        {"reason": "credential_rejected"}
        if status in {401, 403}
        else {"reason": "metadata_http_error", "status": status}
    )
    assert len(opener.calls) == 1 and len(opener.responses) == 1
    assert body.closed
    assert KEY not in "".join(traceback.format_exception(failure.value))
    assert failure.value.__suppress_context__


@pytest.mark.parametrize(
    "status",
    [301, 302, 307, 308, 402, 408, 409, 410, 425, 429, 451, 460, 499, 500, 502, 503],
)
@pytest.mark.parametrize("raised", [True, False])
def test_generation_http_uncertain_outcome_never_retries(status, raised):
    response = (
        urllib.error.HTTPError(URL, status, KEY, {}, io.BytesIO(KEY.encode()))
        if raised
        else Response(KEY.encode(), status=status)
    )
    opener = Opener(response, Response())
    with pytest.raises(EngineUnavailableError) as failure:
        JSONHTTPClient(opener=opener).request("POST", URL, response_kind="generation")
    assert failure.value.details == {
        "reason": "provider_generation_outcome_unknown",
        "outcome_unknown": True,
    }
    assert len(opener.calls) == 1 and len(opener.responses) == 1
    assert KEY not in "".join(traceback.format_exception(failure.value))


@pytest.mark.parametrize(
    "body,headers,response_url",
    [
        (b"not json", {}, URL),
        (b"[]", {}, URL),
        (b'{"value":NaN}', {}, URL),
        (b'{"value":1,"value":2}', {}, URL),
        (b"{}", {"Content-Type": "text/plain"}, URL),
        (b"{}", {"Content-Encoding": "gzip"}, URL),
        (b"{}", {"Content-Length": "invalid"}, URL),
        (b"{}", {"Content-Length": "8388609"}, URL),
        (b"{}", {}, "https://external.invalid/"),
        (json.dumps({"unused": KEY}).encode(), {}, URL),
        (json.dumps({"unused": [None] * 20_001}).encode(), {}, URL),
    ],
)
def test_generation_invalid_response_is_unknown(body, headers, response_url):
    response = Response(body, headers=headers, url=response_url)
    opener = Opener(response, Response())
    with pytest.raises(EngineUnavailableError) as failure:
        JSONHTTPClient(opener=opener).request(
            "POST", URL, headers={"Authorization": "Bearer " + KEY}, response_kind="generation"
        )
    assert failure.value.details == {
        "reason": "provider_generation_outcome_unknown",
        "outcome_unknown": True,
    }
    assert len(opener.calls) == 1 and len(opener.responses) == 1
    assert response.closed and failure.value.__suppress_context__
    assert KEY not in "".join(traceback.format_exception(failure.value))


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError(KEY),
        urllib.error.URLError(KEY),
        ConnectionError(KEY),
        http.client.BadStatusLine(KEY),
        http.client.IncompleteRead(KEY.encode()),
        RuntimeError(KEY),
        EngineUnavailableError(KEY, details={"reason": KEY}),
    ],
)
def test_generation_injected_opener_failure_is_conservatively_unknown(error):
    opener = Opener(error, Response())
    with pytest.raises(EngineUnavailableError) as failure:
        JSONHTTPClient(opener=opener).request("POST", URL, response_kind="generation")
    assert failure.value.details == {
        "reason": "provider_generation_outcome_unknown",
        "outcome_unknown": True,
    }
    assert len(opener.calls) == 1 and len(opener.responses) == 1
    assert KEY not in "".join(traceback.format_exception(failure.value))


@pytest.mark.parametrize("started", [False, True])
def test_explicit_opener_write_boundary_distinguishes_pre_send_failure(started):
    class TrackedOpener:
        handlers = [DeadlineHTTPSHandler(context=tls_context())]

        def open(self, request, *, timeout):
            if started:
                request.narumi_deadline.mark_request_started()
            raise TimeoutError(KEY)

    with pytest.raises(EngineUnavailableError) as failure:
        JSONHTTPClient(opener=TrackedOpener()).request("POST", URL, response_kind="generation")
    assert failure.value.details == (
        {"reason": "provider_generation_outcome_unknown", "outcome_unknown": True}
        if started
        else {"reason": "metadata_connection_failed"}
    )
    assert KEY not in "".join(traceback.format_exception(failure.value))


@pytest.mark.parametrize("response_kind", ["metadata", "generation"])
@pytest.mark.parametrize("authorization", ["Bearer ", "bearer ", "BEARER "])
@pytest.mark.parametrize("full_header", [True, False])
def test_bearer_key_reflection_is_rejected_without_requiring_the_scheme(
    response_kind, authorization, full_header
):
    header = authorization + KEY
    reflected = header if full_header else KEY
    body = json.dumps({"unused": reflected}).replace("f", "\\u0066").encode()
    with pytest.raises(EngineUnavailableError) as failure:
        request_response(
            Response(body), headers={"Authorization": header}, response_kind=response_kind
        )
    assert failure.value.details["reason"] == (
        "unsafe_metadata" if response_kind == "metadata" else "provider_generation_outcome_unknown"
    )
    assert KEY not in "".join(traceback.format_exception(failure.value))


def test_generation_cancelled_before_open_does_not_transmit():
    opener = Opener()
    with pytest.raises(CancelledError) as failure:
        JSONHTTPClient(opener=opener).request(
            "POST", URL, response_kind="generation", should_cancel=lambda: True
        )
    assert not opener.calls
    assert failure.value.details == {"reason": "provider_generation_cancelled"}


@pytest.mark.parametrize("response_kind", ["metadata", "generation"])
def test_cancelled_after_open_records_unknown_only_for_generation(response_kind):
    cancelled = False

    class CancellingOpener(Opener):
        def open(self, request, *, timeout):
            nonlocal cancelled
            response = super().open(request, timeout=timeout)
            cancelled = True
            return response

    response = Response()
    opener = CancellingOpener(response, Response())
    with pytest.raises(CancelledError) as failure:
        JSONHTTPClient(opener=opener).request(
            "POST", URL, response_kind=response_kind, should_cancel=lambda: cancelled
        )
    assert failure.value.details == (
        {"reason": "provider_generation_outcome_unknown", "outcome_unknown": True}
        if response_kind == "generation"
        else {"reason": "provider_generation_cancelled"}
    )
    assert len(opener.calls) == 1 and response.closed


def test_cancel_callback_failure_stops_without_exposing_exception_or_transmitting():
    def broken_callback():
        raise RuntimeError(KEY)

    opener = Opener()
    with pytest.raises(CancelledError) as failure:
        JSONHTTPClient(opener=opener).request(
            "POST", URL, response_kind="generation", should_cancel=broken_callback
        )
    assert not opener.calls
    assert failure.value.details == {"reason": "provider_generation_cancelled"}
    assert KEY not in "".join(traceback.format_exception(failure.value))
