from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .encoding import repair_text

TransportName = Literal["winrm", "ssh"]

_MAX_OUTPUT_CHARS = 20_000
_MAX_OUTPUT_LINES = 200
_HEAD_LINES = 120
_TAIL_LINES = 40
_HEAD_CHARS = 12_000
_TAIL_CHARS = 4_000


def _truncate(text: str) -> tuple[str, dict[str, int] | None]:
    total_chars = len(text)
    lines = text.splitlines()
    total_lines = len(lines)
    if total_chars <= _MAX_OUTPUT_CHARS and total_lines <= _MAX_OUTPUT_LINES:
        return text, None

    if total_lines > _MAX_OUTPUT_LINES:
        omitted = total_lines - _HEAD_LINES - _TAIL_LINES
        lines = [
            *lines[:_HEAD_LINES],
            f"... [{omitted} of {total_lines} lines omitted] ...",
            *lines[-_TAIL_LINES:],
        ]
        text = "\n".join(lines)

    if len(text) > _MAX_OUTPUT_CHARS:
        omitted = len(text) - _HEAD_CHARS - _TAIL_CHARS
        text = f"{text[:_HEAD_CHARS]}\n... [{omitted} characters omitted] ...\n{text[-_TAIL_CHARS:]}"

    return text, {"total_lines": total_lines, "total_chars": total_chars}


@dataclass(slots=True)
class RemoteProfile:
    target: str
    host: str
    username: str
    transport: TransportName
    port: int
    use_ssl: bool = False
    connection_timeout: int = 30
    created_at: int = 0
    updated_at: int = 0

    def connection_key(self) -> tuple[str, str, str, int, bool]:
        return self.host, self.username, self.transport, self.port, self.use_ssl


@dataclass(slots=True)
class CredentialSecret:
    password: str | None = None


@dataclass(slots=True)
class SessionState:
    transport: TransportName
    host: str
    shell_type: str
    platform: str
    computer_name: str | None = None
    remote_identity: str | None = None
    cwd: str | None = None


@dataclass(slots=True)
class ExecResult:
    duration_ms: int
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    cwd: str | None = None

    def __post_init__(self) -> None:
        self.stdout = repair_text(self.stdout)
        self.stderr = repair_text(self.stderr)

    def to_dict(self) -> dict[str, Any]:
        stdout, stdout_meta = _truncate(self.stdout)
        stderr, stderr_meta = _truncate(self.stderr)
        payload: dict[str, Any] = {
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
        }
        if stdout:
            payload["stdout"] = stdout
        if stderr:
            payload["stderr"] = stderr
        if stdout_meta or stderr_meta:
            payload["truncated"] = {
                key: value
                for key, value in (("stdout", stdout_meta), ("stderr", stderr_meta))
                if value is not None
            }
        return payload


@dataclass(slots=True)
class TransferResult:
    target_path: str
    transferred_files: int
    total_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "destination": self.target_path,
            "files": self.transferred_files,
            "bytes": self.total_bytes,
        }
