"""Unit tests for ``MeetingLocks`` (per-meeting manifest write lock)."""

from __future__ import annotations

import threading

import pytest
from narumi.errors import BusyError
from narumi_server.locks import MeetingLocks


def test_hold_is_exclusive_per_meeting_and_reports_the_holder():
    locks = MeetingLocks()
    released = threading.Event()
    entered = threading.Event()

    def job() -> None:
        with locks.hold("m1", purpose="job"):
            entered.set()
            released.wait(5)

    worker = threading.Thread(target=job)
    worker.start()
    assert entered.wait(5)
    assert locks.holder("m1") == "job" and locks.holder("m2") is None
    with pytest.raises(BusyError) as excinfo:
        with locks.hold("m1", purpose="handler", timeout=0.05):
            pass
    assert excinfo.value.details == {"meeting_id": "m1", "holder": "job"}
    with locks.hold("m2", purpose="handler", timeout=0.05):  # another meeting is independent
        assert locks.holder("m2") == "handler"
    released.set()
    worker.join(5)
    assert locks.holder("m1") is None
    with locks.hold("m1", purpose="handler", timeout=0.05):
        pass


def test_hold_waits_for_a_short_holder_and_releases_on_error():
    locks = MeetingLocks()
    done = threading.Event()

    def short() -> None:
        with locks.hold("m1", purpose="short"):
            done.wait(0.2)

    worker = threading.Thread(target=short)
    worker.start()
    with locks.hold("m1", purpose="waiter", timeout=5.0):  # waits ≤ 0.2 s instead of failing
        pass
    worker.join(5)
    with pytest.raises(RuntimeError):
        with locks.hold("m1", purpose="boom"):
            raise RuntimeError("inside")
    assert locks.holder("m1") is None  # released although the body raised
