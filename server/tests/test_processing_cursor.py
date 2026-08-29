from __future__ import annotations

import pytest
from narumi.errors import InvalidArgumentError
from narumi_server.processing_cursor import (
    decode_processing_runs_cursor,
    encode_processing_runs_cursor,
)

MEETING_ID = "20260829T010203Z-a1b2c3d4"
ORDER_KEY = ("2026-08-29T01:02:03Z", "run-0123456789abcdef0123456789abcdef")


def test_cursor_round_trip_is_bounded_url_safe_and_none_stays_none():
    assert encode_processing_runs_cursor(None, meeting_id=MEETING_ID, scope=None) is None
    assert decode_processing_runs_cursor(None, meeting_id=MEETING_ID, scope=None) is None
    cursor = encode_processing_runs_cursor(
        ORDER_KEY, meeting_id=MEETING_ID, scope=["beta", "alpha"]
    )
    assert cursor is not None and 1 <= len(cursor) <= 256
    assert set(cursor) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
    assert (
        decode_processing_runs_cursor(cursor, meeting_id=MEETING_ID, scope=["alpha", "beta"])
        == ORDER_KEY
    )


def test_single_scope_and_one_item_array_share_the_same_query_binding():
    cursor = encode_processing_runs_cursor(ORDER_KEY, meeting_id=MEETING_ID, scope="開発")
    assert decode_processing_runs_cursor(cursor, meeting_id=MEETING_ID, scope=["開発"]) == ORDER_KEY


@pytest.mark.parametrize(
    ("meeting_id", "scope"),
    [
        ("20260829T010203Z-deadbeef", ["alpha", "beta"]),
        (MEETING_ID, ["alpha"]),
        (MEETING_ID, None),
    ],
)
def test_cursor_is_bound_to_the_authorized_meeting_and_normalized_scope(meeting_id, scope):
    cursor = encode_processing_runs_cursor(
        ORDER_KEY, meeting_id=MEETING_ID, scope=["alpha", "beta"]
    )
    with pytest.raises(InvalidArgumentError):
        decode_processing_runs_cursor(cursor, meeting_id=meeting_id, scope=scope)


@pytest.mark.parametrize("mutation", ["truncate", "replace", "append", "invalid"])
def test_malformed_or_modified_cursor_is_rejected(mutation):
    cursor = encode_processing_runs_cursor(ORDER_KEY, meeting_id=MEETING_ID, scope=None)
    assert cursor is not None
    changed = {
        "truncate": cursor[:-1],
        "replace": ("A" if cursor[0] != "A" else "B") + cursor[1:],
        "append": cursor + "A",
        "invalid": cursor[:-1] + "=",
    }[mutation]
    with pytest.raises(InvalidArgumentError):
        decode_processing_runs_cursor(changed, meeting_id=MEETING_ID, scope=None)


@pytest.mark.parametrize(
    "key",
    [
        ("2026-08-29T01:02:03+00:00", ORDER_KEY[1]),
        (ORDER_KEY[0], "run-not-hex"),
        (ORDER_KEY[0], "artifact-0123456789abcdef0123456789abcdef"),
    ],
)
def test_encoder_rejects_noncanonical_order_keys(key):
    with pytest.raises(InvalidArgumentError):
        encode_processing_runs_cursor(key, meeting_id=MEETING_ID, scope=None)
