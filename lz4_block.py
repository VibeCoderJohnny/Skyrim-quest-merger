"""
Pure-Python LZ4 block compressor and decompressor for Skyrim SE/AE .ess saves.

Uses the `lz4.block` C extension if installed, and falls back to a pure-Python
implementation of the standard LZ4 block format (no frame header) otherwise.
"""

from __future__ import annotations

import struct

try:
    import lz4.block as _lz4_block  # type: ignore
except ImportError:
    _lz4_block = None


def decompress_block(src: bytes, uncompressed_size: int) -> bytes:
    """Decompress a raw LZ4 block into `uncompressed_size` bytes."""
    if _lz4_block is not None:
        return _lz4_block.decompress(src, uncompressed_size=uncompressed_size)

    dst = bytearray()
    pos = 0
    src_len = len(src)

    while pos < src_len:
        token = src[pos]
        pos += 1

        # Literal length
        lit_len = token >> 4
        if lit_len == 15:
            while pos < src_len:
                extra = src[pos]
                pos += 1
                lit_len += extra
                if extra != 255:
                    break

        if pos + lit_len > src_len:
            raise ValueError("LZ4 block truncated in literals")

        dst.extend(src[pos : pos + lit_len])
        pos += lit_len

        if pos >= src_len:
            break

        # Match offset (uint16 little-endian)
        if pos + 2 > src_len:
            raise ValueError("LZ4 block truncated in match offset")
        offset = src[pos] | (src[pos + 1] << 8)
        pos += 2
        if offset == 0 or offset > len(dst):
            raise ValueError(f"Invalid LZ4 match offset {offset} at output len {len(dst)}")

        # Match length
        match_len = (token & 0x0F) + 4
        if (token & 0x0F) == 15:
            while pos < src_len:
                extra = src[pos]
                pos += 1
                match_len += extra
                if extra != 255:
                    break

        # Copy match byte-by-byte to handle overlapping matches (offset < match_len)
        match_start = len(dst) - offset
        for i in range(match_len):
            dst.append(dst[match_start + i])

    if len(dst) != uncompressed_size:
        raise ValueError(
            f"LZ4 decompressed size mismatch: got {len(dst)}, expected {uncompressed_size}"
        )
    return bytes(dst)


def compress_block(src: bytes) -> bytes:
    """Compress `src` into a raw LZ4 block."""
    if _lz4_block is not None:
        return _lz4_block.compress(src, store_size=False)

    n = len(src)
    if n == 0:
        return b""

    out = bytearray()
    if n < 13:
        _emit_literals_only(out, src)
        return bytes(out)

    # Hash table mapping 4-byte sequence -> most recent index
    hash_table: dict[bytes, int] = {}
    anchor = 0
    ip = 0
    limit = n - 12  # Last 5 bytes must be literals; need at least 4 bytes for match

    while ip <= limit:
        seq = src[ip : ip + 4]
        ref = hash_table.get(seq)
        hash_table[seq] = ip

        if ref is None or ip - ref > 65535 or ip - ref == 0:
            ip += 1
            continue

        # Found a 4-byte match within 64KB window
        match_len = 4
        max_match = n - 5 - ip
        while match_len < max_match and src[ref + match_len] == src[ip + match_len]:
            match_len += 1

        lit_len = ip - anchor
        offset = ip - ref

        # Write token
        lit_token = min(lit_len, 15)
        match_token = min(match_len - 4, 15)
        out.append((lit_token << 4) | match_token)

        # Extended literal length
        if lit_len >= 15:
            rem = lit_len - 15
            while rem >= 255:
                out.append(255)
                rem -= 255
            out.append(rem)

        # Literals
        out.extend(src[anchor : anchor + lit_len])

        # Offset
        out.extend(struct.pack("<H", offset))

        # Extended match length
        if match_len - 4 >= 15:
            rem = (match_len - 4) - 15
            while rem >= 255:
                out.append(255)
                rem -= 255
            out.append(rem)

        ip += match_len
        anchor = ip

    # Trailing literals
    if anchor < n:
        _emit_literals_only(out, src[anchor:])

    return bytes(out)


def _emit_literals_only(out: bytearray, literals: bytes) -> None:
    lit_len = len(literals)
    lit_token = min(lit_len, 15)
    out.append(lit_token << 4)
    if lit_len >= 15:
        rem = lit_len - 15
        while rem >= 255:
            out.append(255)
            rem -= 255
        out.append(rem)
    out.extend(literals)
