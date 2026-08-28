"""App icon source, committed resources, and native macOS generation."""

from __future__ import annotations

import io
import shutil
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "app" / "Assets"
GENERATOR = ROOT / "scripts" / "generate-app-icon.sh"
SVG_NS = "{http://www.w3.org/2000/svg}"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
ICONSET_SIZES = {
    "icon_16x16.png": 16,
    "icon_16x16@2x.png": 32,
    "icon_32x32.png": 32,
    "icon_32x32@2x.png": 64,
    "icon_128x128.png": 128,
    "icon_128x128@2x.png": 256,
    "icon_256x256.png": 256,
    "icon_256x256@2x.png": 512,
    "icon_512x512.png": 512,
    "icon_512x512@2x.png": 1024,
}
ICNS_SIZES = {
    b"ic04": 16,
    b"ic05": 32,
    b"icp4": 16,
    b"icp5": 32,
    b"ic07": 128,
    b"ic08": 256,
    b"ic09": 512,
    b"ic10": 1024,
    b"ic11": 32,
    b"ic12": 64,
    b"ic13": 256,
    b"ic14": 512,
}


def icns_entries(path: Path) -> dict[bytes, bytes]:
    data = path.read_bytes()
    assert data[:4] == b"icns"
    assert struct.unpack(">I", data[4:8])[0] == len(data)
    entries = {}
    offset = 8
    while offset < len(data):
        kind, length = struct.unpack(">4sI", data[offset : offset + 8])
        assert length > 8
        assert offset + length <= len(data)
        assert kind not in entries
        entries[kind] = data[offset + 8 : offset + length]
        offset += length
    assert offset == len(data)
    return entries


def test_app_icon_svg_is_self_contained_vector_source():
    root = ET.parse(ASSETS / "AppIcon.svg").getroot()
    assert root.tag == SVG_NS + "svg"
    assert root.attrib["viewBox"] == "0 0 1024 1024"
    assert root.attrib["width"] == root.attrib["height"] == "1024"
    allowed = {
        "svg",
        "title",
        "desc",
        "defs",
        "linearGradient",
        "stop",
        "rect",
        "polyline",
        "circle",
    }
    for element in root.iter():
        assert element.tag.removeprefix(SVG_NS) in allowed
        assert not any(name.endswith("href") for name in element.attrib)
    assert root.find(SVG_NS + "polyline").attrib["stroke-linecap"] == "round"


def test_app_icon_master_has_transparent_padding_and_distinct_mark():
    with Image.open(ASSETS / "AppIcon.png") as source:
        assert source.format == "PNG"
        assert source.size == (1024, 1024)
        assert source.mode == "RGBA"
        image = source.convert("RGBA")
    for point in [(0, 0), (1023, 0), (0, 1023), (1023, 1023)]:
        assert image.getpixel(point)[3] == 0
    red, green, blue, alpha = image.getpixel((128, 512))
    assert green > red and blue > red and alpha == 255
    assert min(image.getpixel((320, 512))) > 235
    red, green, blue, alpha = image.getpixel((792, 248))
    assert red > green + 50 and red > blue + 50 and alpha == 255


def test_committed_icns_has_all_standard_resolutions():
    entries = icns_entries(ASSETS / "AppIcon.icns")
    assert {b"ic07", b"ic08", b"ic09", b"ic10", b"ic11", b"ic12", b"ic13", b"ic14"} <= (
        entries.keys()
    )
    sizes = set()
    for kind, payload in entries.items():
        if kind not in ICNS_SIZES:
            continue
        size = ICNS_SIZES[kind]
        sizes.add(size)
        if payload.startswith(PNG_SIGNATURE):
            with Image.open(io.BytesIO(payload)) as image:
                assert image.size == (size, size)
                image.verify()
        else:
            # iconutil may encode the two small 1x images as Apple ARGB data.
            assert kind in {b"ic04", b"ic05"}
            assert payload.startswith(b"ARGB")
    assert sizes == {16, 32, 64, 128, 256, 512, 1024}
    with Image.open(io.BytesIO(entries[b"ic10"])) as largest:
        with Image.open(ASSETS / "AppIcon.png") as preview:
            assert largest.convert("RGBA").tobytes() == preview.convert("RGBA").tobytes()


@pytest.mark.skipif(
    sys.platform != "darwin" or not shutil.which("sips") or not shutil.which("iconutil"),
    reason="icon generation uses native macOS tools",
)
def test_generator_round_trips_all_iconset_sizes(tmp_path: Path):
    output = tmp_path / "generated assets"
    subprocess.run(["bash", str(GENERATOR), str(output)], check=True, capture_output=True)
    iconset = tmp_path / "Decoded.iconset"
    subprocess.run(
        ["iconutil", "-c", "iconset", str(output / "AppIcon.icns"), "-o", str(iconset)],
        check=True,
        capture_output=True,
    )
    assert {path.name for path in output.iterdir()} == {"AppIcon.png", "AppIcon.icns"}
    assert {path.name for path in iconset.iterdir()} == ICONSET_SIZES.keys()
    for filename, size in ICONSET_SIZES.items():
        with Image.open(iconset / filename) as image:
            assert image.size == (size, size)
            assert image.convert("RGBA").getpixel((0, 0))[3] == 0
            assert image.convert("RGBA").getextrema()[3][1] == 255


def test_generator_rejects_extra_arguments_without_writing(tmp_path: Path):
    output = tmp_path / "unused"
    result = subprocess.run(
        ["bash", str(GENERATOR), str(output), "unexpected"],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2
    assert "Usage:" in result.stderr
    assert not output.exists()
