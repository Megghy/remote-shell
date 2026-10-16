"""Verify UTF-8 over a real WinRM connection using saved host credentials."""

from __future__ import annotations

import argparse
import base64
import json

from remote_shell_mcp.credential_store import CredentialStore
from remote_shell_mcp.profile_store import ProfileStore
from remote_shell_mcp.session_manager import SessionManager


def encoded_command(script: str) -> str:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return f"powershell.exe -NoProfile -NonInteractive -EncodedCommand {encoded}"


def verify(host: str) -> None:
    manager = SessionManager(ProfileStore(), CredentialStore())
    message = '错误:  关系 "liveinfo" 不存在，中文输出 😀'
    try:
        for _ in range(2):
            result = manager.execute(
                host,
                "[pscustomobject]@{ Major = $PSVersionTable.PSVersion.Major; "
                "Input = [Console]::InputEncoding.CodePage; "
                "Output = [Console]::OutputEncoding.CodePage; "
                "Pipeline = $OutputEncoding.CodePage } | ConvertTo-Json -Compress",
                30,
            )
            assert result["exit_code"] == 0, result
            assert json.loads(result["stdout"]) == {
                "Major": 7,
                "Input": 65001,
                "Output": 65001,
                "Pipeline": 65001,
            }, result
            result = manager.execute(
                host,
                encoded_command(
                    f"$bytes = [Text.Encoding]::UTF8.GetBytes('{message}'); "
                    "[Console]::OpenStandardOutput().Write($bytes, 0, $bytes.Length); "
                    "[Console]::OpenStandardError().Write($bytes, 0, $bytes.Length); "
                    "exit 7"
                ),
                30,
            )
            assert result["exit_code"] == 7, result
            assert result["stdout"] == result["stderr"] == message, result
            manager.close_all()
            manager = SessionManager(ProfileStore(), CredentialStore())

        result = manager.execute(
            host,
            "'中文输入 😀' | "
            + encoded_command(
                "[Console]::InputEncoding = [Text.UTF8Encoding]::new($false); "
                "$bytes = [Text.Encoding]::UTF8.GetBytes([Console]::In.ReadToEnd()); "
                "[Console]::OpenStandardOutput().Write($bytes, 0, $bytes.Length)"
            ),
            30,
        )
        assert result["exit_code"] == 0, result
        assert result["stdout"] == "中文输入 😀", result
        result = manager.execute(host, "Write-Output '中文输出'; throw '中文异常'", 30)
        assert result["exit_code"] == 1, result
        assert result.get("stdout") == "中文输出", result
        assert "中文异常" in result["stderr"], result
        print(
            f"PASS: {host}: PowerShell 7, UTF-8 stdin/stdout/stderr, exit codes, reconnect"
        )
    finally:
        manager.close_all()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", help="Windows host with saved credentials")
    verify(parser.parse_args().host)
