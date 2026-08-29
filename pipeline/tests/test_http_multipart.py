"""Bounded binary uploads reuse HTTP safety without invoking a real provider."""

from __future__ import annotations

import io
import json
import threading
import time
import traceback
import urllib.error

import pytest
from narumi.errors import AuthenticationRequiredError, CancelledError, EngineUnavailableError
from narumi.providers.metadata import deadline as deadline_module
from narumi.providers.metadata.deadline import DeadlineHTTPSHandler
from narumi.providers.metadata.http import (
    MAX_GENERATION_RESPONSE_BYTES,
    MAX_MULTIPART_REQUEST_BYTES,
    JSONHTTPClient,
)
from narumi.providers.metadata.tls import tls_context
from narumi.providers.metadata.validation import check_public_payload

from .test_provider_metadata_deadline import local_http_server
from .test_provider_metadata_http import KEY, Opener, Response

URL = "https://api.openai.com/v1/audio/transcriptions"
BOUNDARY = "narumi-fixture-boundary"
CONTENT_TYPE = "multipart/form-data; boundary=" + BOUNDARY
PREFIX = (
    f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
    "Content-Type: audio/wav\r\n\r\n"
).encode()
SUFFIX = f"\r\n--{BOUNDARY}--\r\n".encode()
RAW_BODY = PREFIX + b"RIFF\x00\x01fixture\xffWAVE" + SUFFIX
UNKNOWN = {"reason": "provider_generation_outcome_unknown", "outcome_unknown": True}


def upload(client, *, method="POST", url=URL, headers=None, raw_body=RAW_BODY, **options):
    selected = {
        "headers": {"Authorization": "Bearer " + KEY, "Content-Type": CONTENT_TYPE}
        if headers is None
        else headers,
        "raw_body": raw_body,
        "response_kind": "transcription",
    }
    selected.update(options)
    return client.request(method, url, **selected)


@pytest.mark.parametrize("supplied_length", [False, True])
def test_multipart_bytes_are_not_json_encoded_and_length_is_exact(supplied_length):
    headers = {"Content-Type": CONTENT_TYPE, "Authorization": "Bearer " + KEY}
    if supplied_length:
        headers["content-length"] = str(len(RAW_BODY))
    opener = Opener(Response(b'{"text":"fixture"}', url=URL))
    assert upload(JSONHTTPClient(opener=opener), headers=headers) == {"text": "fixture"}
    request = opener.calls[0][0]
    assert request.data is RAW_BODY
    assert request.get_header("Content-type") == CONTENT_TYPE
    assert request.get_header("Content-length") == str(len(RAW_BODY))
    assert request.get_header("Transfer-encoding") is None
    assert request.get_header("Authorization") == "Bearer " + KEY


