"""Decode Chaos Vantage's getImage reply and write it as a PNG, without third-party packages.

Vantage 3.4's getImage answers {"bitmap": <base64 BMP + one NUL>, "headerSize": 56}:
a 32-bit BI_RGB Windows BMP, normally top-down (negative height), pixels B,G,R,X.
The fourth byte isn't a usable alpha.
"""

from __future__ import annotations

import base64
import binascii
import math
import struct
import zlib
from typing import Optional


class VantageImageError(ValueError):
    """The getImage reply didn't hold a usable BMP."""


def decode_bitmap(doc: dict) -> Optional[bytes]:
    """The BMP file bytes from a getImage reply, or None when Vantage had no frame.

    Takes the base64 text out of doc, so a large frame isn't held twice.
    """
    b64 = doc.pop("bitmap", None)
    if b64 is None:
        return None
    if not isinstance(b64, str):
        raise VantageImageError("getImage 'bitmap' isn't a string")
    try:
        return base64.b64decode(b64.rstrip("\x00"), validate=True)  # Vantage appends one NUL
    except (binascii.Error, ValueError) as exc:
        raise VantageImageError("getImage 'bitmap' isn't valid base64: %s" % exc) from exc


def bmp_info(bmp: bytes, header_size: int = 0) -> dict:
    """Width, height, row order, bytes per pixel and pixel offset of a BI_RGB 24/32-bit BMP.

    Only uncompressed BI_RGB is accepted: anything else (bit fields, palettes)
    would decode to wrong colours rather than fail.
    """
    if len(bmp) < 54 or bmp[:2] != b"BM":
        raise VantageImageError("getImage didn't return a BMP")
    off_bits = struct.unpack_from("<I", bmp, 10)[0]
    bi_size, width, height, planes, bpp, compression = struct.unpack_from("<IiiHHI", bmp, 14)
    if bi_size < 40 or planes != 1 or bpp not in (24, 32) or compression != 0 or width <= 0 or height == 0:
        raise VantageImageError("unsupported BMP (header %d, %dx%d, %d bpp, compression %d)"
                                % (bi_size, width, height, bpp, compression))
    if header_size and header_size != off_bits:
        raise VantageImageError("getImage headerSize %d disagrees with the BMP's pixel offset %d"
                                % (header_size, off_bits))
    pixel_bytes = bpp // 8
    stride = (width * pixel_bytes + 3) & ~3
    offset = off_bits
    rows = abs(height)
    if offset < 14 + bi_size or len(bmp) < offset + stride * rows:
        raise VantageImageError("BMP pixel data is truncated (%d bytes for %dx%d)" % (len(bmp), width, rows))
    return {"width": width, "height": rows, "top_down": height < 0, "bpp": bpp,
            "stride": stride, "offset": offset}


def downscale_factor(width: int, max_width: int) -> int:
    """Whole factor that brings width to at most max_width (0 = keep full size)."""
    if max_width <= 0 or width <= max_width:
        return 1
    return math.ceil(width / max_width)


def bmp_to_png(bmp: bytes, header_size: int = 0, max_width: int = 0) -> tuple[bytes, dict]:
    """PNG bytes (RGB, 8-bit) and {width, height, source_width, source_height, factor}.

    Downscaling keeps every factor-th pixel and row (nearest neighbour), so the
    frame is never resampled across pixels.
    """
    info = bmp_info(bmp, header_size)
    w, h, step = info["width"], info["height"], info["bpp"] // 8
    factor = downscale_factor(w, max_width)
    rows = range(0, h, factor)
    out_w = len(range(0, w, factor))
    raw = bytearray()
    out_row = bytearray(out_w * 3)
    span = step * factor
    for y in rows:
        src_y = y if info["top_down"] else h - 1 - y
        start = info["offset"] + src_y * info["stride"]
        line = bmp[start:start + w * step]
        out_row[0::3] = line[2::span]  # R
        out_row[1::3] = line[1::span]  # G
        out_row[2::3] = line[0::span]  # B
        raw += b"\x00"  # PNG filter type 0 (none) per row
        raw += out_row
    out_h = len(rows)
    png = _png_rgb(out_w, out_h, raw)
    return png, {"width": out_w, "height": out_h, "source_width": w, "source_height": h, "factor": factor}


def _png_rgb(width: int, height: int, raw) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")
