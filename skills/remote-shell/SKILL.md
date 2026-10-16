---
name: remote-shell
description: Use when you need to run commands or transfer files on a Windows or Linux host over WinRM or SSH with saved credentials and automatic connection reuse.
---

# Remote Shell

## Workflow

1. Call `run(host, command)` directly when the host is already in `~/.ssh/config` or Windows Credential Manager.
2. Call `connect(host, username, password)` only for a host that is in neither store. Add `transport="ssh"` for SSH; WinRM is the default.
3. Set `timeout` only when needed (`30s`, `10m`, `2h`).
4. Use `write(host, remote_path, content)` to create or overwrite a remote UTF-8 file. Use `edit(host, remote_path, old_text, new_text)` for an exact in-place replacement (one match unless `replace_all=true`). Large or binary files use `upload`.
5. Use `upload` / `download` for local↔remote files and trees. Existing targets fail unless `overwrite=true`.

Do not manage session IDs. Connections are reused and restored automatically. If a connection drops after dispatch or times out, the plugin restores it for later calls but does not repeat the operation.

WinRM defaults to Negotiate on port 5985 and the `PowerShell.7` endpoint. Windows hosts need PowerShell 7 with remoting enabled (`Enable-PSRemoting` from an elevated pwsh). Native command I/O is UTF-8.

SSH reads `~/.ssh/config` (User, Port, IdentityFile, IdentitiesOnly), then ssh-agent and default keys, then a password from Windows Credential Manager or the plugin keyring. Changed host keys are rejected.

## Runtime limits

`status()` reports active and reserved connections, busy hosts, driver PIDs, connection age, and lifecycle counters. Connections expire after 5 minutes without use. An idle LRU connection is closed when the local pool is full; busy connections are never evicted.

Configure the MCP process with these environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `REMOTE_SHELL_MAX_CONNECTIONS` | `8` | Active/opening connections per MCP process |
| `REMOTE_SHELL_MAX_ENDPOINT_CONNECTIONS` | `8` | Connections per endpoint and user across processes on this local machine |
| `REMOTE_SHELL_CONNECTION_WAIT_SECONDS` | `15` | Maximum wait for a connection slot |
| `REMOTE_SHELL_IDLE_TIMEOUT_SECONDS` | `300` | Local idle eviction timeout |
| `REMOTE_SHELL_HEALTHCHECK_INTERVAL_SECONDS` | `30` | Minimum interval between health probes |
| `REMOTE_SHELL_WINRM_IDLE_TIMEOUT_SECONDS` | `180` | Remote PSSession idle timeout; minimum 60 seconds |
| `REMOTE_SHELL_DRIVER_CLOSE_TIMEOUT_SECONDS` | `20` | Grace period for cancellation and remote session removal before killing a driver |
| `REMOTE_SHELL_MAX_CONTENT_CHARS` | `2000000` | Combined text input limit per command/write/edit |

Use identical endpoint limits for all local MCP instances. Different client machines still share the server's WinRM quota, so configure `PowerShell.7/Quotas/MaxShellsPerUser` for the total workload. Increasing this quota does not reclaim old sessions. Abrupt driver termination or network loss can leave a remote session until WinRM's idle timeout; graceful cancellation and idle eviction remove the session immediately when the server is reachable.

Output is bounded while it is consumed, then shortened for display. Truncated output includes an omission notice. A cancelled or timed-out command may already have produced side effects; it is never replayed. The next call establishes a fresh connection.

## Verification

Run the regression suite from the project root:

```powershell
$env:PYTHONPATH = 'src'
uv run --no-sync python -m unittest discover -s tests -v
```

For a saved Windows host, run the real WinRM checks:

```powershell
$env:PYTHONPATH = 'src'
uv run --no-sync python scripts/verify_winrm_encoding.py 10.10.0.2
uv run --no-sync python scripts/verify_lifecycle.py 10.10.0.2
```

The lifecycle check creates and removes a unique temporary remote directory. It validates variable persistence, UTF-8 write/edit, binary transfer, repeated command timeouts and cancellation, including removal of the corresponding remote Shell IDs.
