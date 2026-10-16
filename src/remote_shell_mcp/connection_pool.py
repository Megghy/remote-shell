from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import monotonic
from typing import Any

from filelock import FileLock, Timeout

from .config import RuntimeConfig
from .models import CredentialSecret, RemoteProfile, TransportName
from .profile_store import DATA_DIR
from .transports.base import BaseTransportSession, TransportAdapter

logger = logging.getLogger(__name__)
operation_cancelled: ContextVar[threading.Event | None] = ContextVar(
    "operation_cancelled", default=None
)


def check_cancelled() -> None:
    event = operation_cancelled.get()
    if event is not None and event.is_set():
        raise InterruptedError("Remote operation was cancelled; it was not retried.")


@dataclass(slots=True)
class ManagedConnection:
    profile: RemoteProfile
    session: BaseTransportSession
    lease: FileLock
    opened_at: float = field(default_factory=monotonic)
    last_used_at: float = field(default_factory=monotonic)
    last_healthcheck_at: float = field(default_factory=monotonic)
    operations: int = 0
    cancellation: threading.Event | None = None
    cancellation_lock: threading.Lock = field(default_factory=threading.Lock)


class ConnectionPool:
    def __init__(
        self, config: RuntimeConfig, adapters: dict[TransportName, TransportAdapter]
    ) -> None:
        self.config = config
        self.adapters = adapters
        self.connections: dict[str, ManagedConnection] = {}
        self._condition = threading.Condition(threading.RLock())
        self._host_locks: dict[str, threading.RLock] = {}
        self._host_refs: dict[str, int] = {}
        self._allocated = 0
        self._closed = False
        self._metrics: dict[str, int] = dict.fromkeys(
            (
                "opened",
                "closed",
                "evicted",
                "reconnected",
                "failures",
                "timeouts",
                "cleanup_failures",
                "forced_terminations",
            ),
            0,
        )
        self._stop = threading.Event()
        self._reaper = threading.Thread(
            target=self._reap, name="remote-shell-idle-reaper", daemon=True
        )
        self._reaper.start()

    @contextmanager
    def _host_guard(self, host: str) -> Iterator[None]:
        with self._condition:
            if self._closed:
                raise RuntimeError("Remote-shell connection pool is closed.")
            lock = self._host_locks.setdefault(host, threading.RLock())
            self._host_refs[host] = self._host_refs.get(host, 0) + 1
        try:
            while not lock.acquire(timeout=0.1):
                check_cancelled()
                if self._stop.is_set():
                    raise RuntimeError("Remote-shell connection pool is closed.")
            try:
                with self._condition:
                    if self._closed:
                        raise RuntimeError("Remote-shell connection pool is closed.")
                check_cancelled()
                yield
            finally:
                lock.release()
        finally:
            with self._condition:
                self._host_refs[host] -= 1
                if self._host_refs[host] == 0:
                    del self._host_refs[host]
                    del self._host_locks[host]
                self._condition.notify_all()

    def _connection(self, host: str) -> ManagedConnection | None:
        with self._condition:
            return self.connections.get(host)

    def _open_connection(
        self, profile: RemoteProfile, secret: CredentialSecret
    ) -> ManagedConnection:
        self._reserve()
        lease = None
        try:
            lease = self._endpoint_lease(profile)
            session = self.adapters[profile.transport].open_session(profile, secret)
        except BaseException:
            if lease is not None:
                lease.release()
            self._release()
            raise
        self._record("opened")
        return ManagedConnection(profile, session, lease)

    def _register(self, managed: ManagedConnection) -> None:
        with self._condition:
            if not self._closed:
                self.connections[managed.profile.target] = managed
                return
        self._dispose(managed)
        raise RuntimeError("Remote-shell connection pool is closed.")

    def _reserve(self) -> None:
        deadline = monotonic() + self.config.connection_wait_seconds
        while True:
            with self._condition:
                check_cancelled()
                if self._closed:
                    raise RuntimeError("Remote-shell connection pool is closed.")
                if self._allocated < self.config.max_connections:
                    self._allocated += 1
                    return
                idle = self._pop_idle(force=True)
                if idle is None:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise ConnectionError(
                            f"All {self.config.max_connections} local connections are busy."
                        )
                    self._condition.wait(min(remaining, 0.1))
                    continue
            self._dispose(idle, evicted=True)

    def _endpoint_lease(self, profile: RemoteProfile) -> FileLock:
        # OS locks survive thread handoff and are released when the process exits.
        identity = json.dumps(
            [
                profile.transport,
                profile.host.casefold().rstrip("."),
                profile.port,
                profile.use_ssl,
                profile.username.casefold(),
            ]
        )
        directory = DATA_DIR / "leases" / hashlib.sha256(identity.encode()).hexdigest()
        directory.mkdir(parents=True, exist_ok=True)
        deadline = monotonic() + self.config.connection_wait_seconds
        while True:
            check_cancelled()
            for slot in range(self.config.max_endpoint_connections):
                lease = FileLock(directory / f"{slot}.lock", thread_local=False)
                try:
                    lease.acquire(timeout=0)
                    return lease
                except Timeout:
                    continue
            if monotonic() >= deadline:
                raise ConnectionError(
                    f"All {self.config.max_endpoint_connections} local process slots "
                    f"for {profile.host}:{profile.port}/{profile.username} are occupied."
                )
            if self._stop.wait(min(0.1, max(0, deadline - monotonic()))):
                raise RuntimeError("Remote-shell connection pool is closed.")

    def _close_connection(self, host: str, managed: ManagedConnection) -> None:
        with self._condition:
            if self.connections.get(host) is managed:
                del self.connections[host]
        self._dispose(managed)

    def _dispose(self, managed: ManagedConnection, *, evicted: bool = False) -> None:
        try:
            self.adapters[managed.profile.transport].close(managed.session)
        except Exception:
            self._record("cleanup_failures")
            raise
        finally:
            self._record_forced_terminations(managed)
            managed.lease.release()
            self._release()
            self._record("closed")
            if evicted:
                self._record("evicted")

    def _record_forced_terminations(self, managed: ManagedConnection) -> None:
        with self._condition:
            self._metrics["forced_terminations"] += getattr(
                managed.session, "forced_terminations", 0
            )

    def _release(self) -> None:
        with self._condition:
            self._allocated -= 1
            self._condition.notify_all()

    def _pop_idle(self, *, force: bool = False) -> ManagedConnection | None:
        candidates = (
            managed
            for host, managed in self.connections.items()
            if host not in self._host_refs
            and (
                force
                or monotonic() - managed.last_used_at
                >= self.config.idle_timeout_seconds
            )
        )
        oldest = min(candidates, key=lambda managed: managed.last_used_at, default=None)
        if oldest is not None:
            self.connections.pop(oldest.profile.target)
        return oldest

    def evict_idle(self) -> None:
        while True:
            with self._condition:
                managed = self._pop_idle()
            if managed is None:
                return
            self._dispose(managed, evicted=True)

    def _reap(self) -> None:
        interval = min(30.0, self.config.idle_timeout_seconds / 2)
        while not self._stop.wait(interval):
            try:
                self.evict_idle()
            except Exception:
                logger.exception("Idle remote-shell connection cleanup failed")

    def _record(self, metric: str) -> None:
        with self._condition:
            self._metrics[metric] += 1

    def _connection_alive(self, managed: ManagedConnection) -> bool:
        if not self.adapters[managed.profile.transport].is_open(managed.session):
            return False
        if (
            monotonic() - managed.last_healthcheck_at
            < self.config.healthcheck_interval_seconds
        ):
            return True
        alive = self.adapters[managed.profile.transport].is_alive(managed.session)
        if alive:
            managed.last_healthcheck_at = monotonic()
        return alive

    def status(self) -> dict[str, Any]:
        now = monotonic()
        with self._condition:
            return {
                "active_connections": len(self.connections),
                "reserved_connections": self._allocated,
                "max_connections": self.config.max_connections,
                "max_endpoint_connections": self.config.max_endpoint_connections,
                "idle_timeout_seconds": self.config.idle_timeout_seconds,
                "metrics": dict(self._metrics),
                "connections": [
                    {
                        "host": host,
                        "transport": managed.profile.transport,
                        "age_seconds": round(now - managed.opened_at, 1),
                        "idle_seconds": round(now - managed.last_used_at, 1),
                        "busy": host in self._host_refs,
                        "operations": managed.operations,
                        "driver_pid": getattr(
                            getattr(managed.session, "process", None), "pid", None
                        ),
                    }
                    for host, managed in sorted(self.connections.items())
                ],
            }

    def close_all(self) -> None:
        self._stop.set()
        with self._condition:
            self._closed = True
            self._condition.notify_all()
            busy = [
                managed
                for host, managed in self.connections.items()
                if host in self._host_refs
            ]
        errors = []
        for connection in busy:
            try:
                self.adapters[connection.profile.transport].close(connection.session)
            except Exception as exc:  # noqa: BLE001 - close every connection before raising all errors
                errors.append(exc)
        with self._condition:
            while self._host_refs:
                self._condition.wait()
            managed = list(self.connections.values())
            self.connections.clear()
        self._reaper.join()
        for connection in managed:
            try:
                self._dispose(connection)
            except Exception as exc:  # noqa: BLE001 - close every connection before raising all errors
                errors.append(exc)
        if errors:
            raise ExceptionGroup("Remote-shell connection cleanup failed", errors)
