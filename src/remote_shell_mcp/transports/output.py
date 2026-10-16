from __future__ import annotations

from ..encoding import decode_output


class BoundedOutput:
    def __init__(self, limit: int = 262_144) -> None:
        self.limit = limit
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def append(self, data: bytes) -> None:
        self.total += len(data)
        take = min(len(data), self.limit // 2 - len(self.head))
        self.head.extend(data[:take])
        self.tail.extend(data[take:])
        del self.tail[: max(0, len(self.tail) - self.limit // 2)]

    def text(self) -> str:
        omitted = self.total - len(self.head) - len(self.tail)
        if omitted == 0:
            return decode_output(bytes(self.head + self.tail)).strip()
        return (
            decode_output(bytes(self.head))
            + f"\n... [{omitted} bytes omitted; {self.total} total bytes] ...\n"
            + decode_output(bytes(self.tail))
        ).strip()


class MarkedOutput:
    def __init__(self, marker: bytes, *, status: bool = False) -> None:
        self.output = BoundedOutput()
        self.marker = marker
        self.status = status
        self.pending = bytearray()
        self.done = False
        self.code: int | None = None
        self.cwd: str | None = None

    def feed(self, data: bytes) -> None:
        if self.done:
            return
        self.pending.extend(data)
        start = self.pending.find(self.marker)
        if start < 0:
            count = max(0, len(self.pending) - len(self.marker) + 1)
            self.output.append(bytes(self.pending[:count]))
            del self.pending[:count]
            return
        self.output.append(bytes(self.pending[:start]))
        del self.pending[:start]
        if self.status:
            end = self.pending.find(b"\n")
            if end < 0:
                if len(self.pending) > 65_536:
                    raise ConnectionError("SSH status marker exceeds 64 KiB.")
                return
            value = bytes(self.pending[len(self.marker) : end]).rstrip(b"\r")
            code, separator, cwd = value.partition(b"__")
            if not separator:
                raise ConnectionError("Invalid SSH status marker.")
            self.code = int(code)
            self.cwd = cwd.decode("utf-8", errors="replace") or None
        self.pending.clear()
        self.done = True
