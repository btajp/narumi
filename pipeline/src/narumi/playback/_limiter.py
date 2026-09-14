"""A peak limiter with explicit headroom and sample-aligned lookahead compensation."""

from __future__ import annotations

import re

from narumi.errors import EngineUnavailableError

PEAK_CEILING_DBFS = -2.0
LIMIT_LINEAR = 10 ** (PEAK_CEILING_DBFS / 20)
OVERSAMPLE_RATE = 192000
OUTPUT_RATE = 48000
ATTACK_MS = 5.0
RELEASE_MS = 50.0
LIMITER_SETTINGS = {
    "filter": "alimiter",
    "headroom_dbfs": PEAK_CEILING_DBFS,
    "limit_linear": LIMIT_LINEAR,
    "attack_ms": ATTACK_MS,
    "release_ms": RELEASE_MS,
    "auto_level": False,
    "latency_compensation": True,
    "oversample_rate": OVERSAMPLE_RATE,
    "output_rate": OUTPUT_RATE,
}
LIMITER_FILTER = (
    f"aresample={OVERSAMPLE_RATE},"
    f"alimiter=limit={LIMIT_LINEAR:.16f}:attack={ATTACK_MS}:release={RELEASE_MS}:"
    f"level=false:latency=true,aresample={OUTPUT_RATE}"
)


def ensure_limiter_supported(version: str) -> None:
    """Reject known old releases; snapshot version banners are checked by ffmpeg itself."""
    release = re.match(r"^(\d+)\.(\d+)(?:[.\-+]|$)", version)
    if release is not None and tuple(map(int, release.groups())) < (5, 1):
        raise EngineUnavailableError(
            "録画の音量補正には FFmpeg 5.1 以降が必要です",
            details={"minimum_version": "5.1", "version": version, "feature": "alimiter_latency"},
        )
