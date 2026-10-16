from __future__ import annotations

import posixpath
import stat
from dataclasses import dataclass
from errno import ENOENT
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, TypedDict

import paramiko

from ..models import TransferResult
from .base import BaseTransportSession, TransportAdapter
from .ssh_paths import (
    _is_absolute_remote,
    _relative_remote_local_path,
    _remote_join_sftp,
    _remote_join_shell,
    _remote_leaf,
    _remote_parent,
)

WRITE_TIMEOUT_SECONDS = 300
TRANSFER_TIMEOUT_SECONDS = 3600


@dataclass(slots=True)
class SSHTransportSession(BaseTransportSession):
    client: paramiko.SSHClient
    channel: paramiko.Channel


class RemotePathInfo(TypedDict):
    exists: bool
    full_path: str
    sftp_path: str
    leaf: str
    is_dir: bool
    is_file: bool
    size: int | None


class SSHFileTransport(TransportAdapter):
    def upload_path(
        self,
        session: BaseTransportSession,
        local_path: Path,
        remote_path: str,
        *,
        overwrite: bool,
    ) -> TransferResult:
        if not isinstance(session, SSHTransportSession):
            raise TypeError("SSH adapter received an unexpected session type.")
        source = local_path.expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(f"Local path '{source}' does not exist.")
        with session.client.open_sftp() as sftp:
            self._set_sftp_timeout(sftp, TRANSFER_TIMEOUT_SECONDS)
            if source.is_file():
                return self._upload_file(session, sftp, source, remote_path, overwrite)
            return self._upload_directory(session, sftp, source, remote_path, overwrite)

    def download_path(
        self,
        session: BaseTransportSession,
        remote_path: str,
        local_path: Path,
        *,
        overwrite: bool,
    ) -> TransferResult:
        if not isinstance(session, SSHTransportSession):
            raise TypeError("SSH adapter received an unexpected session type.")
        with session.client.open_sftp() as sftp:
            self._set_sftp_timeout(sftp, TRANSFER_TIMEOUT_SECONDS)
            remote_info = self._get_remote_info(session, sftp, remote_path)
            if not remote_info["exists"]:
                raise FileNotFoundError(f"Remote path '{remote_path}' does not exist.")
            if remote_info["is_file"]:
                return self._download_file(
                    session, sftp, remote_info, local_path, overwrite
                )
            return self._download_directory(
                session, sftp, remote_info, local_path, overwrite
            )

    def write_file(
        self,
        session: BaseTransportSession,
        remote_path: str,
        content: str,
    ) -> dict[str, Any]:
        if not isinstance(session, SSHTransportSession):
            raise TypeError("SSH adapter received an unexpected session type.")
        data = content.encode("utf-8")
        with session.client.open_sftp() as sftp:
            self._set_sftp_timeout(sftp, WRITE_TIMEOUT_SECONDS)
            shell_path = self._resolve_remote_path(session, remote_path)
            sftp_path = self._resolve_remote_sftp_path(session, remote_path)
            try:
                normalized = sftp.normalize(sftp_path)
                attrs = sftp.stat(normalized)
            except OSError as exc:
                if exc.errno != ENOENT:
                    raise
            else:
                if self._is_directory(attrs, normalized):
                    raise IsADirectoryError(
                        f"Remote target '{shell_path}' is a directory."
                    )
                sftp_path = normalized
                if session.state.platform != "windows":
                    shell_path = normalized
            self._prepare_remote_parent(session, sftp, shell_path)
            with sftp.open(sftp_path, "wb") as stream:
                stream.write(data)
            return {"remote_path": shell_path, "bytes": len(data)}

    def edit_file(
        self,
        session: BaseTransportSession,
        remote_path: str,
        old_text: str,
        new_text: str,
        replace_all: bool,
    ) -> dict[str, Any]:
        if not isinstance(session, SSHTransportSession):
            raise TypeError("SSH adapter received an unexpected session type.")
        with session.client.open_sftp() as sftp:
            self._set_sftp_timeout(sftp, WRITE_TIMEOUT_SECONDS)
            info = self._get_remote_info(session, sftp, remote_path)
            if not info["exists"] or not info["is_file"]:
                raise ValueError("remote_path must be an existing regular file.")
            path = str(info["sftp_path"])
            with sftp.open(path, "rb") as stream:
                original = stream.read()
            text = original.decode("utf-8")
            count = text.count(old_text)
            if count == 0 or (count != 1 and not replace_all):
                raise ValueError(
                    f"Expected {'at least one' if replace_all else 'one'} match; found {count}. File unchanged."
                )
            updated = text.replace(old_text, new_text).encode("utf-8")
            with sftp.open(path, "r+b") as stream:
                if stream.read() != original:
                    raise RuntimeError(
                        "Remote file changed during edit. File unchanged."
                    )
                stream.seek(0)
                stream.write(updated)
                stream.truncate(len(updated))
            return {
                "remote_path": str(info["full_path"]),
                "replacements": count,
                "bytes": len(updated),
            }

    def _resolve_remote_path(self, session: SSHTransportSession, path_text: str) -> str:
        if _is_absolute_remote(session.state.platform, path_text):
            return path_text
        base = session.state.cwd or (
            "~" if session.state.platform == "linux" else "C:\\"
        )
        return _remote_join_shell(session.state.platform, base, path_text)

    def _resolve_remote_sftp_path(
        self, session: SSHTransportSession, path_text: str
    ) -> str:
        resolved = self._resolve_remote_path(session, path_text)
        return (
            PureWindowsPath(resolved).as_posix()
            if session.state.platform == "windows"
            else resolved
        )

    def _get_remote_info(
        self,
        session: SSHTransportSession,
        sftp: paramiko.SFTPClient,
        path_text: str,
    ) -> RemotePathInfo:
        shell_path = self._resolve_remote_path(session, path_text)
        sftp_path = self._resolve_remote_sftp_path(session, path_text)
        normalized = sftp.normalize(sftp_path)

        try:
            attrs = sftp.stat(normalized)
            exists = True
            is_dir = self._is_directory(attrs, normalized)
            is_file = not is_dir
            size = attrs.st_size if is_file else None
        except OSError as exc:
            if exc.errno != ENOENT:
                raise
            exists = False
            is_dir = False
            is_file = False
            size = None

        return {
            "exists": exists,
            "full_path": shell_path
            if session.state.platform == "windows"
            else normalized,
            "sftp_path": normalized,
            "leaf": _remote_leaf(session.state.platform, shell_path),
            "is_dir": is_dir,
            "is_file": is_file,
            "size": size,
        }

    def _sftp_mkdirs(
        self, sftp: paramiko.SFTPClient, platform: str, path_text: str
    ) -> None:
        parts = (
            PureWindowsPath(path_text).parts
            if platform == "windows"
            else PurePosixPath(path_text).parts
        )
        current = parts[0] if parts else path_text
        for part in parts[1:]:
            current = _remote_join_sftp(platform, current, part)
            try:
                attrs = sftp.stat(current)
            except OSError as exc:
                if exc.errno != ENOENT:
                    raise
                sftp.mkdir(current)
                continue
            if not self._is_directory(attrs, current):
                raise NotADirectoryError(f"Remote path '{current}' is not a directory.")

    def _prepare_remote_parent(
        self,
        session: SSHTransportSession,
        sftp: paramiko.SFTPClient,
        target_path: str,
    ) -> None:
        parent = _remote_parent(session.state.platform, target_path)
        if not parent:
            return
        self._sftp_mkdirs(
            sftp,
            session.state.platform,
            _remote_join_sftp(session.state.platform, parent),
        )

    def _assert_remote_file_target(
        self,
        session: SSHTransportSession,
        sftp: paramiko.SFTPClient,
        target_path: str,
        overwrite: bool,
    ) -> None:
        info = self._get_remote_info(session, sftp, target_path)
        if info["exists"] and info["is_dir"]:
            raise IsADirectoryError(
                f"Remote target '{info['full_path']}' is a directory."
            )
        if info["exists"] and not overwrite:
            raise FileExistsError(
                f"Remote file '{info['full_path']}' already exists. Use overwrite=true to replace it."
            )

    def _assert_local_file_target(self, target: Path, overwrite: bool) -> None:
        if target.exists() and target.is_dir():
            raise IsADirectoryError(f"Local target '{target}' is a directory.")
        if target.exists() and not overwrite:
            raise FileExistsError(
                f"Local file '{target}' already exists. Use overwrite=true to replace it."
            )

    def _upload_file(
        self,
        session: SSHTransportSession,
        sftp: paramiko.SFTPClient,
        source: Path,
        remote_path: str,
        overwrite: bool,
    ) -> TransferResult:
        remote_info = self._get_remote_info(session, sftp, remote_path)
        if remote_info["exists"] and remote_info["is_dir"]:
            remote_target_shell = _remote_join_shell(
                session.state.platform, str(remote_info["full_path"]), source.name
            )
        else:
            remote_target_shell = self._resolve_remote_path(session, remote_path)
        remote_target_sftp = (
            PureWindowsPath(remote_target_shell).as_posix()
            if session.state.platform == "windows"
            else remote_target_shell
        )

        self._prepare_remote_parent(session, sftp, remote_target_shell)
        self._assert_remote_file_target(session, sftp, remote_target_shell, overwrite)
        sftp.put(str(source), remote_target_sftp)
        return TransferResult(
            target_path=remote_target_shell,
            transferred_files=1,
            total_bytes=source.stat().st_size,
        )

    def _upload_directory(
        self,
        session: SSHTransportSession,
        sftp: paramiko.SFTPClient,
        source: Path,
        remote_path: str,
        overwrite: bool,
    ) -> TransferResult:
        remote_info = self._get_remote_info(session, sftp, remote_path)
        if remote_info["exists"]:
            if remote_info["is_file"]:
                raise NotADirectoryError(
                    f"Remote path '{remote_info['full_path']}' is a file."
                )
            remote_root_shell = _remote_join_shell(
                session.state.platform, str(remote_info["full_path"]), source.name
            )
        else:
            remote_root_shell = self._resolve_remote_path(session, remote_path)

        root_info = self._get_remote_info(session, sftp, remote_root_shell)
        if root_info["exists"] and root_info["is_file"]:
            raise NotADirectoryError(
                f"Remote path '{root_info['full_path']}' is a file."
            )
        if root_info["exists"] and not overwrite:
            raise FileExistsError(
                f"Remote directory '{root_info['full_path']}' already exists. Use overwrite=true to merge into it."
            )

        self._prepare_remote_parent(session, sftp, remote_root_shell)
        self._sftp_mkdirs(
            sftp,
            session.state.platform,
            _remote_join_sftp(session.state.platform, remote_root_shell),
        )

        files = total_bytes = 0
        for file_path in source.rglob("*"):
            if file_path.is_dir():
                self._sftp_mkdirs(
                    sftp,
                    session.state.platform,
                    _remote_join_sftp(
                        session.state.platform,
                        remote_root_shell,
                        *file_path.relative_to(source).parts,
                    ),
                )
                continue
            if not file_path.is_file():
                continue
            relative_path = file_path.relative_to(source)
            remote_target_shell = _remote_join_shell(
                session.state.platform, remote_root_shell, *relative_path.parts
            )
            remote_target_sftp = _remote_join_sftp(
                session.state.platform, remote_root_shell, *relative_path.parts
            )
            self._assert_remote_file_target(
                session, sftp, remote_target_shell, overwrite
            )
            self._prepare_remote_parent(session, sftp, remote_target_shell)
            sftp.put(str(file_path), remote_target_sftp)
            total_bytes += file_path.stat().st_size
            files += 1

        return TransferResult(
            target_path=remote_root_shell,
            transferred_files=files,
            total_bytes=total_bytes,
        )

    def _download_file(
        self,
        session: SSHTransportSession,
        sftp: paramiko.SFTPClient,
        remote_info: RemotePathInfo,
        local_path: Path,
        overwrite: bool,
    ) -> TransferResult:
        local_target = local_path.expanduser().resolve()
        if local_target.exists() and local_target.is_dir():
            local_target = local_target / str(remote_info["leaf"])
        local_target.parent.mkdir(parents=True, exist_ok=True)
        self._assert_local_file_target(local_target, overwrite)
        sftp.get(str(remote_info["sftp_path"]), str(local_target))
        return TransferResult(
            target_path=str(local_target),
            transferred_files=1,
            total_bytes=remote_info["size"] or local_target.stat().st_size,
        )

    def _download_directory(
        self,
        session: SSHTransportSession,
        sftp: paramiko.SFTPClient,
        remote_info: RemotePathInfo,
        local_path: Path,
        overwrite: bool,
    ) -> TransferResult:
        local_root = local_path.expanduser().resolve()
        if local_root.exists():
            if not local_root.is_dir():
                raise NotADirectoryError(f"Local path '{local_root}' is a file.")
            local_root = local_root / str(remote_info["leaf"])

        if local_root.exists() and not overwrite:
            raise FileExistsError(
                f"Local directory '{local_root}' already exists. Use overwrite=true to merge into it."
            )

        local_root.parent.mkdir(parents=True, exist_ok=True)
        local_root.mkdir(parents=True, exist_ok=True)

        files: list[tuple[str, str]] = []
        total_bytes = 0

        def walk(remote_dir_sftp: str, relative_dir: str = "") -> None:
            nonlocal total_bytes
            for entry in sftp.listdir_attr(remote_dir_sftp):
                child_remote = posixpath.join(remote_dir_sftp, entry.filename)
                child_relative = (
                    posixpath.join(relative_dir, entry.filename)
                    if relative_dir
                    else entry.filename
                )
                if self._is_directory(entry, child_remote):
                    (
                        local_root
                        / _relative_remote_local_path(
                            session.state.platform, child_relative
                        )
                    ).mkdir(
                        parents=True,
                        exist_ok=True,
                    )
                    walk(child_remote, child_relative)
                else:
                    files.append((child_remote, child_relative))

        walk(str(remote_info["sftp_path"]))
        for remote_file, relative_path in files:
            local_target = local_root / _relative_remote_local_path(
                session.state.platform, relative_path
            )
            self._assert_local_file_target(local_target, overwrite)
            local_target.parent.mkdir(parents=True, exist_ok=True)
            sftp.get(remote_file, str(local_target))
            total_bytes += local_target.stat().st_size

        return TransferResult(
            target_path=str(local_root),
            transferred_files=len(files),
            total_bytes=total_bytes,
        )

    @staticmethod
    def _set_sftp_timeout(sftp: paramiko.SFTPClient, timeout_seconds: float) -> None:
        channel = sftp.get_channel()
        if channel is None:
            raise ConnectionError("SSH SFTP channel was not created.")
        channel.settimeout(timeout_seconds)

    @staticmethod
    def _is_directory(attrs: paramiko.SFTPAttributes, path: str) -> bool:
        if attrs.st_mode is None:
            raise RuntimeError(f"SSH server did not return a file type for '{path}'.")
        return stat.S_ISDIR(attrs.st_mode)
