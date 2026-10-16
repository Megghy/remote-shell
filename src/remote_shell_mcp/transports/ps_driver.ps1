param(
    [Parameter(Mandatory)][string]$RHost,
    [Parameter(Mandatory)][string]$User,
    [string]$Auth = "Negotiate",
    [int]$Port = 5985,
    [int]$OpenTimeoutMs = 30000,
    [int]$IdleTimeoutMs = 900000,
    [int]$CancelTimeoutMs = 5000,
    [switch]$UseSsl
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $OutputEncoding
[Console]::OutputEncoding = $OutputEncoding
. "$PSScriptRoot/ps_actions.ps1"
$Session = $null
$Worker = $null

try {
    $Init = [Console]::In.ReadLine() | ConvertFrom-Json
    if ([string]::IsNullOrEmpty($Init.password)) { throw "Missing WinRM password." }
    $SecurePassword = ConvertTo-SecureString $Init.password -AsPlainText -Force
    $Credential = [pscredential]::new($User, $SecurePassword)
    $Init = $null
    $SessionParams = @{
        ComputerName = $RHost
        Credential = $Credential
        Authentication = $Auth
        ConfigurationName = "PowerShell.7"
        Port = $Port
        SessionOption = (New-PSSessionOption -NoMachineProfile -OpenTimeout $OpenTimeoutMs -IdleTimeout $IdleTimeoutMs -CancelTimeout $CancelTimeoutMs)
    }
    if ($UseSsl) { $SessionParams.UseSSL = $true }
    $Session = New-PSSession @SessionParams
    $Info = Invoke-Command -Session $Session -ScriptBlock {
        $global:OutputEncoding = [System.Text.UTF8Encoding]::new($false)
        [Console]::InputEncoding = $global:OutputEncoding
        [Console]::OutputEncoding = $global:OutputEncoding
        @{ computer_name = $env:COMPUTERNAME; remote_identity = (whoami); cwd = (Get-Location).Path }
    }
    Emit @{
        type = "ready"; session_id = $Session.InstanceId.ToString()
        computer_name = $Info.computer_name; remote_identity = $Info.remote_identity; cwd = $Info.cwd
    }

    $Worker = [powershell]::Create()
    $NextLine = [Console]::In.ReadLineAsync()
    while ($null -ne ($Line = $NextLine.GetAwaiter().GetResult())) {
        $Request = $Line | ConvertFrom-Json
        if ($Request.action -eq "close") { break }
        $null = $Worker.AddScript({
            param($Actions, $RemoteSession, $Operation)
            . $Actions
            Invoke-RemoteRequest $Operation $RemoteSession
        }).AddArgument("$PSScriptRoot/ps_actions.ps1").AddArgument($Session).AddArgument($Request)
        $Pending = $Worker.BeginInvoke()
        $NextLine = [Console]::In.ReadLineAsync()
        # Reading the control channel continues while commands and transfers run.
        while (-not $Pending.IsCompleted -and -not $NextLine.IsCompleted) {
            $null = $Pending.AsyncWaitHandle.WaitOne(50)
        }
        if ($NextLine.IsCompleted) {
            $ControlLine = $NextLine.GetAwaiter().GetResult()
            if ($null -eq $ControlLine -or ($ControlLine | ConvertFrom-Json).action -eq "close") {
                Emit @{ type = "lifecycle"; phase = "stopping"; at_ms = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() }
                break
            }
        }
        $null = $Worker.EndInvoke($Pending)
        if ($Worker.HadErrors) { throw $Worker.Streams.Error[0] }
        $Worker.Commands.Clear()
        $Worker.Streams.Error.Clear()
    }
} catch {
    Emit @{ type = "driver_error"; error = $_.Exception.Message }
    exit 1
} finally {
    if ($null -ne $Session) {
        try {
            Emit @{ type = "lifecycle"; phase = "removing"; at_ms = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() }
            Remove-PSSession $Session -ErrorAction SilentlyContinue
            Emit @{ type = "lifecycle"; phase = "removed"; at_ms = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() }
        } catch {}
    }
    if ($null -ne $Worker) {
        try { $Worker.Dispose() } catch {}
    }
}
