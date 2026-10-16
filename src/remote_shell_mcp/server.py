from __future__ import annotations

import asyncio
import logging
import re
import threading
from pathlib import Path
from typing import Annotated, Any

from anyio import CancelScope
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .config import RuntimeConfig
from .connection_pool import operation_cancelled
from .credential_store import CredentialStore
from .models import TransportName
from .profile_store import ProfileStore
from .session_manager import SessionManager

mcp = FastMCP(
    "remote-shell",
    instructions="Call run(host) directly when the host is in ~/.ssh/config or Windows Credential Manager. Otherwise connect once. Connections are reused automatically.",
    log_level="WARNING",
)

profile_store = ProfileStore()
credential_store = CredentialStore()
runtime_config = RuntimeConfig.from_env()
session_manager = SessionManager(profile_store, credential_store, runtime_config)
logging.getLogger("paramiko").setLevel(logging.WARNING)

_DURATION_RE = re.compile(r"([1-9]\d*)([smh])")
_DURATION_MULTIPLIER = {"s": 1, "m": 60, "h": 3600}
_MAX_COMMAND_SECONDS = 86_400
Port = Annotated[int, Field(ge=1, le=65535)]
Password = Annotated[str, Field(min_length=1, json_schema_extra={"writeOnly": True})]
Duration = Annotated[str, Field(pattern=r"^[1-9]\d*[smh]$")]


def _required(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty.")
    return normalized


def _optional(value: str | None, field: str) -> str | None:
    return _required(value, field) if value is not None else None


def _timeout_seconds(value: str) -> int:
    match = _DURATION_RE.fullmatch(value.strip().lower())
    if match is None:
        raise ValueError("timeout must use a duration such as '30s', '5m', or '2h'.")
    seconds = int(match.group(1)) * _DURATION_MULTIPLIER[match.group(2)]
    if seconds > _MAX_COMMAND_SECONDS:
        raise ValueError("timeout must not exceed 24h.")
    return seconds


@mcp.tool()
async def connect(
    host: str,
    username: str | None = None,
    password: Password | None = None,
    transport: TransportName | None = None,
    port: Port | None = None,
    use_ssl: bool | None = None,
) -> dict[str, Any]:
    """Connect and save credentials. Hosts already in ~/.ssh/config or Windows Credential Manager do not need this."""
    normalized_host = _required(host, "host")
    normalized_username = _optional(username, "username")
    normalized_password = _optional(password, "password")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535.")
    if transport == "ssh" and use_ssl:
        raise ValueError("use_ssl applies only to WinRM.")
    return await _invoke(
        session_manager.connect,
        normalized_host,
        username=normalized_username,
        password=normalized_password,
        transport=transport,
        port=port,
        use_ssl=use_ssl,
    )


@mcp.tool()
async def run(host: str, command: str, timeout: Duration = "5m") -> dict[str, Any]:
    """Run a command on a host. Reuses or restores the connection automatically."""
    return await _invoke(
        session_manager.execute,
        _required(host, "host"),
        _required(command, "command"),
        _timeout_seconds(timeout),
    )


@mcp.tool()
async def write(host: str, remote_path: str, content: str) -> dict[str, Any]:
    """Write UTF-8 text to a remote file, creating parent directories. Overwrites if the file exists. Large or binary files use upload."""
    return await _invoke(
        session_manager.write_file,
        _required(host, "host"),
        _required(remote_path, "remote_path"),
        content,
    )


@mcp.tool()
async def edit(
    host: str,
    remote_path: str,
    old_text: Annotated[str, Field(min_length=1)],
    new_text: str,
    replace_all: bool = False,
) -> dict[str, Any]:
    """Replace exact text in an existing remote UTF-8 file. Requires one match unless replace_all=true. Preserves BOM and unmatched line endings. Empty new_text deletes the match."""
    return await _invoke(
        session_manager.edit_file,
        _required(host, "host"),
        _required(remote_path, "remote_path"),
        old_text,
        new_text,
        replace_all,
    )


@mcp.tool()
async def upload(
    host: str,
    local_path: str,
    remote_path: str,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Upload a file or directory. Creates missing parents. Fails if the target exists unless overwrite=true."""
    local = str(Path(_required(local_path, "local_path")).expanduser())
    return await _invoke(
        session_manager.upload_path,
        _required(host, "host"),
        local,
        _required(remote_path, "remote_path"),
        overwrite=overwrite,
    )


@mcp.tool()
async def download(
    host: str,
    remote_path: str,
    local_path: str,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Download a file or directory. Creates missing parents. Fails if the target exists unless overwrite=true."""
    local = str(Path(_required(local_path, "local_path")).expanduser())
    return await _invoke(
        session_manager.download_path,
        _required(host, "host"),
        _required(remote_path, "remote_path"),
        local,
        overwrite=overwrite,
    )


async def _invoke(callback, host: str, *args, **kwargs):
    cancelled = threading.Event()
    token = operation_cancelled.set(cancelled)
    future = session_manager.submit(callback, host, *args, **kwargs)
    task = asyncio.wrap_future(future)
    operation_cancelled.reset(token)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancelled.set()
        if future.cancel():
            raise
        # MCP cancels an AnyIO scope, so asynchronous cleanup must be shielded.
        with CancelScope(shield=True):
            try:
                await asyncio.to_thread(session_manager.abort, host, cancelled)
            finally:
                await asyncio.gather(task, return_exceptions=True)
        raise


@mcp.tool()
async def status() -> dict[str, Any]:
    """Show connection counts, busy hosts, driver PIDs and lifecycle metrics."""
    return session_manager.status()


def main() -> None:
    try:
        mcp.run(transport="stdio")
    finally:
        session_manager.close_all()


if __name__ == "__main__":
    main()
