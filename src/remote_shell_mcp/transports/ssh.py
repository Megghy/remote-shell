from __future__ import annotations

import time
from time import perf_counter

import paramiko

from ..models import (
    CredentialSecret,
    ExecResult,
    RemoteProfile,
    SessionState,
)
from ..profile_store import DATA_DIR
from ..shells import build_envelope, default_shell_for_platform, resolve_shell
from ..ssh_config import lookup_ssh_config
from .base import BaseTransportSession
from .output import BoundedOutput, MarkedOutput
from .ssh_files import SSHFileTransport, SSHTransportSession

KNOWN_HOSTS_PATH = DATA_DIR / "known_hosts"
PROBE_TIMEOUT_SECONDS = 5


class SSHTransportAdapter(SSHFileTransport):
    def open_session(
        self, profile: RemoteProfile, secret: CredentialSecret
    ) -> BaseTransportSession:
        client = paramiko.SSHClient()
        try:
            KNOWN_HOSTS_PATH.parent.mkdir(parents=True, exist_ok=True)
            KNOWN_HOSTS_PATH.touch(exist_ok=True)
            client.load_system_host_keys()
            client.load_host_keys(str(KNOWN_HOSTS_PATH))
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            ssh = lookup_ssh_config(profile.target) or lookup_ssh_config(profile.host)
            identities_only = bool(ssh and ssh.identities_only and ssh.identity_files)
            connect_kwargs = {
                "hostname": profile.host,
                "port": profile.port,
                "username": profile.username,
                "timeout": profile.connection_timeout,
                "banner_timeout": profile.connection_timeout,
                "auth_timeout": profile.connection_timeout,
                "look_for_keys": not identities_only,
                "allow_agent": True,
            }
            if ssh and ssh.identity_files:
                connect_kwargs["key_filename"] = list(ssh.identity_files)
            if secret.password:
                connect_kwargs["password"] = secret.password
            client.connect(**connect_kwargs)
            transport = client.get_transport()
            if transport is None:
                raise ConnectionError("SSH transport was not created.")
            transport.set_keepalive(30)

            platform = self._detect_platform(client, profile)
            shell_spec = resolve_shell(default_shell_for_platform(platform), platform)
            channel = transport.open_session(timeout=profile.connection_timeout)
            channel.exec_command(shell_spec.launch_command)
            channel.settimeout(0.0)
            session = SSHTransportSession(
                profile=profile,
                state=SessionState(
                    transport="ssh",
                    host=profile.host,
                    shell_type=shell_spec.shell_type,
                    platform=shell_spec.platform,
                ),
                client=client,
                channel=channel,
            )
            self._drain_startup(session)
            if shell_spec.init_command:
                self._execute_internal(
                    session, shell_spec.init_command, profile.connection_timeout
                )
            session.state.computer_name = (
                self._execute_internal(
                    session, shell_spec.hostname_command, profile.connection_timeout
                ).stdout.strip()
                or profile.host
            )
            session.state.remote_identity = (
                self._execute_internal(
                    session, shell_spec.identity_command, profile.connection_timeout
                ).stdout.strip()
                or profile.username
            )
            session.state.cwd = self._execute_internal(
                session,
                shell_spec.cwd_command,
                profile.connection_timeout,
            ).cwd
            return session
        except Exception:
            client.close()
            raise

    def is_open(self, session: BaseTransportSession) -> bool:
        if not isinstance(session, SSHTransportSession):
            raise TypeError("SSH adapter received an unexpected session type.")
        return self._session_open(session)

    def is_alive(self, session: BaseTransportSession) -> bool:
        if not isinstance(session, SSHTransportSession) or not self._session_open(
            session
        ):
            return False
        transport = session.client.get_transport()
        if transport is None:
            return False
        try:
            probe = transport.open_session(timeout=PROBE_TIMEOUT_SECONDS)
        except (OSError, EOFError, paramiko.SSHException):
            return False
        try:
            probe.close()
        except (OSError, EOFError, paramiko.SSHException):
            return False
        return self._session_open(session)

    @staticmethod
    def _session_open(session: SSHTransportSession) -> bool:
        transport = session.client.get_transport()
        return bool(
            transport
            and transport.is_active()
            and transport.is_authenticated()
            and not session.channel.closed
            and not session.channel.exit_status_ready()
        )

    def execute(
        self, session: BaseTransportSession, command: str, timeout_seconds: float
    ) -> ExecResult:
        if not isinstance(session, SSHTransportSession):
            raise TypeError("SSH adapter received an unexpected session type.")
        result = self._execute_internal(session, command, timeout_seconds)
        session.state.cwd = result.cwd or session.state.cwd
        return result

    def close(self, session: BaseTransportSession) -> None:
        if not isinstance(session, SSHTransportSession):
            return
        try:
            session.channel.close()
        finally:
            session.client.close()

    def _detect_platform(
        self, client: paramiko.SSHClient, profile: RemoteProfile
    ) -> str:
        if self._exec_once(client, "uname -s")[0] == 0:
            return "linux"
        if self._exec_once(client, "cmd /c ver")[0] == 0:
            return "windows"
        if (
            self._exec_once(client, 'powershell -NoLogo -NoProfile -Command "$env:OS"')[
                0
            ]
            == 0
        ):
            return "windows"
        raise RuntimeError(f"Failed to detect the platform on '{profile.host}'.")

    def _exec_once(
        self,
        client: paramiko.SSHClient,
        command: str,
        timeout_seconds: float = 10,
    ) -> tuple[int, str, str]:
        transport = client.get_transport()
        if transport is None:
            raise ConnectionError("SSH transport was not created.")
        channel = transport.open_session(timeout=timeout_seconds)
        try:
            channel.exec_command(command)
            channel.shutdown_write()
            deadline = time.monotonic() + timeout_seconds
            stdout = BoundedOutput()
            stderr = BoundedOutput()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"SSH command timed out after {timeout_seconds:g} seconds."
                    )
                if channel.recv_ready():
                    stdout.append(channel.recv(4096))
                    continue
                if channel.recv_stderr_ready():
                    stderr.append(channel.recv_stderr(4096))
                    continue
                if channel.exit_status_ready():
                    while channel.recv_ready():
                        stdout.append(channel.recv(4096))
                    while channel.recv_stderr_ready():
                        stderr.append(channel.recv_stderr(4096))
                    break
                time.sleep(min(0.02, remaining))
            return (
                channel.recv_exit_status(),
                stdout.text(),
                stderr.text(),
            )
        finally:
            channel.close()

    def _drain_startup(self, session: SSHTransportSession) -> None:
        end_at = time.monotonic() + 0.3
        while time.monotonic() < end_at:
            drained = False
            if session.channel.recv_ready():
                session.channel.recv(4096)
                drained = True
            if session.channel.recv_stderr_ready():
                session.channel.recv_stderr(4096)
                drained = True
            if not drained:
                time.sleep(0.02)

    def _execute_internal(
        self,
        session: SSHTransportSession,
        command: str,
        timeout_seconds: float,
    ) -> ExecResult:
        if not self._session_open(session):
            raise ConnectionError("SSH connection closed before command dispatch.")
        envelope = build_envelope(session.state.shell_type, command)
        started = perf_counter()
        try:
            session.channel.settimeout(session.profile.connection_timeout)
            session.channel.sendall(envelope.payload.encode("utf-8"))
        finally:
            session.channel.settimeout(0.0)
        stdout = MarkedOutput(
            envelope.stdout_marker_prefix.encode("ascii"), status=True
        )
        stderr = MarkedOutput(envelope.stderr_marker.encode("ascii"))
        deadline = time.monotonic() + timeout_seconds
        while not (stdout.done and stderr.done):
            if time.monotonic() >= deadline:
                session.channel.close()
                raise TimeoutError(
                    f"SSH command timed out after {timeout_seconds:g} seconds."
                )
            had_data = False
            if session.channel.recv_ready():
                stdout.feed(session.channel.recv(4096))
                had_data = True
            if session.channel.recv_stderr_ready():
                stderr.feed(session.channel.recv_stderr(4096))
                had_data = True
            if stdout.done and stderr.done:
                break
            if not self._session_open(session):
                raise ConnectionError(
                    "SSH connection closed before command completion."
                )
            if not had_data:
                time.sleep(0.02)
        return ExecResult(
            duration_ms=int((perf_counter() - started) * 1000),
            exit_code=stdout.code if stdout.code is not None else 1,
            stdout=stdout.output.text(),
            stderr=stderr.output.text(),
            cwd=stdout.cwd or session.state.cwd,
        )
