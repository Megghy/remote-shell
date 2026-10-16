from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import paramiko

SSH_CONFIG_PATH = Path.home() / ".ssh" / "config"


@dataclass(frozen=True, slots=True)
class SshHostConfig:
    hostname: str
    user: str | None = None
    port: int | None = None
    identity_files: tuple[str, ...] = ()
    identities_only: bool = False


def lookup_ssh_config(host: str) -> SshHostConfig | None:
    if not SSH_CONFIG_PATH.is_file():
        return None
    parsed = paramiko.SSHConfig()
    with SSH_CONFIG_PATH.open(encoding="utf-8") as handle:
        parsed.parse(handle)
    info = parsed.lookup(host)
    user = info.get("user")
    port_text = info.get("port")
    identity_files = tuple(info.get("identityfile") or ())
    if user is None and port_text is None and not identity_files:
        return None
    return SshHostConfig(
        hostname=info.get("hostname") or host,
        user=user,
        port=int(port_text) if port_text is not None else None,
        identity_files=identity_files,
        identities_only=str(info.get("identitiesonly", "no")).lower() == "yes",
    )