@pytest.mark.parametrize(
    "options",
    [
        {"payload": {}},
        {"raw_body": None},
        {"raw_body": b""},
        {"raw_body": "audio"},
        {"raw_body": bytearray(RAW_BODY)},
        {"raw_body": RAW_BODY[:-2]},
        {"method": "GET"},
        {"method": "PUT"},
        {"response_kind": "metadata"},
        {"response_kind": "generation"},
        {"headers": {}},
        {"headers": {"Content-Type": "application/json"}},
        {"headers": {"Content-Type": "multi-part/form-data; boundary=" + BOUNDARY}},
        {"headers": {"Content-Type": 'multipart/form-data; boundary="' + BOUNDARY + '"'}},
        {"headers": {"Content-Type": "multipart/form-data; boundary=" + "x" * 71}},
        {"headers": {"Content-Type": "multipart/form-data; boundary=wrong-boundary"}},
        {"headers": {"Content-Type": CONTENT_TYPE + "\r\n" + KEY}},
        {"headers": {"Content-Type": CONTENT_TYPE, "content-type": CONTENT_TYPE}},
        {"headers": {"Content-Type": CONTENT_TYPE, "Transfer-Encoding": "chunked"}},
        {"headers": {"Content-Type": CONTENT_TYPE, "transfer-encoding": ""}},
        {"headers": {"Content-Type": CONTENT_TYPE, "Content-Length": "0"}},
        {"headers": {"Content-Type": CONTENT_TYPE, "Content-Length": True}},
        {
            "headers": {
                "Content-Type": CONTENT_TYPE,
                "Content-Length": str(len(RAW_BODY)),
                "content-length": str(len(RAW_BODY)),
            }
        },
        {
            "headers": {
                "Content-Type": CONTENT_TYPE,
                "Authorization": KEY,
                "authorization": KEY,
            }
        },
        {"headers": {"Content-Type": CONTENT_TYPE, "Bad\nHeader": KEY}},
        {"headers": {"Content-Type": CONTENT_TYPE, "Authorization": "Bearer " + KEY + "\n"}},
    ],
)
def test_invalid_multipart_options_are_rejected_before_open(options):
    opener = Opener()
    with pytest.raises(EngineUnavailableError) as failure:
        upload(JSONHTTPClient(opener=opener), **options)
    assert failure.value.details == {"reason": "invalid_http_options"}
    assert not opener.calls
    assert KEY not in "".join(traceback.format_exception(failure.value))


@pytest.mark.parametrize("size", [MAX_MULTIPART_REQUEST_BYTES, MAX_MULTIPART_REQUEST_BYTES + 1])
def test_multipart_request_byte_limit(size):
    body = PREFIX + b"a" * (size - len(PREFIX) - len(SUFFIX)) + SUFFIX
    opener = Opener(Response(url=URL))
    if size > MAX_MULTIPART_REQUEST_BYTES:
        with pytest.raises(EngineUnavailableError) as failure:
            upload(JSONHTTPClient(opener=opener), raw_body=body)
        assert failure.value.details == {"reason": "invalid_http_options"}
        assert not opener.calls
    else:
        assert upload(JSONHTTPClient(opener=opener), raw_body=body) == {}
        assert opener.calls[0][0].get_header("Content-length") == str(size)


@pytest.mark.parametrize("status", [400, 401, 403, 413, 429, 408, 409, 404, 422, 500, 502, 503])
def test_transcription_only_treats_explicit_refusal_statuses_as_known(status):
    class UnreadableBody(io.BytesIO):
        def read(self, *args):
            pytest.fail("upstream error body must never be read")

    body = UnreadableBody(KEY.encode())
    opener = Opener(urllib.error.HTTPError(URL, status, KEY, {}, body), Response(url=URL))
    error_type = AuthenticationRequiredError if status in {401, 403} else EngineUnavailableError
    with pytest.raises(error_type) as failure:
        upload(JSONHTTPClient(opener=opener))
    if status in {401, 403}:
        assert failure.value.details == {"reason": "credential_rejected"}
    elif status in {400, 413, 429}:
        assert failure.value.details == {"reason": "metadata_http_error", "status": status}
    else:
        assert failure.value.details == UNKNOWN
    assert body.closed and len(opener.calls) == 1 and len(opener.responses) == 1
    assert KEY not in "".join(traceback.format_exception(failure.value))


