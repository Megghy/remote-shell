import unittest
from unittest.mock import Mock, patch

from remote_shell_mcp.models import RemoteProfile, SessionState
from remote_shell_mcp.transports.output import BoundedOutput, MarkedOutput
from remote_shell_mcp.transports.ssh import SSHTransportAdapter, SSHTransportSession


class OutputTests(unittest.TestCase):
    def test_short_utf8_output(self):
        buf = BoundedOutput()
        for byte in "中文正常".encode():
            buf.append(bytes([byte]))
        self.assertEqual(buf.text(), "中文正常")

    def test_large_line_keeps_head_tail_and_byte_count(self):
        buf = BoundedOutput(limit=100)
        buf.append(b"HEAD")
        for _ in range(10000):
            buf.append(b"x" * 4096)
            self.assertLessEqual(len(buf.head) + len(buf.tail), 100)
        buf.append(b"TAIL")
        self.assertTrue(buf.text().startswith("HEAD"))
        self.assertTrue(buf.text().endswith("TAIL"))
        self.assertIn("40960008 total bytes", buf.text())

    def test_stdout_marker_split_at_every_boundary(self):
        marker = b"__RS_STATUS_123__"
        content = b"hello\n" + marker + b"7__/tmp/a b\r\n"
        for split in range(len(content) + 1):
            with self.subTest(split=split):
                stream = MarkedOutput(marker, status=True)
                stream.feed(content[:split])
                stream.feed(content[split:])
                self.assertTrue(stream.done)
                self.assertEqual(stream.code, 7)
                self.assertEqual(stream.cwd, "/tmp/a b")
                self.assertEqual(stream.output.text(), "hello")

    def test_large_output_before_status_marker(self):
        stream = MarkedOutput(b"__STATUS__", status=True)
        for _ in range(2000):
            stream.feed(b"x" * 4096)
        for byte in b"__STATUS__0__/work\n":
            stream.feed(bytes([byte]))
        self.assertTrue(stream.done)
        self.assertEqual(stream.cwd, "/work")
        self.assertIn("bytes omitted", stream.output.text())
        self.assertLess(len(stream.output.text()), 263000)

    def test_stderr_marker_and_stdout_marker_are_independent(self):
        out = MarkedOutput(b"OUT", status=True)
        err = MarkedOutput(b"ERR")
        out.feed(b"oneO")
        err.feed(b"twoE")
        out.feed(b"UT0__/cwd")
        err.feed(b"RRignored")
        self.assertTrue(err.done)
        self.assertFalse(out.done)
        out.feed(b"\n")
        self.assertEqual(out.output.text(), "one")
        self.assertEqual(err.output.text(), "two")
        self.assertTrue(out.done)

    def test_oversized_status_fails_without_growing_forever(self):
        stream = MarkedOutput(b"MARK", status=True)
        with self.assertRaises(ConnectionError):
            stream.feed(b"MARK0__" + b"a" * 65536)

    def test_ssh_timeout_closes_channel(self):
        channel = Mock()
        session = SSHTransportSession(
            RemoteProfile("a", "a", "u", "ssh", 22),
            SessionState("ssh", "a", "bash", "linux"),
            Mock(),
            channel,
        )
        adapter = SSHTransportAdapter()
        with (
            patch.object(adapter, "_session_open", return_value=True),
            self.assertRaises(TimeoutError),
        ):
            adapter._execute_internal(session, "long-running", 0)
        channel.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
