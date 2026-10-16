from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import (
    CredentialSecret,
    ExecResult,
    RemoteProfile,
    SessionState,
    TransferResult,
)


@dataclass(slots=True)
class BaseTransportSession:
    profile: RemoteProfile
    state: SessionState


class TransportAdapter(ABC):
    @abstractmethod
    def open_session(
        self, profile: RemoteProfile, secret: CredentialSecret
    ) -> BaseTransportSession:
        raise NotImplementedError

    @abstractmethod
    def is_open(self, session: BaseTransportSession) -> bool:
        """Check local transport state without remote I/O."""
        raise NotImplementedError

    @abstractmethod
    def is_alive(self, session: BaseTransportSession) -> bool:
        raise NotImplementedError

    @abstractmethod
    def execute(
        self, session: BaseTransportSession, command: str, timeout_seconds: float
    ) -> ExecResult:
        raise NotImplementedError

    @abstractmethod
    def upload_path(
        self,
        session: BaseTransportSession,
        local_path: Path,
        remote_path: str,
        *,
        overwrite: bool,
    ) -> TransferResult:
        raise NotImplementedError

    @abstractmethod
    def download_path(
        self,
        session: BaseTransportSession,
        remote_path: str,
        local_path: Path,
        *,
        overwrite: bool,
    ) -> TransferResult:
        raise NotImplementedError

    @abstractmethod
    def write_file(
        self, session: BaseTransportSession, remote_path: str, content: str,
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def edit_file(
        self, session: BaseTransportSession, remote_path: str,
        old_text: str, new_text: str, replace_all: bool,
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def close(self, session: BaseTransportSession) -> None:
        raise NotImplementedError