@pytest.mark.parametrize("started", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
def test_transcription_uses_real_or_injected_http_send_boundary(started, cancel):
    cancelled = threading.Event()

    class TrackedOpener:
        handlers = [DeadlineHTTPSHandler(context=tls_context())]
        calls = 0

        def open(self, request, **kwargs):
            self.calls += 1
            if started:
                request.narumi_deadline.mark_request_started()
            if cancel:
                cancelled.set()
            raise TimeoutError(KEY)

    opener = TrackedOpener()
    with pytest.raises(CancelledError if cancel else EngineUnavailableError) as failure:
        upload(JSONHTTPClient(opener=opener), should_cancel=cancelled.is_set)
    assert failure.value.details == (
        UNKNOWN
        if started
        else {"reason": "provider_generation_cancelled" if cancel else "metadata_connection_failed"}
    )
    assert opener.calls == 1
    assert KEY not in "".join(traceback.format_exception(failure.value))


def test_already_cancelled_audio_upload_never_opens_transport():
    opener = Opener()
    with pytest.raises(CancelledError) as failure:
        upload(JSONHTTPClient(opener=opener), should_cancel=lambda: True)
    assert failure.value.details == {"reason": "provider_generation_cancelled"}
    assert not opener.calls


@pytest.mark.parametrize("field", ["text", "context", "unused"])
@pytest.mark.parametrize("full_header", [False, True])
def test_transcription_checks_secrets_in_all_fields_before_any_discard(field, full_header):
    reflected = "Bearer " + KEY if full_header else KEY
    body = json.dumps({field: {"nested": [reflected]}}).replace("f", "\\u0066").encode()
    opener = Opener(Response(body, url=URL))
    with pytest.raises(EngineUnavailableError) as failure:
        upload(JSONHTTPClient(opener=opener))
    assert failure.value.details == UNKNOWN
    assert KEY not in "".join(traceback.format_exception(failure.value))


def test_word_timestamp_response_can_exceed_metadata_node_limit_without_dropping_fields():
    body = {
        "text": "fixture words",
        "context": {"note": "preserved"},
        "words": [{"word": "fixture", "start": n / 5, "end": (n + 1) / 5} for n in range(3000)],
    }
    with pytest.raises(EngineUnavailableError) as failure:
        check_public_payload(body)
    assert failure.value.details == {"reason": "metadata_structure_limit"}
    assert (
        upload(JSONHTTPClient(opener=Opener(Response(json.dumps(body).encode(), url=URL)))) == body
    )


@pytest.mark.parametrize("max_nodes", [None, True, False, 0, -1, 1.5, "200000", 200_001])
def test_payload_node_limit_rejects_invalid_or_unbounded_overrides(max_nodes):
    with pytest.raises(EngineUnavailableError):
        check_public_payload({}, max_nodes=max_nodes)


def test_payload_node_limit_accepts_exact_boundaries():
    check_public_payload({}, max_nodes=1)
    check_public_payload({"context": [None] * 199_997}, max_nodes=200_000)
    with pytest.raises(EngineUnavailableError) as failure:
        check_public_payload({"context": [None] * 199_998}, max_nodes=200_000)
    assert failure.value.details == {"reason": "metadata_structure_limit"}


def test_normalized_tuple_results_reject_secret_reflections_and_enforce_node_budget():
    with pytest.raises(EngineUnavailableError) as failure:
        check_public_payload({"segments": ({"text": KEY},)}, secrets=(KEY,))
    assert failure.value.details == {"reason": "unsafe_metadata"}
    with pytest.raises(EngineUnavailableError) as failure:
        check_public_payload({"words": (None,) * 200_000}, max_nodes=200_000)
    assert failure.value.details == {"reason": "metadata_structure_limit"}
    check_public_payload({"words": (None,) * 199_997}, max_nodes=200_000)


def test_normalized_tuple_results_retain_the_depth_bound():
    value = 0
    for _ in range(33):
        value = (value,)
    with pytest.raises(EngineUnavailableError) as failure:
        check_public_payload({"segments": value}, max_nodes=200_000)
    assert failure.value.details == {"reason": "metadata_structure_limit"}


@pytest.mark.parametrize("kind", ["nodes", "depth", "duplicate", "nonobject", "nonfinite"])
def test_audio_response_remains_bounded_and_strict(kind):
    bodies = {
        "nodes": lambda: json.dumps({"context": [None] * 200_000}).encode(),
        "depth": lambda: b'{"context":' + b"[" * 33 + b"0" + b"]" * 33 + b"}",
        "duplicate": lambda: b'{"text":"a","text":"b"}',
        "nonobject": lambda: b'[{"text":"fixture"}]',
        "nonfinite": lambda: b'{"start":NaN}',
    }
    with pytest.raises(EngineUnavailableError) as failure:
        upload(JSONHTTPClient(opener=Opener(Response(bodies[kind](), url=URL))))
    assert failure.value.details == UNKNOWN


@pytest.mark.parametrize("declared", [True, False])
def test_audio_response_never_exceeds_eight_mebibytes(declared):
    headers = {"Content-Length": str(MAX_GENERATION_RESPONSE_BYTES + 1)} if declared else {}
    response = Response(b"x" * (MAX_GENERATION_RESPONSE_BYTES + 1), url=URL, headers=headers)
    with pytest.raises(EngineUnavailableError) as failure:
        upload(JSONHTTPClient(opener=Opener(response)))
    assert failure.value.details == UNKNOWN
    assert response.read_sizes == ([] if declared else [MAX_GENERATION_RESPONSE_BYTES + 1])


def test_real_loopback_receives_exact_multipart_bytes_once():
    def reply(connection):
        connection.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"
        )

    with local_http_server(reply) as (url, requests):
        assert upload(JSONHTTPClient(), url=url) == {}
        assert len(requests) == 1
        head, body = requests[0]
        assert body == RAW_BODY
        assert f"content-length: {len(RAW_BODY)}\r\n".encode() in head.lower() + b"\r\n"
        assert ("content-type: " + CONTENT_TYPE).encode() in head.lower()
        assert b"transfer-encoding:" not in head.lower()


