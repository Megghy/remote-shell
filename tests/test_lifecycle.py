import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from remote_shell_mcp.config import RuntimeConfig
from remote_shell_mcp.connection_pool import ConnectionPool
from remote_shell_mcp.models import (
    CredentialSecret,
    ExecResult,
    RemoteProfile,
    SessionState,
)
from remote_shell_mcp.profile_store import ProfileStore
from remote_shell_mcp.session_manager import SessionManager
from remote_shell_mcp.transports.base import BaseTransportSession


def profile(host="a", endpoint=None):
    return RemoteProfile(host, endpoint or host, "user", "winrm", 5985)


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patch = patch(
            "remote_shell_mcp.connection_pool.DATA_DIR", Path(self.temp.name)
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.adapter = Mock()
        self.adapter.open_session.side_effect = lambda p, s: BaseTransportSession(
            p, SessionState("winrm", p.host, "powershell", "windows")
        )
        self.adapter.is_alive.return_value = True

    def pool(self, **settings):
        config = RuntimeConfig(connection_wait_seconds=0.1, **settings)
        pool = ConnectionPool(config, {"winrm": self.adapter})
        self.addCleanup(pool.close_all)
        return pool

    def open(self, pool, host="a", endpoint=None):
        with pool._host_guard(host):
            connection = pool._open_connection(
                profile(host, endpoint), CredentialSecret()
            )
            pool._register(connection)
        return connection

    def test_lru_replaces_free_connection_at_capacity(self):
        pool = self.pool(max_connections=2)
        self.open(pool, "a")
        self.open(pool, "b")
        self.open(pool, "c")
        self.assertEqual(set(pool.connections), {"b", "c"})
        self.assertEqual(pool.status()["metrics"]["evicted"], 1)
        self.assertEqual(pool.status()["reserved_connections"], 2)

    def test_busy_connection_is_not_evicted(self):
        pool = self.pool(max_connections=1)
        self.open(pool)
        with pool._host_guard("a"), self.assertRaises(ConnectionError):
            self.open(pool, "b")
        self.assertEqual(set(pool.connections), {"a"})
        self.assertEqual(set(pool._host_locks), set())

    def test_background_reaper_closes_without_new_requests(self):
        pool = self.pool(idle_timeout_seconds=0.06)
        closed = threading.Event()
        self.adapter.close.side_effect = lambda session: closed.set()
        self.open(pool)
        self.assertTrue(closed.wait(1))
        with pool._condition:
            self.assertTrue(
                pool._condition.wait_for(lambda: pool._allocated == 0, timeout=1)
            )
        self.assertEqual(pool._host_locks, {})

    def test_endpoint_limit_is_shared_by_independent_pools(self):
        a = self.pool(max_endpoint_connections=1)
        b = self.pool(max_endpoint_connections=1)
        first = self.open(a, "alias-a", "same-host")
        with self.assertRaises(ConnectionError):
            self.open(b, "alias-b", "same-host")
        self.assertEqual(b.status()["reserved_connections"], 0)
        a._close_connection("alias-a", first)
        self.open(b, "alias-b", "same-host")

    def test_open_failure_releases_both_budgets(self):
        pool = self.pool(max_connections=1, max_endpoint_connections=1)
        self.adapter.open_session.side_effect = RuntimeError("open failed")
        with self.assertRaisesRegex(RuntimeError, "open failed"):
            self.open(pool)
        self.assertEqual(pool.status()["reserved_connections"], 0)
        self.adapter.open_session.side_effect = lambda p, s: BaseTransportSession(
            p, SessionState("winrm", p.host, "powershell", "windows")
        )
        self.open(pool)

    def test_concurrent_host_calls_serialize_and_locks_retire(self):
        pool = self.pool()
        state = {"active": 0, "peak": 0}

        def operation(_):
            with pool._host_guard("a"):
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
                time.sleep(0.002)
                state["active"] -= 1

        with ThreadPoolExecutor(max_workers=12) as executor:
            list(executor.map(operation, range(60)))
        self.assertEqual(state["peak"], 1)
        self.assertEqual(pool._host_locks, {})
        self.assertEqual(pool._host_refs, {})

    def test_healthcheck_is_cached(self):
        pool = self.pool()
        managed = self.open(pool)
        for _ in range(20):
            self.assertTrue(pool._connection_alive(managed))
        self.adapter.is_alive.assert_not_called()
        managed.last_healthcheck_at = 0
        pool._connection_alive(managed)
        pool._connection_alive(managed)
        self.adapter.is_alive.assert_called_once()

    def test_shutdown_disallows_new_calls(self):
        pool = self.pool()
        self.open(pool)
        pool.close_all()
        with self.assertRaises(RuntimeError):
            self.open(pool, "b")
        self.assertEqual(pool.status()["reserved_connections"], 0)

    def test_timeout_does_not_replay_operation(self):
        store = ProfileStore(Path(self.temp.name) / "profiles.json")
        store.save(profile())
        credentials = Mock()
        credentials.get.return_value = CredentialSecret("unused")
        manager = SessionManager(store, credentials, RuntimeConfig())
        manager.adapters["winrm"] = self.adapter
        self.addCleanup(manager.close_all)
        self.adapter.execute.side_effect = TimeoutError("expired")
        with self.assertRaises(TimeoutError):
            manager.execute("a", "side-effect", 1)
        self.adapter.execute.assert_called_once()
        self.assertEqual(manager.status()["reserved_connections"], 0)
        self.adapter.execute.side_effect = None
        self.adapter.execute.return_value = ExecResult(1, 0, "ok")
        self.assertEqual(manager.execute("a", "later", 1)["stdout"], "ok")
        self.assertEqual(self.adapter.open_session.call_count, 2)

    def test_content_limit_rejects_before_connect(self):
        manager = SessionManager(Mock(), Mock(), RuntimeConfig(max_content_chars=5))
        self.addCleanup(manager.close_all)
        with self.assertRaises(ValueError):
            manager.execute("a", "123456", 1)
        self.assertEqual(manager.status()["active_connections"], 0)


class ConfigTests(unittest.TestCase):
    def test_default_types_and_values(self):
        with patch.dict(os.environ, {}, clear=True):
            config = RuntimeConfig.from_env()
        self.assertEqual(config, RuntimeConfig())
        self.assertIsInstance(config.max_connections, int)

    def test_env_values_and_invalid_values(self):
        with patch.dict(os.environ, {"REMOTE_SHELL_MAX_CONNECTIONS": "12"}):
            self.assertEqual(RuntimeConfig.from_env().max_connections, 12)
        for value in ["0", "-1", "nan", "inf"]:
            with (
                self.subTest(value=value),
                patch.dict(os.environ, {"REMOTE_SHELL_CONNECTION_WAIT_SECONDS": value}),
                self.assertRaises(ValueError),
            ):
                RuntimeConfig.from_env()


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from remote_shell_mcp import server

        self.server = server
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        lease_patch = patch(
            "remote_shell_mcp.connection_pool.DATA_DIR", Path(self.temp.name)
        )
        lease_patch.start()
        self.addCleanup(lease_patch.stop)
        store = ProfileStore(Path(self.temp.name) / "profiles.json")
        store.save(profile())
        credentials = Mock()
        credentials.get.return_value = CredentialSecret()
        self.manager = SessionManager(store, credentials, RuntimeConfig())
        self.adapter = Mock()
        self.adapter.open_session.side_effect = lambda p, s: BaseTransportSession(
            p, SessionState("winrm", p.host, "powershell", "windows")
        )
        self.manager.adapters["winrm"] = self.adapter
        server_patch = patch.object(server, "session_manager", self.manager)
        server_patch.start()
        self.addCleanup(server_patch.stop)
        self.started, self.finish = threading.Event(), threading.Event()
        self.adapter.close.side_effect = lambda _: self.finish.set()

    def tearDown(self):
        self.finish.set()
        self.manager.close_all()

    async def test_cancelling_queued_host_call_does_not_abort_active_call(self):
        import asyncio

        def execute(*_):
            self.started.set()
            self.finish.wait(3)
            return ExecResult(1, 0, "done")

        self.adapter.execute.side_effect = execute
        active = asyncio.create_task(
            self.server._invoke(self.manager.execute, "a", "active", 5)
        )
        self.assertTrue(await asyncio.to_thread(self.started.wait, 1))
        queued = asyncio.create_task(
            self.server._invoke(self.manager.execute, "a", "queued", 5)
        )
        await asyncio.sleep(0)
        queued.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(queued, 1)
        self.adapter.close.assert_not_called()
        self.assertFalse(active.done())
        self.finish.set()
        self.assertEqual((await active)["stdout"], "done")
        self.adapter.execute.assert_called_once()

    async def test_cancelling_active_call_closes_and_releases_connection(self):
        import asyncio

        def execute(*_):
            self.started.set()
            self.finish.wait(3)
            raise ConnectionError("closed")

        self.adapter.execute.side_effect = execute
        task = asyncio.create_task(
            self.server._invoke(self.manager.execute, "a", "active", 5)
        )
        self.assertTrue(await asyncio.to_thread(self.started.wait, 1))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        self.assertTrue(self.finish.is_set())
        self.assertEqual(self.manager.status()["reserved_connections"], 0)
        self.assertEqual(self.manager._host_refs, {})

    async def test_anyio_scope_cancellation_waits_for_transport_cleanup(self):
        import anyio
        from anyio.to_thread import run_sync

        def execute(*_):
            self.started.set()
            self.finish.wait(3)
            raise ConnectionError("closed")

        self.adapter.execute.side_effect = execute
        async with anyio.create_task_group() as group:
            group.start_soon(
                self.server._invoke, self.manager.execute, "a", "active", 5
            )
            self.assertTrue(await run_sync(self.started.wait, 1))
            group.cancel_scope.cancel()
        self.assertTrue(self.finish.is_set())
        self.assertEqual(self.manager.status()["reserved_connections"], 0)
        self.assertEqual(self.manager._host_refs, {})


    async def test_cancellation_runs_when_many_requests_are_waiting(self):
        import asyncio

        def execute(*_):
            self.started.set()
            self.finish.wait(3)
            raise ConnectionError("closed")

        self.adapter.execute.side_effect = execute
        active = asyncio.create_task(self.server._invoke(self.manager.execute, "a", "active", 5))
        self.assertTrue(await asyncio.to_thread(self.started.wait, 1))
        queued = [asyncio.create_task(self.server._invoke(self.manager.execute, "a", "queued", 5)) for _ in range(40)]
        await asyncio.sleep(0.02)
        active.cancel()
        for task in queued:
            task.cancel()
        try:
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(active, 1)
            self.assertTrue(self.finish.is_set())
        finally:
            self.finish.set()
            await asyncio.gather(*queued, return_exceptions=True)


class ShutdownTests(unittest.TestCase):
    def test_shutdown_closes_busy_transport_before_waiting_for_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProfileStore(Path(directory) / "profiles.json")
            store.save(profile())
            credentials = Mock()
            credentials.get.return_value = CredentialSecret()
            manager = SessionManager(store, credentials, RuntimeConfig())
            adapter = Mock()
            adapter.open_session.side_effect = lambda p, s: BaseTransportSession(
                p, SessionState("winrm", p.host, "powershell", "windows")
            )
            started, stopped = threading.Event(), threading.Event()

            def execute(*_):
                started.set()
                stopped.wait(3)
                raise ConnectionError("closed")

            adapter.execute.side_effect = execute
            adapter.close.side_effect = lambda _: stopped.set()
            manager.adapters["winrm"] = adapter
            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    work = executor.submit(manager.execute, "a", "long-running", 5)
                    self.assertTrue(started.wait(1))
                    executor.submit(manager.close_all).result(timeout=1)
                    with self.assertRaises(ConnectionError):
                        work.result(timeout=1)
                self.assertEqual(manager.status()["reserved_connections"], 0)
            finally:
                stopped.set()
                manager.close_all()


class WorkerPoolTests(unittest.TestCase):
    def test_cancelled_queued_future_never_starts_remote_work(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProfileStore(Path(directory) / "profiles.json")
            store.save(profile())
            credentials = Mock()
            credentials.get.return_value = CredentialSecret()
            manager = SessionManager(
                store, credentials, RuntimeConfig(max_connections=1)
            )
            adapter = Mock()
            adapter.open_session.side_effect = lambda p, s: BaseTransportSession(
                p, SessionState("winrm", p.host, "powershell", "windows")
            )
            started, finish = threading.Event(), threading.Event()

            def execute(*_):
                started.set()
                finish.wait(3)
                return ExecResult(0, 0, "done")

            adapter.execute.side_effect = execute
            manager.adapters["winrm"] = adapter
            try:
                first = manager.submit(manager.execute, "a", "first", 5)
                self.assertTrue(started.wait(1))
                second = manager.submit(manager.execute, "a", "second", 5)
                self.assertTrue(second.cancel())
                finish.set()
                self.assertEqual(first.result(timeout=1)["stdout"], "done")
                self.assertEqual(adapter.execute.call_count, 1)
            finally:
                finish.set()
                manager.close_all()


if __name__ == "__main__":
    unittest.main()
