"""Verify WinRM lifecycle, file I/O and cancellation using saved credentials."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import uuid
from pathlib import Path
from typing import cast
from unittest.mock import patch

from remote_shell_mcp import server
from remote_shell_mcp.config import RuntimeConfig
from remote_shell_mcp.credential_store import CredentialStore, lookup_windows_credential
from remote_shell_mcp.models import RemoteProfile
from remote_shell_mcp.profile_store import ProfileStore
from remote_shell_mcp.session_manager import SessionManager
from remote_shell_mcp.transports.winrm import WinRMTransportSession


def remote_shell_ids(manager: SessionManager, host: str) -> set[str]:
    result = manager.execute(
        host,
        "ConvertTo-Json -InputObject @(Get-WSManInstance -ResourceURI shell -Enumerate | ForEach-Object ShellId) -Compress",
        30,
    )
    assert result["exit_code"] == 0, result
    return {value.casefold() for value in json.loads(result["stdout"])}


def verify_files(manager: SessionManager, host: str, directory: Path) -> None:
    root = manager.execute(host, "[IO.Path]::GetTempPath()", 30)["stdout"]
    remote = root.rstrip("\\/") + "/remote-shell-verify-" + uuid.uuid4().hex
    try:
        text_path = remote + "/中文.txt"
        manager.write_file(host, text_path, "中文-unique-value")
        edited = manager.edit_file(host, text_path, "unique", "verified", False)
        assert edited["replacements"] == 1, edited
        result = manager.execute(
            host, f"Get-Content -LiteralPath '{text_path}' -Raw", 30
        )
        assert result["stdout"] == "中文-verified-value", result
        local = directory / "data.bin"
        local.write_bytes(bytes(range(256)) * 64)
        uploaded = manager.upload_path(
            host, str(local), remote + "/data.bin", overwrite=False
        )
        assert uploaded["bytes"] == local.stat().st_size, uploaded
        downloaded = directory / "received.bin"
        manager.download_path(
            host, remote + "/data.bin", str(downloaded), overwrite=False
        )
        assert downloaded.read_bytes() == local.read_bytes()
    finally:
        manager.execute(
            host, f"Remove-Item -LiteralPath '{remote}' -Recurse -Force", 30
        )


async def verify_cancel(manager: SessionManager, host: str) -> None:
    with patch.object(server, "session_manager", manager):
        task = asyncio.create_task(
            server._invoke(manager.execute, host, "Start-Sleep -Seconds 60", 90)
        )
        await asyncio.sleep(0.5)
        managed = manager.connections[host]
        session_id = cast(WinRMTransportSession, managed.session).remote_session_id
        assert session_id is not None
        task.cancel()
        try:
            await asyncio.wait_for(
                task, manager.config.driver_close_timeout_seconds + 5
            )
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("Cancellation did not propagate")
        assert manager.status()["reserved_connections"] == 0, manager.status()
        assert session_id.casefold() not in remote_shell_ids(manager, host)


def verify(host: str) -> None:
    windows = lookup_windows_credential(host)
    profile = (
        RemoteProfile(host, host, windows.username, "winrm", 5985)
        if windows
        else ProfileStore().get(host)
    )
    assert profile.transport == "winrm", "This verification script requires WinRM."
    with tempfile.TemporaryDirectory() as directory:
        store = ProfileStore(Path(directory) / "profiles.json")
        store.save(profile)
        manager = SessionManager(store, CredentialStore(), RuntimeConfig())
        try:
            print("[1/5] Testing basic variable persistence...", flush=True)
            result = manager.execute(host, "$rs_regular_value=42; 'ready'", 30)
            assert result["exit_code"] == 0, result
            assert manager.execute(host, "$rs_regular_value", 30)["stdout"] == "42"
            print("[2/5] Testing file write, edit, upload and download...", flush=True)
            verify_files(manager, host, Path(directory))
            print("[3/5] Testing repeated timeouts...", flush=True)
            for idx in range(2):
                print(f"  Timeout test {idx+1}/2...", flush=True)
                manager.execute(host, "'warmup'", 30)
                session_id = cast(
                    WinRMTransportSession, manager.connections[host].session
                ).remote_session_id
                assert session_id is not None
                try:
                    manager.execute(host, "Start-Sleep -Seconds 60", 0.5)
                except TimeoutError:
                    pass
                else:
                    raise AssertionError("Timeout did not propagate")
                assert manager.status()["reserved_connections"] == 0
                remaining = remote_shell_ids(manager, host)
                assert session_id.casefold() not in remaining, (
                    session_id,
                    remaining,
                    manager.status(),
                )
            print("[4/5] Testing cancellation...", flush=True)
            asyncio.run(verify_cancel(manager, host))
            print("[5/5] Verification completed successfully!", flush=True)
            print(
                "PASS:",
                host,
                "variables, write/edit, binary upload/download, 3 timeouts, cancellation",
            )
            print(json.dumps(manager.status(), ensure_ascii=False))
        finally:
            manager.close_all()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", help="Windows host with saved credentials")
    verify(parser.parse_args().host)
