"""Perceptual hashing for slide frames (pure Python + pillow, deterministic).

The hash is 128 bits, hex encoded (32 chars): a 64-bit DCT pHash (structure) followed by a
64-bit fixed-threshold average hash (absolute luminance). The DCT half alone cannot tell two
flat frames of different colors apart (every AC coefficient is zero for both) and real decks
produce flat frames all the time (blank screens, title slides); the fixed 128-luminance half
separates those while staying fully deterministic. The DCT is hand rolled — only the low
frequency 8x8 block is computed, so no numpy/scipy dependency is needed.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from functools import cache
from pathlib import Path
from typing import Any

from narumi.errors import EngineUnavailableError, InvalidArgumentError, NotFoundError

try:
    from PIL import Image as _pil_image
except ImportError:  # pragma: no cover - exercised only without the slides extra
    _pil_image = None  # type: ignore[assignment]

PHASH_SIZE = 32
"""Grayscale downsample edge for the DCT half."""
DCT_BLOCK = 8
"""Low-frequency DCT block edge (8x8 = 64 coefficients, DC dropped)."""
AHASH_SIZE = 8
AHASH_THRESHOLD = 128
"""Fixed luminance threshold of the average-hash half (not the mean: flat frames must differ)."""
HASH_BITS = 128
HASH_HEX_LEN = HASH_BITS // 4
PILLOW_HINT = "install the slides extra (`uv sync` in the repo, or `pip install narumi[slides]`)"


def _require_pillow() -> Any:
    if _pil_image is None:  # pragma: no cover - exercised only without the slides extra
        raise EngineUnavailableError(
            f"pillow is not installed; {PILLOW_HINT}", details={"module": "PIL"}
        )
    return _pil_image


@cache
def _cos_table(n: int, block: int) -> tuple[tuple[float, ...], ...]:
    """``cos((2x+1)·u·π / 2n)`` for ``u < block``, ``x < n`` (DCT-II basis, unnormalized)."""
    return tuple(
        tuple(math.cos(math.pi * (2 * x + 1) * u / (2 * n)) for x in range(n)) for u in range(block)
    )


def _dct_lowfreq(pixels: Sequence[float], n: int, block: int) -> list[float]:
    """Top-left ``block``x``block`` 2D DCT-II coefficients of an ``n``x``n`` image, row major.

    Normalization factors are omitted (like common pHash implementations): the bits compare
    coefficients against their own median, so a per-coefficient scale has no effect there and
    determinism is all that matters.
    """
    cos = _cos_table(n, block)
    rows = [[0.0] * n for _ in range(block)]
    for u in range(block):
        cu = cos[u]
        row = rows[u]
        for x in range(n):
            c = cu[x]
            base = x * n
            for y in range(n):
                row[y] += pixels[base + y] * c
    coeffs: list[float] = []
    for u in range(block):
        row = rows[u]
        for v in range(block):
            cv = cos[v]
            coeffs.append(sum(row[y] * cv[y] for y in range(n)))
    return coeffs


def _phash_bits(gray: Sequence[int]) -> int:
    """64-bit field: 63 AC coefficients thresholded by their median, one trailing zero bit.

    Coefficients are rounded to 6 decimals first: on flat frames every AC coefficient is
    analytically zero but carries ~1e-11 float summation noise, and thresholding that noise by
    its own median would produce pseudo-random bits. Real content has coefficients orders of
    magnitude above the rounding, so it is unaffected.
    """
    coeffs = _dct_lowfreq([float(p) for p in gray], PHASH_SIZE, DCT_BLOCK)
    ac = [round(c, 6) for c in coeffs[1:]]  # drop the DC coefficient
    median = statistics.median(ac)
    bits = 0
    for coeff in ac:
        bits = (bits << 1) | (coeff > median)
    return bits << 1


def _ahash_bits(image: Any) -> int:
    small = image.resize((AHASH_SIZE, AHASH_SIZE), _pil_image.Resampling.LANCZOS)
    bits = 0
    for pixel in small.tobytes():  # mode "L": one byte per pixel, row major
        bits = (bits << 1) | (pixel >= AHASH_THRESHOLD)
    return bits


def phash_image(image: Any) -> str:
    """128-bit perceptual hash of a ``PIL.Image.Image`` as a 32-char lowercase hex string."""
    _require_pillow()
    gray = image.convert("L")
    big = gray.resize((PHASH_SIZE, PHASH_SIZE), _pil_image.Resampling.LANCZOS)
    return f"{_phash_bits(big.tobytes()):016x}{_ahash_bits(gray):016x}"


def phash(path: Path | str) -> str:
    """Perceptual hash of the image file at ``path`` (see :func:`phash_image`)."""
    pil = _require_pillow()
    path = Path(path)
    if not path.is_file():
        raise NotFoundError(f"image missing: {path}", details={"path": str(path)})
    with pil.open(path) as image:
        return phash_image(image)


def hamming(a: str, b: str) -> int:
    """Hamming distance between two equally sized hex hash strings (0 = identical)."""
    if not a or not b or len(a) != len(b):
        raise InvalidArgumentError(
            "hashes must be non-empty hex strings of equal length",
            details={"a_len": len(a), "b_len": len(b)},
        )
    try:
        return (int(a, 16) ^ int(b, 16)).bit_count()
    except ValueError as exc:
        raise InvalidArgumentError("hashes must be hexadecimal", details={"a": a, "b": b}) from exc
