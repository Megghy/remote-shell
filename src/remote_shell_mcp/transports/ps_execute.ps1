param([string]$__rs_command)

$global:LASTEXITCODE = 0
$__rs_code = 0
$__rs_buffers = @{}
foreach ($__rs_name in @('stdout', 'stderr')) {
    $__rs_buffers[$__rs_name] = @{
        Head = [System.Text.StringBuilder]::new()
        Tail = [System.Text.StringBuilder]::new()
        Total = 0L
    }
}
$__rs_append = {
    param($Buffer, [string]$Text)
    $Buffer.Total += $Text.Length
    $Take = [Math]::Min(131072 - $Buffer.Head.Length, $Text.Length)
    if ($Take -gt 0) { $null = $Buffer.Head.Append($Text.Substring(0, $Take)) }
    $Rest = $Text.Substring($Take)
    if ($Rest.Length -gt 65536) { $Rest = $Rest.Substring($Rest.Length - 65536) }
    $null = $Buffer.Tail.Append($Rest)
    if ($Buffer.Tail.Length -gt 65536) { $null = $Buffer.Tail.Remove(0, $Buffer.Tail.Length - 65536) }
}
. {
    try {
        . ([ScriptBlock]::Create($__rs_command))
        if (-not $?) { $__rs_code = 1 }
        if ($LASTEXITCODE -ne 0) { $__rs_code = [int]$LASTEXITCODE }
    } catch {
        $__rs_code = 1
        Write-Error -Message $_.Exception.Message -ErrorAction Continue
    }
} *>&1 | ForEach-Object {
    if ($_ -is [System.Management.Automation.ErrorRecord]) {
        & $__rs_append $__rs_buffers.stderr ($_.ToString() + [Environment]::NewLine)
    } else {
        $_
    }
} | Out-String -Stream -Width 200 | ForEach-Object {
    & $__rs_append $__rs_buffers.stdout ($_ + [Environment]::NewLine)
}
$__rs_render = {
    param($Buffer)
    $Omitted = $Buffer.Total - $Buffer.Head.Length - $Buffer.Tail.Length
    $Gap = if ($Omitted -gt 0) { "`n... [$Omitted characters omitted; $($Buffer.Total) total] ...`n" } else { '' }
    return $Buffer.Head.ToString() + $Gap + $Buffer.Tail.ToString()
}
[pscustomobject]@{
    exit_code = $__rs_code
    stdout = (& $__rs_render $__rs_buffers.stdout)
    stderr = (& $__rs_render $__rs_buffers.stderr)
    cwd = (Get-Location).Path
}
