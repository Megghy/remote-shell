from __future__ import annotations

import uuid
from dataclasses import dataclass

STDOUT_PREFIX = "__CODEX_STATUS__"
STDERR_PREFIX = "__CODEX_STDERR__"


@dataclass(slots=True)
class CommandEnvelope:
    marker: str
    payload: str
    stdout_marker_prefix: str
    stderr_marker: str


@dataclass(slots=True)
class ShellSpec:
    shell_type: str
    platform: str
    launch_command: str
    init_command: str | None = None
    hostname_command: str = "hostname"
    identity_command: str = ""
    cwd_command: str = ""


def infer_platform_from_shell(shell_type: str) -> str:
    if shell_type in {"bash", "sh", "zsh"}:
        return "linux"
    return "windows"


def default_shell_for_platform(platform: str) -> str:
    return "bash" if platform == "linux" else "powershell"


def resolve_shell(shell_type: str, platform_hint: str) -> ShellSpec:
    platform = (
        platform_hint
        if platform_hint != "auto"
        else infer_platform_from_shell(shell_type)
    )
    resolved_shell = (
        shell_type if shell_type != "auto" else default_shell_for_platform(platform)
    )

    if resolved_shell == "bash":
        return ShellSpec(
            shell_type="bash",
            platform="linux",
            launch_command="bash --noprofile --norc",
            init_command="export LANG=C.UTF-8 >/dev/null 2>&1 || true",
            hostname_command="hostname",
            identity_command="whoami",
            cwd_command="pwd",
        )
    if resolved_shell == "sh":
        return ShellSpec(
            shell_type="sh",
            platform="linux",
            launch_command="sh",
            hostname_command="hostname",
            identity_command="whoami",
            cwd_command="pwd",
        )
    if resolved_shell == "zsh":
        return ShellSpec(
            shell_type="zsh",
            platform="linux",
            launch_command="zsh -f",
            hostname_command="hostname",
            identity_command="whoami",
            cwd_command="pwd",
        )
    if resolved_shell == "cmd":
        return ShellSpec(
            shell_type="cmd",
            platform="windows",
            launch_command="cmd.exe /Q /D /K chcp 65001>nul",
            hostname_command="hostname",
            identity_command="whoami",
            cwd_command="cd",
        )
    if resolved_shell == "pwsh":
        return ShellSpec(
            shell_type="pwsh",
            platform="windows" if platform == "windows" else "linux",
            launch_command="pwsh -NoLogo -NoProfile -Command -",
            init_command="[Console]::InputEncoding=[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)",
            hostname_command="hostname",
            identity_command="whoami",
            cwd_command="(Get-Location).Path",
        )
    return ShellSpec(
        shell_type="powershell",
        platform="windows",
        launch_command="powershell -NoLogo -NoProfile -Command -",
        init_command="[Console]::InputEncoding=[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)",
        hostname_command="hostname",
        identity_command="whoami",
        cwd_command="(Get-Location).Path",
    )


def build_envelope(shell_type: str, command: str) -> CommandEnvelope:
    marker = uuid.uuid4().hex
    stdout_marker_prefix = f"{STDOUT_PREFIX}{marker}__"
    stderr_marker = f"{STDERR_PREFIX}{marker}"

    if shell_type in {"bash", "sh", "zsh"}:
        payload = (
            f"{command}\n"
            f"__codex_rc=$?\n"
            f'printf "{stdout_marker_prefix}%s__%s\\n" "$__codex_rc" "$PWD"\n'
            f'printf "{stderr_marker}\\n" >&2\n'
        )
    elif shell_type in {"powershell", "pwsh"}:
        payload = (
            "$global:LASTEXITCODE = 0\n"
            "$__codex_success = $true\n"
            "try {\n"
            f"{command}\n"
            "} catch {\n"
            "  $__codex_success = $false\n"
            "  Write-Error $_\n"
            "}\n"
            "$__codex_exit = if (-not $__codex_success -or -not $?) { "
            "if ($LASTEXITCODE -ne 0) { [int]$LASTEXITCODE } else { 1 } "
            "} else { [int]$LASTEXITCODE }\n"
            "$__codex_cwd = (Get-Location).Path\n"
            f'Write-Output "{stdout_marker_prefix}$($__codex_exit)__$($__codex_cwd)"\n'
            f'Write-Error "{stderr_marker}"\n'
        )
    else:
        payload = (
            f"{command}\r\n"
            f"echo {stdout_marker_prefix}%errorlevel%__%cd%\r\n"
            f"echo {stderr_marker} 1>&2\r\n"
        )

    return CommandEnvelope(
        marker=marker,
        payload=payload,
        stdout_marker_prefix=stdout_marker_prefix,
        stderr_marker=stderr_marker,
    )
