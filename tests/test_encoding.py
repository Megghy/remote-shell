from __future__ import annotations

import unittest

from remote_shell_mcp.encoding import decode_output, repair_text
from remote_shell_mcp.models import ExecResult

WSL_TEXT = (
    "无法连接到远程主机\r\n"
    "Error code: Wsl/Service/WSAETIMEDOUT\r\n"
    "https://github.com/microsoft/WSL/issues/10738\n"
)


class DecodeOutputTests(unittest.TestCase):
    def test_utf8_passthrough(self) -> None:
        text = "中文输出 😀\nclickhouse-client"
        self.assertEqual(decode_output(text.encode("utf-8")), text)

    def test_empty(self) -> None:
        self.assertEqual(decode_output(b""), "")

    def test_utf16_le_wsl_error(self) -> None:
        self.assertEqual(decode_output(WSL_TEXT.encode("utf-16-le")), WSL_TEXT)

    def test_utf16_le_bom(self) -> None:
        self.assertEqual(
            decode_output(b"\xff\xfe" + "Wsl/Service/WSAETIMEDOUT".encode("utf-16-le")),
            "Wsl/Service/WSAETIMEDOUT",
        )

    def test_mixed_utf8_prefix_even(self) -> None:
        mixed = b"ok\r\n" + "Wsl/Service/WSAETIMEDOUT".encode("utf-16-le")
        self.assertEqual(decode_output(mixed), "ok\r\nWsl/Service/WSAETIMEDOUT")

    def test_mixed_utf8_prefix_odd(self) -> None:
        mixed = b"ok\n" + "Wsl/Service/WSAETIMEDOUT".encode("utf-16-le")
        self.assertEqual(decode_output(mixed), "ok\nWsl/Service/WSAETIMEDOUT")

    def test_short_nul_run_untouched(self) -> None:
        data = b"A\x00B\x00C\x00D\x00"
        self.assertEqual(decode_output(data), data.decode("utf-8"))

    def test_binary_without_ascii_nul_run(self) -> None:
        data = bytes(range(256)) * 2
        self.assertNotIn("Wsl/Service", decode_output(data))


class RepairTextTests(unittest.TestCase):
    def test_utf8_misdecoded_wsl_ascii(self) -> None:
        raw = "Wsl/Service/WSAETIMEDOUT\nhttps://github.com/microsoft/WSL".encode(
            "utf-16-le"
        )
        mojibake = raw.decode("utf-8", errors="replace")
        repaired = repair_text(mojibake)
        self.assertIn("Wsl/Service/WSAETIMEDOUT", repaired)
        self.assertIn("https://github.com/microsoft/WSL", repaired)
        self.assertNotIn("\x00", repaired)

    def test_utf8_replace_recovers_english_from_chinese_wsl(self) -> None:
        mojibake = WSL_TEXT.encode("utf-16-le").decode("utf-8", errors="replace")
        repaired = repair_text(mojibake)
        self.assertIn("Wsl/Service/WSAETIMEDOUT", repaired)
        self.assertIn("https://github.com/microsoft/WSL/issues/10738", repaired)

    def test_latin1_roundtrip_recovers_chinese(self) -> None:
        raw = WSL_TEXT.encode("utf-16-le")
        mojibake = raw.decode("latin-1")
        self.assertEqual(repair_text(mojibake), WSL_TEXT)

    def test_plain_utf8_unchanged(self) -> None:
        self.assertEqual(repair_text("中文输出 😀"), "中文输出 😀")

    def test_exec_result_repairs_stdout(self) -> None:
        raw = "Wsl/Service/WSAETIMEDOUT".encode("utf-16-le").decode("utf-8")
        result = ExecResult(duration_ms=1, exit_code=-1, stdout=raw)
        self.assertEqual(result.stdout, "Wsl/Service/WSAETIMEDOUT")
        self.assertEqual(result.to_dict()["stdout"], "Wsl/Service/WSAETIMEDOUT")


if __name__ == "__main__":
    unittest.main()
