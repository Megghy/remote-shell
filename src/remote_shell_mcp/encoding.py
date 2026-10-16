from __future__ import annotations

import re

_MIN_RUN = 8
_ASCII = frozenset((9, 10, 13, *range(0x20, 0x7F)))
_ASCII_NUL_RUN = re.compile(r"(?:[\t\n\r\x20-\x7e]\x00){8,}")


def decode_output(data: bytes) -> str:
    """Decode command output that may be UTF-8, UTF-16 LE, or a mix of both."""
    if not data:
        return ""
    if data.startswith(b"\xff\xfe"):
        return _decode_utf16(data[2:], "utf-16-le")
    if data.startswith(b"\xfe\xff"):
        return _decode_utf16(data[2:], "utf-16-be")
    run = _first_ascii_nul_run(data)
    if run is not None and run % 2 == 0 and not _valid_utf8_prefix(data[:run]):
        return _decode_utf16(data, "utf-16-le")
    return "".join(_segments(data, run))


def repair_text(text: str) -> str:
    """Undo UTF-16 LE that was already decoded as UTF-8/Latin-1 (NUL-interleaved)."""
    if text.count("\x00") < _MIN_RUN:
        return text
    if all(ord(c) <= 0xFF for c in text):
        repaired = decode_output(text.encode("latin-1"))
        if "\x00" not in repaired:
            return repaired
    return _ASCII_NUL_RUN.sub(lambda match: match.group(0)[::2], text)


def _decode_utf16(data: bytes, encoding: str) -> str:
    even = data if len(data) % 2 == 0 else data[:-1]
    return even.decode(encoding, errors="replace") if even else ""


def _valid_utf8_prefix(data: bytes) -> bool:
    if not data:
        return False
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _run_len(data: bytes, index: int) -> int:
    n = 0
    while index + 1 < len(data) and data[index] in _ASCII and data[index + 1] == 0:
        n += 1
        index += 2
    return n


def _first_ascii_nul_run(data: bytes, start: int = 0) -> int | None:
    limit = len(data) - 2 * _MIN_RUN
    while start <= limit:
        if _run_len(data, start) >= _MIN_RUN:
            return start
        start += 1
    return None


def _segments(data: bytes, first_run: int | None):
    index = 0
    next_at = first_run
    while index < len(data):
        if next_at is None:
            yield _utf8(data[index:])
            return
        if next_at > index:
            yield _utf8(data[index:next_at])
        end = next_at + _run_len(data, next_at) * 2
        yield data[next_at:end].decode("utf-16-le")
        index = end
        next_at = _first_ascii_nul_run(data, index)


def _utf8(data: bytes) -> str:
    return data.decode("utf-8", errors="replace") if data else ""
