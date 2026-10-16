from __future__ import annotations

import json
import logging
import queue
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass, field
from itertools import count
from pathlib import Path
from time import perf_counter
from typing import IO, Any

from ..config import RuntimeConfig
from ..models import (
    CredentialSecret,
    ExecResult,
    RemoteProfile,
    SessionState,
    TransferResult,
)
from .base import BaseTransportSession, TransportAdapter

DRIVER_PATH = Path(__file__).with_name("ps_driver.ps1")
MARKER = "__RS_RESP__"
WRITE_TIMEOUT_SECONDS = 300
TRANSFER_TIMEOUT_SECONDS = 3600
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class WinRMTransportSession(BaseTransportSession):
    process: subprocess.Popen[str]
    responses: queue.Queue[dict[str, Any] | None] = field(default_factory=queue.Queue)
    errors: deque[str] = field(default_factory=lambda: deque(maxlen=20))
    ids: count = field(default_factory=lambda: count(1))
    lock: threading.Lock = field(default_factory=threading.Lock)
    close_lock: threading.Lock = field(default_factory=threading.Lock)
    closed: bool = False
    forced_terminations: int = 0
    remote_session_id: str | None = None
    cleanup_events: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=8)
    )


class WinRMTransportAdapter(TransportAdapter):
    def __init__(self, config: RuntimeConfig | None = None) -> None:
        self.config = config or RuntimeConfig.from_env()

    def open_session(
        self, profile: RemoteProfile, secret: CredentialSecret
    ) -> BaseTransportSession:
        if not secret.password:
            raise ValueError(
                f"A password is required to connect to '{profile.host}' over WinRM."
            )
        args = [
            "pwsh",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(DRIVER_PATH),
            "-RHost",
            profile.host,
            "-User",
            profile.username,
            "-Auth",
            "Negotiate",
            "-Port",
            str(profile.port),
            "-OpenTimeoutMs",
            str(profile.connection_timeout * 1000),
            "-IdleTimeoutMs",
            str(self.config.winrm_idle_timeout_seconds * 1000),
            "-CancelTimeoutMs",
            "5000",
        ]
        if profile.use_ssl:
            args.append("-UseSsl")
        process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        session = WinRMTransportSession(
            profile=profile,
            state=SessionState(
                transport="winrm",
                host=profile.host,
                shell_type="powershell",
                platform="windows",
            ),
            process=process,
        )
        self._start_readers(session)
        try:
            self._write(session, {"password": secret.password})
            ready = self._receive(session, profile.connection_timeout + 5)
            if ready.get("type") != "ready":
                raise ConnectionError(
                    str(ready.get("error") or "WinRM session initialization failed.")
                )
            session.remote_session_id = ready["session_id"]
            session.state.computer_name = ready.get("computer_name")
            session.state.remote_identity = ready.get("remote_identity")
            session.state.cwd = ready.get("cwd")
            return session
        except Exception:
            self.close(session)
            raise

    def is_open(self, session: BaseTransportSession) -> bool:
        winrm = self._as_winrm(session)
        return not winrm.closed and winrm.process.poll() is None

    def is_alive(self, session: BaseTransportSession) -> bool:
        if (
            not isinstance(session, WinRMTransportSession)
            or session.process.poll() is not None
        ):
            return False
        try:
            return bool(self._request(session, {"action": "ping"}, 5).get("success"))
        except (ConnectionError, TimeoutError):
            self._close_driver(session)
            return False

    def execute(
        self, session: BaseTransportSession, command: str, timeout_seconds: float
    ) -> ExecResult:
        winrm = self._as_winrm(session)
        started = perf_counter()
        response = self._request(
            winrm,
            {"action": "execute", "command": command},
            timeout_seconds,
        )
        cwd = response.get("cwd") or winrm.state.cwd
        winrm.state.cwd = cwd
        return ExecResult(
            duration_ms=int((perf_counter() - started) * 1000),
            exit_code=int(response.get("exit_code", 1)),
            stdout=str(response.get("stdout") or "").strip(),
            stderr=str(response.get("stderr") or "").strip(),
            cwd=cwd,
        )

    def upload_path(
        self,
        session: BaseTransportSession,
        local_path: Path,
        remote_path: str,
        *,
        overwrite: bool,
    ) -> TransferResult:
        winrm = self._as_winrm(session)
        source = local_path.expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(f"Local path '{source}' does not exist.")
        response = self._request(
            winrm,
            {
                "action": "copy",
                "direction": "upload",
                "local": str(source),
                "remote": remote_path,
                "overwrite": overwrite,
            },
            TRANSFER_TIMEOUT_SECONDS,
        )
        self._raise_transfer_error(response, "Upload")
        files, total_bytes = self._local_stats(source)
        return TransferResult(
            target_path=str(response.get("target") or remote_path),
            transferred_files=files,
            total_bytes=total_bytes,
        )

    def download_path(
        self,
        session: BaseTransportSession,
        remote_path: str,
        local_path: Path,
        *,
        overwrite: bool,
    ) -> TransferResult:
        winrm = self._as_winrm(session)
        target = local_path.expanduser().resolve()
        response = self._request(
            winrm,
            {
                "action": "copy",
                "direction": "download",
                "local": str(target),
                "remote": remote_path,
                "overwrite": overwrite,
            },
            TRANSFER_TIMEOUT_SECONDS,
        )
        self._raise_transfer_error(response, "Download")
        actual_target = Path(str(response.get("target") or target))
        files, total_bytes = self._local_stats(actual_target)
        return TransferResult(
            target_path=str(actual_target),
            transferred_files=files,
            total_bytes=total_bytes,
        )

    def write_file(
        self,
        session: BaseTransportSession,
        remote_path: str,
        content: str,
    ) -> dict[str, Any]:
        response = self._request(
            self._as_winrm(session),
            {"action": "write", "remote": remote_path, "content": content},
            WRITE_TIMEOUT_SECONDS,
        )
        self._raise_transfer_error(response, "Write")
        return {"remote_path": str(response["remote_path"]), "bytes": response["bytes"]}

    def edit_file(
        self,
        session: BaseTransportSession,
        remote_path: str,
        old_text: str,
        new_text: str,
        replace_all: bool,
    ) -> dict[str, Any]:
        response = self._request(
            self._as_winrm(session),
            {
                "action": "edit",
                "remote": remote_path,
                "old_text": old_text,
                "new_text": new_text,
                "replace_all": replace_all,
            },
            WRITE_TIMEOUT_SECONDS,
        )
        self._raise_transfer_error(response, "Edit")
        return {
            "remote_path": str(response["remote_path"]),
            "replacements": response["replacements"],
            "bytes": response["bytes"],
        }

    def close(self, session: BaseTransportSession) -> None:
        if isinstance(session, WinRMTransportSession):
            self._close_driver(session)

    def _request(
        self,
        session: WinRMTransportSession,
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        with session.lock:
            if session.process.poll() is not None:
                raise ConnectionError(
                    f"WinRM driver exited before dispatch: {self._error_text(session)}"
                )
            request_id = next(session.ids)
            self._write(session, {"id": request_id, **payload})
            try:
                response = self._receive(session, timeout_seconds)
            except TimeoutError:
                self._close_driver(session)
                raise TimeoutError(
                    f"WinRM operation timed out after {timeout_seconds:g} seconds."
                ) from None
            if response.get("id") != request_id:
                self._close_driver(session)
                raise ConnectionError("WinRM driver returned an invalid response.")
            if response.get("connection_lost"):
                self._close_driver(session)
                raise ConnectionError(
                    str(response.get("stderr") or "WinRM connection was lost.")
                )
            return response

    @staticmethod
    def _write(session: WinRMTransportSession, payload: dict[str, Any]) -> None:
        stream = session.process.stdin
        if stream is None or stream.closed:
            raise ConnectionError("WinRM driver input is closed.")
        try:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
            stream.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise ConnectionError("WinRM driver closed before dispatch.") from exc

    def _receive(
        self, session: WinRMTransportSession, timeout_seconds: float
    ) -> dict[str, Any]:
        try:
            response = session.responses.get(timeout=timeout_seconds)
        except queue.Empty as exc:
            raise TimeoutError from exc
        if response is None:
            raise ConnectionError(
                f"WinRM driver closed unexpectedly: {self._error_text(session)}"
            )
        return response

    def _start_readers(self, session: WinRMTransportSession) -> None:
        threading.Thread(
            target=self._read_stdout,
            args=(session,),
            name=f"winrm-out-{session.profile.host}",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._read_stderr,
            args=(session,),
            name=f"winrm-err-{session.profile.host}",
            daemon=True,
        ).start()

    @staticmethod
    def _read_stdout(session: WinRMTransportSession) -> None:
        stream = session.process.stdout
        if stream is None:
            session.responses.put(None)
            return
        try:
            for line in stream:
                if not line.startswith(MARKER):
                    continue
                try:
                    response = json.loads(line[len(MARKER) :])
                    if response.get("type") == "lifecycle":
                        session.cleanup_events.append(response)
                    else:
                        session.responses.put(response)
                except json.JSONDecodeError:
                    session.errors.append("WinRM driver emitted invalid JSON.")
        finally:
            session.responses.put(None)

    @staticmethod
    def _read_stderr(session: WinRMTransportSession) -> None:
        stream = session.process.stderr
        if stream is None:
            return
        for line in stream:
            if text := line.strip():
                session.errors.append(text)

    def _close_driver(self, session: WinRMTransportSession) -> None:
        with session.close_lock:
            if session.closed:
                return
            session.closed = True
            if session.process.poll() is None:
                try:
                    self._write(session, {"action": "close"})
                    session.process.wait(
                        timeout=self.config.driver_close_timeout_seconds
                    )
                except (
                    ConnectionError,
                    OSError,
                    ValueError,
                    subprocess.TimeoutExpired,
                ):
                    session.forced_terminations += 1
                    logger.warning(
                        "WinRM driver %s forced to exit; session=%s cleanup=%s",
                        session.process.pid,
                        session.remote_session_id,
                        list(session.cleanup_events),
                    )
            self._terminate(session)

    @staticmethod
    def _terminate(session: WinRMTransportSession) -> None:
        process = session.process
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        for stream in (process.stdin, process.stdout, process.stderr):
            WinRMTransportAdapter._close_stream(stream)

    @staticmethod
    def _close_stream(stream: IO[Any] | None) -> None:
        if stream is not None and not stream.closed:
            try:
                stream.close()
            except OSError:
                pass

    @staticmethod
    def _error_text(session: WinRMTransportSession) -> str:
        return " | ".join(session.errors) or "no driver error was reported"

    @staticmethod
    def _local_stats(path: Path) -> tuple[int, int]:
        if path.is_file():
            return 1, path.stat().st_size
        files = total_bytes = 0
        for item in path.rglob("*"):
            if item.is_file():
                files += 1
                total_bytes += item.stat().st_size
        return files, total_bytes

    @staticmethod
    def _raise_transfer_error(response: dict[str, Any], operation: str) -> None:
        if not response.get("success"):
            raise RuntimeError(
                f"{operation} failed: {response.get('stderr') or 'unknown error'}"
            )

    @staticmethod
    def _as_winrm(session: BaseTransportSession) -> WinRMTransportSession:
        if not isinstance(session, WinRMTransportSession):
            raise TypeError("WinRM adapter received an unexpected session type.")
        return session
