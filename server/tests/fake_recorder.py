#!/usr/bin/env python3
"""Fake ``narumi-recorder`` for tests: speaks the recorder JSON-Lines protocol without capturing.

    python3 fake_recorder.py record --output DIR [--no-video] [--display N] [--mic UID]
    python3 fake_recorder.py check

``record`` writes ``DIR/mic.wav`` and ``DIR/system.wav`` (1 s of 16 kHz mono silence) plus an
empty ``DIR/screen.mp4`` placeholder (omitted with ``--no-video``), prints the ``started`` event,
waits for SIGINT / SIGTERM, a ``stop`` line on stdin or stdin EOF, then prints the ``stopped``
event with byte sizes and exits 0. Environment knobs for failure paths:

* ``FAKE_RECORDER_FAIL=<code>``      emit ``{"event":"error","code":<code>}`` instead of starting
* ``FAKE_RECORDER_START_DELAY=<s>``  sleep before ``started`` (start-timeout tests)
* ``FAKE_RECORDER_STOP_DELAY=<s>``   sleep after the stop request before ``stopped``
* ``FAKE_RECORDER_CRASH_ON_STOP=1``  exit 3 without ``stopped`` (recorder crash tests)
* ``FAKE_RECORDER_ERROR_AFTER_STOP=<code>`` emit ``stopped`` (tracks finalized) followed by an
  ``error`` event and exit 1 — a capture failure mid-meeting whose audio survived
* ``FAKE_RECORDER_CHECK=<json>``     what ``check`` prints instead of the all-granted report
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SAMPLE_RATE = 16000
SILENCE_SECONDS = 1.0
TRACK_FILES = {"screen": "screen.mp4", "mic": "mic.wav", "system": "system.wav"}


def emit(event: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_silence(path: Path, seconds: float = SILENCE_SECONDS) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(b"\x00\x00" * int(SAMPLE_RATE * seconds))


def _env_float(name: str) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else 0.0


def record(args: argparse.Namespace) -> int:
    fail = os.environ.get("FAKE_RECORDER_FAIL")
    if fail:
        emit({"event": "error", "code": fail, "message": f"simulated {fail}"})
        return 1
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    tracks: dict[str, str] = {}
    if not args.no_video:
        (out / TRACK_FILES["screen"]).write_bytes(b"")
        tracks["screen"] = TRACK_FILES["screen"]
    for name in ("mic", "system"):
        write_silence(out / TRACK_FILES[name])
        tracks[name] = TRACK_FILES[name]

    time.sleep(_env_float("FAKE_RECORDER_START_DELAY"))
    started_at = now_iso()
    t0 = time.monotonic()
    emit({"event": "started", "started_at": started_at, "tracks": tracks})

    stop = threading.Event()

    def on_signal(_signum: int, _frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    def watch_stdin() -> None:
        for line in sys.stdin:
            if line.strip() == "stop":
                break
        stop.set()  # a "stop" line or EOF (parent went away) both end the recording

    threading.Thread(target=watch_stdin, name="stdin", daemon=True).start()
    while not stop.wait(0.05):
        pass

    emit({"event": "log", "message": "stop requested; finalizing"})
    time.sleep(_env_float("FAKE_RECORDER_STOP_DELAY"))
    if os.environ.get("FAKE_RECORDER_CRASH_ON_STOP"):
        return 3
    stopped_at = now_iso()
    duration = round(time.monotonic() - t0, 3)
    summaries = {
        name: {
            "path": file_name,
            "bytes": (out / file_name).stat().st_size,
            "duration_sec": SILENCE_SECONDS if file_name.endswith(".wav") else 0.0,
        }
        for name, file_name in tracks.items()
    }
    (out / "recorder.json").write_text(
        json.dumps(
            {
                "started_at": started_at,
                "stopped_at": stopped_at,
                "duration_sec": duration,
                "tracks": summaries,
                "recorder_version": "fake",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    emit(
        {
            "event": "stopped",
            "stopped_at": stopped_at,
            "duration_sec": duration,
            "tracks": summaries,
        }
    )
    late_error = os.environ.get("FAKE_RECORDER_ERROR_AFTER_STOP")
    if late_error:
        emit({"event": "error", "code": late_error, "message": f"simulated {late_error}"})
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fake-recorder")
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record")
    rec.add_argument("--output", "-o", required=True)
    rec.add_argument("--display", type=int, default=None)
    rec.add_argument("--no-video", action="store_true")
    rec.add_argument("--mic", default=None)
    sub.add_parser("check")
    args = parser.parse_args(argv)
    if args.command == "check":
        report = os.environ.get("FAKE_RECORDER_CHECK")
        print(report or json.dumps({"screen_recording": "granted", "microphone": "granted"}))
        return 0
    return record(args)


if __name__ == "__main__":
    sys.exit(main())
