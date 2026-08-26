from pathlib import Path

import pytest
from narumi.bundle import Bundle, new_meeting_id
from narumi.bundle.session import MEETING_ID_RE
from narumi.errors import InvalidArgumentError, NotFoundError


def test_meeting_id_format():
    mid = new_meeting_id()
    assert MEETING_ID_RE.match(mid)


def test_create_open_roundtrip(tmp_path: Path):
    b = Bundle.create(tmp_path, meeting_name="定例", scope="cloudnative")
    assert (b.path / "manifest.json").exists()
    for sub in ("tracks", "transcripts", "minutes"):
        assert (b.path / sub).is_dir()
    reopened = Bundle.open(b.path)
    assert reopened.manifest.meeting_name == "定例"
    assert reopened.manifest.scope == "cloudnative"
    assert reopened.manifest.status == "recording"


def test_open_missing_raises(tmp_path: Path):
    with pytest.raises(NotFoundError):
        Bundle.open(tmp_path / "nope")
    with pytest.raises(InvalidArgumentError):
        Bundle.find(tmp_path, "bad-id")


def test_run_stage_is_idempotent(tmp_path: Path):
    b = Bundle.create(tmp_path, meeting_name="x")
    calls: list[int] = []

    def produce(out: Path) -> None:
        calls.append(1)
        out.write_text("hello", encoding="utf-8")

    r1 = b.run_stage(
        "demo/out",
        inputs={"src": "abc"},
        params={"a": 1},
        producer=("demo", "1"),
        output="preprocess/out.txt",
        fn=produce,
    )
    assert not r1.skipped and len(calls) == 1
    r2 = b.run_stage(
        "demo/out",
        inputs={"src": "abc"},
        params={"a": 1},
        producer=("demo", "1"),
        output="preprocess/out.txt",
        fn=produce,
    )
    assert r2.skipped and len(calls) == 1
    # changed params → re-run
    r3 = b.run_stage(
        "demo/out",
        inputs={"src": "abc"},
        params={"a": 2},
        producer=("demo", "1"),
        output="preprocess/out.txt",
        fn=produce,
    )
    assert not r3.skipped and len(calls) == 2
    # changed input hash → re-run
    b.run_stage(
        "demo/out",
        inputs={"src": "zzz"},
        params={"a": 2},
        producer=("demo", "1"),
        output="preprocess/out.txt",
        fn=produce,
    )
    assert len(calls) == 3
    # force → re-run
    b.run_stage(
        "demo/out",
        inputs={"src": "zzz"},
        params={"a": 2},
        producer=("demo", "1"),
        output="preprocess/out.txt",
        fn=produce,
        force=True,
    )
    assert len(calls) == 4
    # manifest persisted the record
    reopened = Bundle.open(b.path)
    assert reopened.artifact("demo/out") is not None
    assert reopened.artifact("demo/out").params == {"a": 2}


def test_run_stage_requires_output(tmp_path: Path):
    b = Bundle.create(tmp_path, meeting_name="x")
    with pytest.raises(InvalidArgumentError):
        b.run_stage(
            "demo/none",
            inputs={},
            params={},
            producer=("demo", "1"),
            output="preprocess/never.txt",
            fn=lambda p: None,
        )


def test_minutes_versions(tmp_path: Path):
    b = Bundle.create(tmp_path, meeting_name="x")
    assert b.next_minutes_version() == 1
    assert b.minutes_dir(1).is_dir()
