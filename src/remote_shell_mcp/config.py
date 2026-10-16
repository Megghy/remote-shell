from __future__ import annotations

import math
import os
from dataclasses import dataclass, fields


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    max_connections: int = 8
    max_endpoint_connections: int = 8
    connection_wait_seconds: float = 15.0
    idle_timeout_seconds: float = 300.0
    healthcheck_interval_seconds: float = 30.0
    winrm_idle_timeout_seconds: int = 180
    driver_close_timeout_seconds: float = 20.0
    max_content_chars: int = 2_000_000

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{item.name} must be finite and positive.")
        if self.winrm_idle_timeout_seconds < 60:
            raise ValueError("winrm_idle_timeout_seconds must be at least 60.")

    @classmethod
    def from_env(cls) -> RuntimeConfig:
        defaults = cls()
        values = {}
        for item in fields(defaults):
            default = getattr(defaults, item.name)
            raw = os.getenv(f"REMOTE_SHELL_{item.name.upper()}")
            values[item.name] = type(default)(raw) if raw is not None else default
        return cls(**values)
