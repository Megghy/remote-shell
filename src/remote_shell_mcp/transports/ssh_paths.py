from __future__ import annotations

import re
from pathlib import Path, PurePosixPath, PureWindowsPath

WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]|^\\\\")


def _is_absolute_remote(platform: str, path_text: str) -> bool:
    return (
        bool(WINDOWS_ABS_RE.match(path_text))
        if platform == "windows"
        else path_text.startswith("/")
    )


def _remote_join_shell(platform: str, base: str, *parts: str) -> str:
    if platform == "windows":
        path = PureWindowsPath(base)
        for part in parts:
            if part:
                path /= PureWindowsPath(part)
        return str(path)
    path = PurePosixPath(base)
    for part in parts:
        if part:
            path /= PurePosixPath(part)
    return str(path)


def _remote_join_sftp(platform: str, base: str, *parts: str) -> str:
    shell_path = _remote_join_shell(platform, base, *parts)
    return (
        PureWindowsPath(shell_path).as_posix() if platform == "windows" else shell_path
    )


def _remote_parent(platform: str, path_text: str) -> str | None:
    parent = (
        str(PureWindowsPath(path_text).parent)
        if platform == "windows"
        else str(PurePosixPath(path_text).parent)
    )
    return None if parent == "." else parent


def _remote_leaf(platform: str, path_text: str) -> str:
    return (
        PureWindowsPath(path_text).name
        if platform == "windows"
        else PurePosixPath(path_text).name
    )


def _relative_remote_local_path(platform: str, relative_path: str) -> Path:
    if not relative_path or relative_path == ".":
        return Path()
    parts = (
        PureWindowsPath(relative_path).parts
        if platform == "windows"
        else PurePosixPath(relative_path).parts
    )
    return Path(*parts)
