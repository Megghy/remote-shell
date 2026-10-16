import io
import json
import queue
import subprocess
import unittest
from unittest.mock import Mock

from remote_shell_mcp.config import RuntimeConfig
from remote_shell_mcp.models import RemoteProfile, SessionState
from remote_shell_mcp.transports.winrm import (
    MARKER,
    WinRMTransportAdapter,
    WinRMTransportSession,
)


class DriverTests(unittest.TestCase):
    def session(self):
        process = Mock()
        process.stdin = io.StringIO()
        process.stdout = io.StringIO()
        process.stderr = io.StringIO()
        session = WinRMTransportSession(
            RemoteProfile("host", "host", "user", "winrm", 5985),
            SessionState("winrm", "host", "powershell", "windows"),
            process,
        )
        return session, process

    def test_lifecycle_messages_do_not_enter_request_response_queue(self):
        session, process = self.session()
        response = {"id": 3, "type": "result", "success": True}
        event = {"type": "lifecycle", "phase": "removed", "at_ms": 1234}
        process.stdout = io.StringIO(
            MARKER + json.dumps(event) + "\n" + MARKER + json.dumps(response) + "\n"
        )
        WinRMTransportAdapter._read_stdout(session)
        self.assertEqual(session.responses.get_nowait(), response)
        self.assertIsNone(session.responses.get_nowait())
        self.assertEqual(list(session.cleanup_events), [event])
        with self.assertRaises(queue.Empty):
            session.responses.get_nowait()

    def test_graceful_close_uses_configured_timeout_and_is_idempotent(self):
        session, process = self.session()
        process.poll.side_effect = [None, 0]
        adapter = WinRMTransportAdapter(RuntimeConfig(driver_close_timeout_seconds=27))
        adapter.close(session)
        adapter.close(session)
        self.assertEqual(process.wait.call_args_list[0].kwargs["timeout"], 27)
        process.kill.assert_not_called()
        self.assertTrue(session.closed)
        self.assertEqual(session.forced_terminations, 0)

    def test_forced_close_records_failure_and_still_reaps_process(self):
        session, process = self.session()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("pwsh", 1), 0]
        adapter = WinRMTransportAdapter(RuntimeConfig(driver_close_timeout_seconds=1))
        with self.assertLogs("remote_shell_mcp.transports.winrm", level="WARNING"):
            adapter.close(session)
        self.assertEqual(session.forced_terminations, 1)
        process.kill.assert_called_once()
        self.assertTrue(session.closed)


if __name__ == "__main__":
    unittest.main()
