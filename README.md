# Remote Shell MCP

提供跨平台远程命令执行与文件传输的 MCP 服务端，支持 WinRM（PowerShell 7）与 SSH，具备连接复用、并发控制、自动空闲回收及优雅超时取消机制。

## 主要特性

- **凭据自动解析**：优先读取本地 Windows 凭据管理器（WinRM）与 `~/.ssh/config` / SSH Agent，无需在每次调用时明文提供密码。
- **连接复用与状态保持**：同主机命令自动复用连接，保持工作目录与临时会话变量；同一主机的调用自动串行化。
- **并发与生命周期管理**：
  - 限制单实例全局连接数与跨进程单目标并发配额。
  - LRU 空闲连接自动淘汰（默认 5 分钟），由后台定时器主动回收。
  - 请求取消与超时秒级回收远端 Session，防止服务端 Shell 配额耗尽。
- **流式内存限制**：命令输出与文件传输在流式接收阶段即限制内存缓冲区（最大 256 KiB 首尾保留），防止大输出导致内存膨胀。

## MCP 工具列表

| 工具 | 说明 | 关键参数 |
| --- | --- | --- |
| `run` | 在远程主机上执行命令并返回输出 | `host`, `command`, `timeout`（支持 `30s`, `10m`, `2h`） |
| `write` | 写入或覆盖远程 UTF-8 文本文件（自动创建父目录） | `host`, `remote_path`, `content` |
| `edit` | 远程文件原位文本替换 | `host`, `remote_path`, `old_text`, `new_text`, `replace_all` |
| `upload` | 本地文件或目录上传至远程 | `host`, `local_path`, `remote_path`, `overwrite` |
| `download` | 远程文件或目录下载至本地 | `host`, `remote_path`, `local_path`, `overwrite` |
| `connect` | 手动注册新主机连接凭据 | `host`, `username`, `password`, `transport` (`winrm`/`ssh`), `port`, `use_ssl` |
| `status` | 查询连接池实时状态与生命周期指标 | 无 |

## 运行环境与前置准备

- **运行时**：Python >= 3.11，推荐使用 `uv` 管理依赖。
- **Windows 目标机（WinRM）**：
  - 需要安装并启用 PowerShell 7 remoting（以管理员身份在 pwsh 执行 `Enable-PSRemoting`）。
  - 默认使用 Negotiate 认证与 5985 端口，连接 `PowerShell.7` 终结点。
  - 若为高并发或长期运行场景，建议调高插件层并发配额并缩短空闲超时：
    ```powershell
    Set-Item WSMan:\localhost\Plugin\PowerShell.7\Quotas\MaxShellsPerUser -Value 50
    Set-Item WSMan:\localhost\Plugin\PowerShell.7\Quotas\IdleTimeoutms -Value 180000
    Restart-Service winrm
    ```
- **Linux 目标机（SSH）**：
  - 依赖标准 OpenSSH 服务与 SFTP 子系统。

## 环境变量配置

可按需在 MCP 运行环境中配置以下变量：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `REMOTE_SHELL_MAX_CONNECTIONS` | `8` | 单个 MCP 进程内最大活动/正在建立的连接数 |
| `REMOTE_SHELL_MAX_ENDPOINT_CONNECTIONS` | `8` | 本机所有 MCP 实例对同一远端终结点的并发上限 |
| `REMOTE_SHELL_IDLE_TIMEOUT_SECONDS` | `300` | 连接空闲超时时间（秒），超时后由后台定时器主动释放 |
| `REMOTE_SHELL_WINRM_IDLE_TIMEOUT_SECONDS` | `180` | WinRM 远程会话断开后的服务端保留时长（秒，最低 60） |
| `REMOTE_SHELL_DRIVER_CLOSE_TIMEOUT_SECONDS` | `20` | 命令超时或取消时，等待驱动与远端 Session 优雅关闭的宽限时间（秒） |
| `REMOTE_SHELL_CONNECTION_WAIT_SECONDS` | `15` | 等待可用连接槽位的最大等待时间（秒） |
| `REMOTE_SHELL_MAX_CONTENT_CHARS` | `2000000` | 单次命令、写入或编辑请求的最大输入字符数 |

## 测试与验证

项目提供单元测试与针对真实主机的回归验证脚本：

```powershell
$env:PYTHONPATH = 'src'

# 1. 运行全量单元测试与本地生命周期测试
uv run --no-sync python -m unittest discover -s tests -v

# 2. 真实 WinRM 编码与长输出截断回归（需已配置目标机凭据）
uv run --no-sync python scripts/verify_winrm_encoding.py <host>

# 3. 真实 WinRM 完整生命周期回归（文件传输、连续超时与取消清理）
uv run --no-sync python scripts/verify_lifecycle.py <host>
```