@pytest.mark.parametrize("status", [408, 409, 500])
def test_real_audio_post_unknown_status_never_retries(status):
    def reply(connection):
        connection.sendall(f"HTTP/1.1 {status} Failure\r\nContent-Length: 0\r\n\r\n".encode())

    with local_http_server(reply) as (url, requests):
        with pytest.raises(EngineUnavailableError) as failure:
            upload(JSONHTTPClient(), url=url)
        assert failure.value.details == UNKNOWN
        assert len(requests) == 1


def test_real_audio_post_incomplete_content_length_is_unknown():
    def reply(connection):
        connection.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 52\r\n\r\n{}"
        )

    with local_http_server(reply) as (url, requests):
        with pytest.raises(EngineUnavailableError) as failure:
            upload(JSONHTTPClient(), url=url)
        assert failure.value.details == UNKNOWN and len(requests) == 1


def test_cancelling_a_blocked_binary_upload_closes_it_without_a_retry(monkeypatch):
    cancelled, release_peer, body_finished = threading.Event(), threading.Event(), threading.Event()
    cancelled_at, observed_blocked = [], []
    raw_body = PREFIX + b"x" * (8 * 1024 * 1024) + SUFFIX
    original_send = deadline_module._TrackedSend.send

    def send(connection, data):
        try:
            return original_send(connection, data)
        finally:
            if data is raw_body:
                body_finished.set()

    def cancel():
        observed_blocked.append(not body_finished.is_set())
        cancelled_at.append(time.monotonic())
        cancelled.set()

    def reply(connection):
        timer = threading.Timer(0.05, cancel)
        timer.start()
        try:
            release_peer.wait(timeout=3)
        finally:
            timer.cancel()

    monkeypatch.setattr(deadline_module._TrackedSend, "send", send)
    with local_http_server(reply, read_body=False) as (url, requests):
        try:
            with pytest.raises(CancelledError) as failure:
                upload(
                    JSONHTTPClient(),
                    url=url,
                    raw_body=raw_body,
                    timeout=3,
                    should_cancel=cancelled.is_set,
                )
            assert time.monotonic() - cancelled_at[0] < 0.5
            assert failure.value.details == UNKNOWN
            assert observed_blocked == [True] and body_finished.is_set()
            assert len(requests) == 1 and len(requests[0][1]) < len(raw_body)
        finally:
            release_peer.set()
