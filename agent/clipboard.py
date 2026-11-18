from __future__ import annotations

import ctypes
import struct
import sys
import time
import zlib
from ctypes import wintypes
from pathlib import Path

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
CF_DIB = 8
CF_DIBV5 = 17
MAX_DIMENSION = 2560


def grab_clipboard_image(dest: str | Path) -> Path | None:
    """Lưu ảnh đang có trong clipboard Windows thành PNG. Trả về None nếu clipboard không có ảnh."""
    if sys.platform != "win32":
        return None
    png = _clipboard_png()
    if png is None:
        return None
    target = Path(dest)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(png)
    return target


def _clipboard_png() -> bytes | None:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterClipboardFormatW.restype = wintypes.UINT
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalSize.restype = ctypes.c_size_t

    for _ in range(4):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return None

    try:
        png_format = user32.RegisterClipboardFormatW("PNG")
        if png_format:
            blob = _read_format(user32, kernel32, int(png_format))
            if blob and blob.startswith(PNG_SIGNATURE):
                return blob
        for fmt in (CF_DIBV5, CF_DIB):
            blob = _read_format(user32, kernel32, fmt)
            if not blob:
                continue
            try:
                return _dib_to_png(blob)
            except (ValueError, struct.error):
                continue
        return None
    finally:
        user32.CloseClipboard()


def _read_format(user32, kernel32, fmt: int) -> bytes | None:
    handle = user32.GetClipboardData(fmt)
    if not handle:
        return None
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        return None
    try:
        size = kernel32.GlobalSize(handle)
        if not size:
            return None
        return ctypes.string_at(pointer, size)
    finally:
        kernel32.GlobalUnlock(handle)


def _dib_to_png(dib: bytes) -> bytes:
    if len(dib) < 40:
        raise ValueError("DIB quá ngắn")

    header_size = struct.unpack_from("<I", dib, 0)[0]
    if header_size == 12:
        width, height, _planes, bpp = struct.unpack_from("<HHHH", dib, 4)
        compression = 0
        colors_used = 0
        palette_offset = 12
        if bpp not in (24, 32, 8):
            raise ValueError(f"không hỗ trợ {bpp}bpp")
    else:
        if header_size < 40 or header_size > len(dib):
            raise ValueError("BITMAPINFOHEADER không hợp lệ")
        width, height = struct.unpack_from("<ii", dib, 4)
        _planes, bpp = struct.unpack_from("<HH", dib, 12)
        compression = struct.unpack_from("<I", dib, 16)[0]
        colors_used = struct.unpack_from("<I", dib, 32)[0]
        if compression not in (0, 3):
            raise ValueError(f"không hỗ trợ compression {compression}")
        palette_offset = header_size
        if compression == 3 and header_size == 40:
            palette_offset += 12

    top_down = height < 0
    height = abs(height)
    if width <= 0 or height <= 0:
        raise ValueError("kích thước ảnh không hợp lệ")

    if bpp == 8:
        palette = _palette(dib, palette_offset, colors_used, header_size)
        if not palette:
            raise ValueError("bảng màu rỗng")
    elif bpp in (24, 32):
        palette = []
    else:
        raise ValueError(f"không hỗ trợ {bpp}bpp")

    stride = ((width * bpp + 31) // 32) * 4
    if len(dib) < palette_offset + stride * height:
        raise ValueError("dữ liệu pixel bị thiếu")

    step = max(1, -(-max(width, height) // MAX_DIMENSION))
    out_w = (width + step - 1) // step
    out_h = (height + step - 1) // step

    raw = bytearray()
    for row_index in range(out_h):
        source_y = row_index * step
        if not top_down:
            source_y = height - 1 - source_y
        start = palette_offset + source_y * stride
        row = dib[start:start + width * (bpp // 8)]
        raw.append(0)  # PNG filter: none

        if bpp == 32:
            line = bytearray(out_w * 3)
            line[0::3] = row[2::step * 4]
            line[1::3] = row[1::step * 4]
            line[2::3] = row[0::step * 4]
            raw += line
        elif bpp == 24:
            line = bytearray(out_w * 3)
            line[0::3] = row[2::step * 3]
            line[1::3] = row[1::step * 3]
            line[2::3] = row[0::step * 3]
            raw += line
        else:
            for index in row[0::step]:
                red, green, blue = palette[index] if index < len(palette) else (0, 0, 0)
                raw += bytes((red, green, blue))

    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", out_w, out_h, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + _chunk(b"IEND", b"")
    )


def _palette(dib: bytes, offset: int, colors_used: int, header_size: int) -> list[tuple[int, int, int]]:
    count = colors_used or 256
    entry_size = 3 if header_size == 12 else 4
    palette: list[tuple[int, int, int]] = []
    for index in range(count):
        base = offset + index * entry_size
        if base + 3 > len(dib):
            break
        palette.append((dib[base + 2], dib[base + 1], dib[base]))
    return palette


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )
