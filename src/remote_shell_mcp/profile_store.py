from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

from filelock import FileLock

from .models import RemoteProfile, TransportName
from .serialization import unix_timestamp

DATA_DIR = Path.home() / ".codex" / "remote-shell"
PROFILES_PATH = DATA_DIR / "profiles.json"
VALID_TRANSPORT = {"winrm", "ssh"}


def _load_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 2, "profiles": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("profiles"), dict):
        raise TypeError(f"{path} must contain a profiles object.")
    if payload.get("version") != 2:
        raise ValueError(f"{path} uses an unsupported profile format.")
    return payload


def _parse_profile(target: str, raw: dict[str, Any]) -> RemoteProfile:
    raw_transport = raw["transport"]
    if raw_transport not in VALID_TRANSPORT:
        raise ValueError(f"Unknown transport '{raw_transport}' for target '{target}'.")
    transport = cast(TransportName, raw_transport)
    host = raw["host"]
    username = raw["username"]
    port = raw["port"]
    use_ssl = raw["use_ssl"]
    connection_timeout = raw["connection_timeout"]
    if (
        not isinstance(host, str)
        or not host
        or not isinstance(username, str)
        or not username
    ):
        raise ValueError(f"Target '{target}' has an invalid host or username.")
    if isinstance(port, bool) or not isinstance(port, int):
        raise TypeError(f"Target '{target}' has an invalid port type.")
    if not 1 <= port <= 65535:
        raise ValueError(f"Target '{target}' has an invalid port.")
    if not isinstance(use_ssl, bool):
        raise TypeError(f"Target '{target}' has an invalid use_ssl type.")
    if isinstance(connection_timeout, bool) or not isinstance(connection_timeout, int):
        raise TypeError(f"Target '{target}' has an invalid connection timeout type.")
    if connection_timeout < 1:
        raise ValueError(f"Target '{target}' has an invalid connection timeout.")
    return RemoteProfile(
        target=target,
        host=host,
        username=username,
        transport=transport,
        port=port,
        use_ssl=use_ssl,
        connection_timeout=connection_timeout,
        created_at=int(raw["created_at"]),
        updated_at=int(raw["updated_at"]),
    )


class ProfileStore:
    def __init__(self, path: Path = PROFILES_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = FileLock(str(path) + ".lock")

    def get(self, target: str) -> RemoteProfile:
        with self._lock:
            profiles = _load_payload(self.path)["profiles"]
            try:
                raw = profiles[target]
            except KeyError as exc:
                raise KeyError(
                    f"Target '{target}' has not been connected before."
                ) from exc
            return _parse_profile(target, raw)

    def save(self, profile: RemoteProfile) -> None:
        with self._lock:
            payload = _load_payload(self.path)
            now = unix_timestamp()
            existing = payload["profiles"].get(profile.target, {})
            profile.created_at = int(
                existing.get("created_at", profile.created_at or now)
            )
            profile.updated_at = now
            data = asdict(profile)
            data.pop("target")
            payload["version"] = 2
            payload["profiles"][profile.target] = data
            self._write(payload)

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        temp_path.replace(self.path)
