from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path
from time import monotonic
from typing import Any, TypeVar

from .config import RuntimeConfig
from .connection_pool import (
    ConnectionPool,
    ManagedConnection,
    check_cancelled,
    operation_cancelled,
)
from .credential_store import CredentialStore, lookup_windows_credential
from .models import CredentialSecret, RemoteProfile, TransportName
from .profile_store import ProfileStore
from .ssh_config import lookup_ssh_config
from .transports.ssh import SSHTransportAdapter
from .transports.winrm import WinRMTransportAdapter

T = TypeVar("T")


class SessionManager(ConnectionPool):
    def __init__(
        self,
        profile_store: ProfileStore,
        credential_store: CredentialStore,
        config: RuntimeConfig | None = None,
    ) -> None:
        self.profile_store = profile_store
        self.credential_store = credential_store
        config = config or RuntimeConfig.from_env()
        self._workers = ThreadPoolExecutor(
            max_workers=config.max_connections, thread_name_prefix="remote-shell"
        )
        super().__init__(
            config,
            {
                "winrm": WinRMTransportAdapter(config),
                "ssh": SSHTransportAdapter(),
            },
        )

    def submit(
        self, callback: Callable[..., T], host: str, *args, **kwargs
    ) -> Future[T]:
        return self._workers.submit(copy_context().run, callback, host, *args, **kwargs)

    def close_all(self) -> None:
        try:
            super().close_all()
        finally:
            self._workers.shutdown(wait=True, cancel_futures=True)

    def connect(
        self,
        host: str,
        *,
        username: str | None,
        password: str | None,
        transport: TransportName | None,
        port: int | None,
        use_ssl: bool | None,
    ) -> dict[str, Any]:
        with self._host_guard(host):
            saved = self._saved_profile(host)
            profile = self._build_profile(
                host, username, transport, port, use_ssl, saved
            )
            current = self._connection(host)
            if (
                current is not None
                and password is None
                and current.profile.connection_key() == profile.connection_key()
                and self._connection_alive(current)
            ):
                with self._condition:
                    current.last_used_at = monotonic()
                return self._connection_payload(current, reused=True)
            if current is not None:
                self._close_connection(host, current)
            secret = (
                CredentialSecret(password)
                if password is not None
                else self.credential_store.get(host)
            )
            managed = self._open_connection(profile, secret)
            try:
                check_cancelled()
                self._persist(profile, password, saved)
                check_cancelled()
            except BaseException:
                self._dispose(managed)
                raise
            self._register(managed)
            return self._connection_payload(managed, reused=False)

    def execute(
        self, host: str, command: str, timeout_seconds: float
    ) -> dict[str, Any]:
        self._check_content(command)
        result, reconnected = self._perform(
            host,
            lambda managed: self.adapters[managed.profile.transport].execute(
                managed.session,
                command,
                timeout_seconds,
            ),
        )
        return self._result_payload(result.to_dict(), reconnected)

    def upload_path(
        self,
        host: str,
        local_path: str,
        remote_path: str,
        *,
        overwrite: bool,
    ) -> dict[str, Any]:
        result, reconnected = self._perform(
            host,
            lambda managed: self.adapters[managed.profile.transport].upload_path(
                managed.session,
                Path(local_path),
                remote_path,
                overwrite=overwrite,
            ),
        )
        return self._result_payload(result.to_dict(), reconnected)

    def download_path(
        self,
        host: str,
        remote_path: str,
        local_path: str,
        *,
        overwrite: bool,
    ) -> dict[str, Any]:
        result, reconnected = self._perform(
            host,
            lambda managed: self.adapters[managed.profile.transport].download_path(
                managed.session,
                remote_path,
                Path(local_path),
                overwrite=overwrite,
            ),
        )
        return self._result_payload(result.to_dict(), reconnected)

    def write_file(self, host: str, remote_path: str, content: str) -> dict[str, Any]:
        self._check_content(content)
        result, reconnected = self._perform(
            host,
            lambda managed: self.adapters[managed.profile.transport].write_file(
                managed.session,
                remote_path,
                content,
            ),
        )
        return self._result_payload(result, reconnected)

    def edit_file(
        self,
        host: str,
        remote_path: str,
        old_text: str,
        new_text: str,
        replace_all: bool,
    ) -> dict[str, Any]:
        if not old_text:
            raise ValueError("old_text must not be empty.")
        self._check_content(old_text, new_text)
        result, reconnected = self._perform(
            host,
            lambda managed: self.adapters[managed.profile.transport].edit_file(
                managed.session,
                remote_path,
                old_text,
                new_text,
                replace_all,
            ),
        )
        return self._result_payload(result, reconnected)

    def _perform(
        self,
        host: str,
        callback: Callable[[ManagedConnection], T],
    ) -> tuple[T, bool]:
        with self._host_guard(host):
            check_cancelled()
            managed, reconnected = self._ensure_connected(host)
            try:
                with managed.cancellation_lock:
                    managed.cancellation = operation_cancelled.get()
                check_cancelled()
                with self._condition:
                    managed.operations += 1
                result = callback(managed)
                check_cancelled()
            except Exception as exc:
                self._record("failures")
                if isinstance(exc, TimeoutError):
                    self._record("timeouts")
                adapter = self.adapters[managed.profile.transport]
                if isinstance(
                    exc, (ConnectionError, TimeoutError, EOFError, InterruptedError)
                ) or not adapter.is_open(managed.session):
                    self._close_connection(host, managed)
                # Reconnect lazily: failed operations can have side effects and must not be replayed.
                raise
            else:
                with self._condition:
                    managed.last_healthcheck_at = monotonic()
                return result, reconnected
            finally:
                with managed.cancellation_lock:
                    managed.cancellation = None
                with self._condition:
                    managed.last_used_at = monotonic()

    def _ensure_connected(self, host: str) -> tuple[ManagedConnection, bool]:
        managed = self._connection(host)
        if managed is not None:
            if self._connection_alive(managed):
                return managed, False
            self._close_connection(host, managed)
        saved = self._saved_profile(host)
        profile = saved or self._build_profile(host, None, None, None, None, None)
        managed = self._open_connection(profile, self.credential_store.get(host))
        try:
            check_cancelled()
            if saved is None:
                self.profile_store.save(profile)
        except BaseException:
            self._dispose(managed)
            raise
        self._register(managed)
        if saved is not None:
            self._record("reconnected")
        return managed, saved is not None

    def abort(self, host: str, cancelled: threading.Event) -> None:
        managed = self._connection(host)
        if managed is not None:
            with managed.cancellation_lock:
                if managed.cancellation is cancelled:
                    self.adapters[managed.profile.transport].close(managed.session)

    def _check_content(self, *values: str) -> None:
        if sum(map(len, values)) > self.config.max_content_chars:
            raise ValueError(
                f"Input exceeds {self.config.max_content_chars} characters; use upload for large files."
            )

    def _persist(
        self,
        profile: RemoteProfile,
        password: str | None,
        previous: RemoteProfile | None,
    ) -> None:
        previous_password: str | None = None
        if password is not None and previous is not None:
            previous_password = self.credential_store.get(profile.target).password
        try:
            if password is not None:
                self.credential_store.set_password(profile.target, password)
            self.profile_store.save(profile)
        except Exception:
            if password is not None:
                if previous_password is None:
                    self.credential_store.delete(profile.target)
                else:
                    self.credential_store.set_password(
                        profile.target, previous_password
                    )
            raise

    def _build_profile(
        self,
        host: str,
        username: str | None,
        transport: TransportName | None,
        port: int | None,
        use_ssl: bool | None,
        saved: RemoteProfile | None,
    ) -> RemoteProfile:
        ssh = lookup_ssh_config(host)
        windows = lookup_windows_credential(host)
        resolved_username = (
            username
            or (saved.username if saved else None)
            or (ssh.user if ssh else None)
            or (windows.username if windows else None)
        )
        if resolved_username is None:
            raise ValueError(
                "username is required unless supplied by ~/.ssh/config or Windows Credential Manager."
            )
        resolved_transport = (
            transport
            or (saved.transport if saved else None)
            or ("ssh" if ssh else "winrm")
        )
        resolved_ssl = (
            use_ssl if use_ssl is not None else (saved.use_ssl if saved else False)
        )
        if port is not None:
            resolved_port = port
        elif saved is not None and saved.transport == resolved_transport:
            resolved_port = saved.port
        elif resolved_transport == "ssh" and ssh is not None and ssh.port:
            resolved_port = ssh.port
        else:
            resolved_port = (
                22 if resolved_transport == "ssh" else (5986 if resolved_ssl else 5985)
            )
        resolved_host = saved.host if saved else (ssh.hostname if ssh else host)
        return RemoteProfile(
            target=host,
            host=resolved_host,
            username=resolved_username,
            transport=resolved_transport,
            port=resolved_port,
            use_ssl=resolved_ssl,
            connection_timeout=saved.connection_timeout if saved else 30,
            created_at=saved.created_at if saved else 0,
        )

    def _saved_profile(self, host: str) -> RemoteProfile | None:
        try:
            return self.profile_store.get(host)
        except KeyError:
            return None

    @staticmethod
    def _connection_payload(
        managed: ManagedConnection, *, reused: bool
    ) -> dict[str, Any]:
        return {
            "host": managed.profile.target,
            "transport": managed.profile.transport,
            "shell": managed.session.state.shell_type,
            "reused": reused,
        }

    @staticmethod
    def _result_payload(payload: dict[str, Any], reconnected: bool) -> dict[str, Any]:
        if reconnected:
            payload["reconnected"] = True
        return payload
