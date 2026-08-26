"""Thin deterministic wrappers around the ``ffmpeg`` / ``ffprobe`` binaries.

Every call spawns the binary with explicit arguments (never through a shell). A missing binary
raises ``EngineUnavailableError``; a failing invocation raises ``FfmpegError`` (code ``internal``)
whose ``details["stderr_tail"]`` carries the end of the ffmpeg log.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from narumi.errors import (
    EngineUnavailableError,
    ErrorCode,
    InvalidArgumentError,
    NarumiError,
    NotFoundError,
)

ENV_FFMPEG = "NARUMI_FFMPEG"
ENV_FFPROBE = "NARUMI_FFPROBE"

FALLBACK_DIRS: tuple[str, ...] = ("/opt/homebrew/bin", "/usr/local/bin")
"""Searched after ``PATH`` so launchd-started processes without a shell PATH still find Homebrew."""

INSTALL_HINT = "install ffmpeg (`brew install ffmpeg`) or set NARUMI_FFMPEG to the binary path"
STDERR_TAIL_CHARS = 2000
_VERSION_RE = re.compile(r"^ff(?:mpeg|probe) version (\S+)")
_FRAME_PREFIX = "frame_"


class FfmpegError(NarumiError):
    """ffmpeg / ffprobe exited with an error (``details.stderr_tail`` holds the log tail)."""

    code = ErrorCode.INTERNAL


# ---------------------------------------------------------------------------- binaries
def _resolve_binary(tool: str, env: str, *, sibling_env: str | None = None) -> Path:
    override = os.environ.get(env)
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_file():
            raise EngineUnavailableError(
                f"{env}={override!r} is not a file; {INSTALL_HINT}",
                details={"tool": tool, "path": str(candidate)},
            )
        return candidate
    if sibling_env and os.environ.get(sibling_env):
        sibling = Path(os.environ[sibling_env]).expanduser().with_name(tool)
        if sibling.is_file():
            return sibling
    found = shutil.which(tool)
    if found is not None:
        return Path(found)
    for directory in FALLBACK_DIRS:
        candidate = Path(directory) / tool
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise EngineUnavailableError(
        f"{tool} not found; {INSTALL_HINT}", details={"tool": tool, "env": env}
    )


def ffmpeg_path() -> Path:
    """Path to ``ffmpeg``: ``$NARUMI_FFMPEG`` > ``PATH`` > Homebrew dirs."""
    return _resolve_binary("ffmpeg", ENV_FFMPEG)


def ffprobe_path() -> Path:
    """Path to ``ffprobe``: ``$NARUMI_FFPROBE`` > sibling of ``$NARUMI_FFMPEG`` > ``PATH``."""
    return _resolve_binary("ffprobe", ENV_FFPROBE, sibling_env=ENV_FFMPEG)


def run_tool(
    args: list[str], *, timeout: float | None = None
) -> subprocess.CompletedProcess[bytes]:
    """Run ``args`` (``args[0]`` is the binary) and return the completed process.

    Nonzero exit → ``FfmpegError`` with the stderr tail; unexecutable binary →
    ``EngineUnavailableError``.
    """
    tool = Path(args[0]).name
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            check=False,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise EngineUnavailableError(
            f"{tool} could not be executed ({exc}); {INSTALL_HINT}",
            details={"tool": tool, "args": args},
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise FfmpegError(
            f"{tool} timed out after {timeout}s",
            details={"tool": tool, "args": args, "timeout": timeout},
        ) from exc
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", errors="replace")[-STDERR_TAIL_CHARS:].strip()
        last_line = tail.splitlines()[-1] if tail else "(no stderr)"
        raise FfmpegError(
            f"{tool} failed with exit code {proc.returncode}: {last_line}",
            details={
                "tool": tool,
                "args": args,
                "returncode": proc.returncode,
                "stderr_tail": tail,
            },
        )
    return proc


def ffmpeg_version() -> str:
    """Version of the resolved ffmpeg binary, e.g. ``"9.0.1"`` (cached per binary path)."""
    return _tool_version(str(ffmpeg_path()))


@functools.lru_cache(maxsize=8)
def _tool_version(binary: str) -> str:
    proc = run_tool([binary, "-version"])
    lines = proc.stdout.decode("utf-8", errors="replace").splitlines()
    match = _VERSION_RE.match(lines[0]) if lines else None
    if match is None:
        raise FfmpegError(
            f"could not parse the version banner of {binary}", details={"output": lines[:1]}
        )
    return match.group(1)


# ---------------------------------------------------------------------------- audio
def extract_audio(src: Path, dst: Path, *, sample_rate: int = 16000, channels: int = 1) -> Path:
    """Decode ``src`` (any container, audio or video) to PCM s16le wav at ``dst``.

    Output is written to ``<dst>.part`` and renamed on success, so a failed run never leaves a
    half-written artifact behind. ``-fflags +bitexact`` and ``-map_metadata -1`` keep the bytes
    stable across runs so the artifact hash is reproducible.
    """
    src = Path(src)
    dst = Path(dst)
    if not src.is_file():
        raise NotFoundError(f"audio source missing: {src}", details={"path": str(src)})
    if sample_rate <= 0 or channels <= 0:
        raise InvalidArgumentError(
            "sample_rate and channels must be positive",
            details={"sample_rate": sample_rate, "channels": channels},
        )
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.name + ".part")
    args = [
        str(ffmpeg_path()),
        "-y",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-vn",
        "-sn",
        "-dn",
        "-map_metadata",
        "-1",
        "-ac",
        str(channels),
        "-ar",
        str(sample_rate),
        "-c:a",
        "pcm_s16le",
        "-fflags",
        "+bitexact",
        "-f",
        "wav",
        str(part),
    ]
    try:
        run_tool(args)
    except NarumiError:
        part.unlink(missing_ok=True)
        raise
    part.replace(dst)
    return dst


# ---------------------------------------------------------------------------- probe
def probe(path: Path) -> dict[str, Any]:
    """``ffprobe -show_format -show_streams`` as a dict."""
    path = Path(path)
    if not path.is_file():
        raise NotFoundError(f"media file missing: {path}", details={"path": str(path)})
    args = [
        str(ffprobe_path()),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    proc = run_tool(args)
    try:
        data = json.loads(proc.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FfmpegError(
            f"ffprobe returned invalid JSON for {path}", details={"path": str(path)}
        ) from exc
    if not isinstance(data, dict):
        raise FfmpegError(f"ffprobe returned a non-object for {path}", details={"path": str(path)})
    return data


def _as_seconds(value: Any, *, path: Path, where: str) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise FfmpegError(
            f"ffprobe reported a non-numeric {where} duration for {path}: {value!r}",
            details={"path": str(path), "value": value},
        ) from exc


def probe_duration(path: Path) -> float | None:
    """Duration in seconds from ``ffprobe`` (container first, else the longest stream)."""
    path = Path(path)
    info = probe(path)
    fmt = info.get("format") or {}
    duration = _as_seconds(fmt.get("duration"), path=path, where="format")
    if duration is not None:
        return duration
    stream_durations = [
        d
        for stream in info.get("streams") or []
        if (d := _as_seconds(stream.get("duration"), path=path, where="stream")) is not None
    ]
    return max(stream_durations) if stream_durations else None


# ---------------------------------------------------------------------------- frames
def extract_frames(
    video: Path,
    out_dir: Path,
    *,
    interval_sec: float = 5.0,
    scene_threshold: float = 0.08,
    width: int = 640,
) -> list[Path]:
    """Extract candidate key frames as PNG into ``out_dir`` (groundwork for slide extraction).

    A frame is kept when it is the first frame, when ``interval_sec`` has elapsed since the last
    kept frame, or when ffmpeg's scene score exceeds ``scene_threshold``. Frames are scaled to
    ``width`` (height keeps the aspect ratio, even). Files are named
    ``frame_<index:04d>_<ms:08d>.png`` so lexical order equals chronological order; stale
    ``frame_*.png`` files in ``out_dir`` are removed first.
    """
    video = Path(video)
    out_dir = Path(out_dir)
    if not video.is_file():
        raise NotFoundError(f"video missing: {video}", details={"path": str(video)})
    if interval_sec <= 0:
        raise InvalidArgumentError("interval_sec must be positive", details={"value": interval_sec})
    if not 0.0 <= scene_threshold <= 1.0:
        raise InvalidArgumentError(
            "scene_threshold must be within [0, 1]", details={"value": scene_threshold}
        )
    if width <= 0:
        raise InvalidArgumentError("width must be positive", details={"value": width})
    out_dir.mkdir(parents=True, exist_ok=True)

    select = (
        f"gt(scene,{scene_threshold:g})"
        f"+isnan(prev_selected_t)"
        f"+gte(t-prev_selected_t,{interval_sec:g})"
    )
    video_filter = f"select='{select}',scale={width}:-2"
    work = Path(tempfile.mkdtemp(prefix=".frames-", dir=out_dir))
    args = [
        str(ffmpeg_path()),
        "-y",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video),
        "-an",
        "-sn",
        "-dn",
        "-vf",
        video_filter,
        "-fps_mode",
        "vfr",
        "-enc_time_base",
        "1/1000",
        "-frame_pts",
        "1",
        "-f",
        "image2",
        str(work / f"{_FRAME_PREFIX}%d.png"),
    ]
    try:
        run_tool(args)
        produced = sorted(
            (p for p in work.glob(f"{_FRAME_PREFIX}*.png") if p.is_file()),
            key=_frame_ms,
        )
        for stale in out_dir.glob(f"{_FRAME_PREFIX}*.png"):
            stale.unlink()
        results: list[Path] = []
        for index, source in enumerate(produced):
            target = out_dir / f"{_FRAME_PREFIX}{index:04d}_{_frame_ms(source):08d}.png"
            source.replace(target)
            results.append(target)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return results


def _frame_ms(path: Path) -> int:
    """Millisecond timestamp encoded by ``-frame_pts 1`` with ``-enc_time_base 1/1000``."""
    raw = path.stem.removeprefix(_FRAME_PREFIX)
    try:
        return max(int(raw), 0)
    except ValueError as exc:
        raise FfmpegError(
            f"unexpected frame file name from ffmpeg: {path.name}", details={"path": str(path)}
        ) from exc
