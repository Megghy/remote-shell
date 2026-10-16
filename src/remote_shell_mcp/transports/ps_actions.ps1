function Emit($Value) {
    [Console]::Out.WriteLine("__RS_RESP__" + ($Value | ConvertTo-Json -Compress -Depth 16))
    [Console]::Out.Flush()
}

function Get-UploadParameters($Request, $Session) {
    $Source = Get-Item -LiteralPath $Request.local -Force
    $RemoteTarget = Invoke-Command -Session $Session -ScriptBlock {
        param($Path, $Leaf)
        $Existing = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
        if ($null -ne $Existing -and $Existing.PSIsContainer) {
            return Join-Path $Existing.FullName $Leaf
        }
        return $Path
    } -ArgumentList $Request.remote, $Source.Name
    $RemoteExists = Invoke-Command -Session $Session -ScriptBlock {
        param($Path)
        Test-Path -LiteralPath $Path
    } -ArgumentList $RemoteTarget
    if ($RemoteExists -and -not $Request.overwrite) {
        throw "Remote target '$RemoteTarget' already exists."
    }
    Invoke-Command -Session $Session -ScriptBlock {
        param($Path)
        $Parent = Split-Path -Parent $Path
        if ($Parent) {
            $null = [System.IO.Directory]::CreateDirectory($Parent)
        }
    } -ArgumentList $RemoteTarget
    $CopyParams = @{
        LiteralPath = $Source.FullName
        Destination = $RemoteTarget
        ToSession   = $Session
    }
    $Target = $RemoteTarget
    return @{ Parameters = $CopyParams; Source = $Source; Target = $Target }
}

function Get-DownloadParameters($Request, $Session) {
    $RemoteSource = Invoke-Command -Session $Session -ScriptBlock {
        param($Path)
        Get-Item -LiteralPath $Path -Force
    } -ArgumentList $Request.remote
    $LocalTarget = $Request.local
    if ((Test-Path -LiteralPath $LocalTarget -PathType Container)) {
        $LocalTarget = Join-Path $LocalTarget $RemoteSource.Name
    }
    if ((Test-Path -LiteralPath $LocalTarget) -and -not $Request.overwrite) {
        throw "Local target '$LocalTarget' already exists."
    }
    $LocalParent = Split-Path -Parent $LocalTarget
    if ($LocalParent) {
        $null = [System.IO.Directory]::CreateDirectory($LocalParent)
    }
    $CopyParams = @{
        LiteralPath = $Request.remote
        Destination = $LocalTarget
        FromSession = $Session
    }
    $Source = $RemoteSource
    $Target = $LocalTarget
    return @{ Parameters = $CopyParams; Source = $Source; Target = $Target }
}

function Invoke-RemoteCopy($Request, $Session) {
    $Plan = switch ($Request.direction) {
        upload { Get-UploadParameters $Request $Session }
        download { Get-DownloadParameters $Request $Session }
        default { throw "Unknown copy direction '$($Request.direction)'." }
    }
    $CopyParams = $Plan.Parameters
    if ($Plan.Source.PSIsContainer) { $CopyParams.Recurse = $true }
    if ($Request.overwrite) { $CopyParams.Force = $true }
    Copy-Item @CopyParams
    return @{ target = $Plan.Target }
}

function Invoke-RemoteWrite($Request, $Session) {
    return Invoke-Command -Session $Session -ScriptBlock {
        param($Request)
        $ErrorActionPreference = "Stop"
        $Path = [string]$Request.remote
        $Existing = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
        if ($null -ne $Existing -and $Existing.PSIsContainer) {
            throw "Remote target must be a regular file."
        }
        $Parent = Split-Path -Parent $Path
        if ($Parent) {
            $null = [System.IO.Directory]::CreateDirectory($Parent)
        }
        $Bytes = [System.Text.UTF8Encoding]::new($false, $true).GetBytes([string]$Request.content)
        [System.IO.File]::WriteAllBytes($Path, $Bytes)
        @{ bytes = $Bytes.Length; remote_path = (Get-Item -LiteralPath $Path -Force).FullName }
    } -ArgumentList $Request
}

function Invoke-RemoteEdit($Request, $Session) {
    return Invoke-Command -Session $Session -ScriptBlock {
        param($Request)
        $ErrorActionPreference = "Stop"
        $File = Get-Item -LiteralPath $Request.remote -Force
        if ($File.PSIsContainer) { throw "Remote target must be a regular file." }
        $Stream = [System.IO.File]::Open($File.FullName, "Open", "ReadWrite", "None")
        try {
            $Buffer = [System.IO.MemoryStream]::new()
            $Stream.CopyTo($Buffer)
            $Bytes = $Buffer.ToArray()
            $Buffer.Dispose()
            $Utf8 = [System.Text.UTF8Encoding]::new($false, $true)
            $Text = $Utf8.GetString($Bytes)
            $Count = ([regex]::Matches($Text, [regex]::Escape([string]$Request.old_text))).Count
            if ($Count -eq 0 -or ($Count -ne 1 -and -not $Request.replace_all)) {
                $Expected = if ($Request.replace_all) { "at least one" } else { "one" }
                throw "Expected $Expected match; found $Count. File unchanged."
            }
            $Comparison = [System.StringComparison]::Ordinal
            $Updated = if ($Request.replace_all) {
                $Text.Replace([string]$Request.old_text, [string]$Request.new_text, $Comparison)
            } else {
                $Index = $Text.IndexOf([string]$Request.old_text, $Comparison)
                $Text.Substring(0, $Index) + [string]$Request.new_text + $Text.Substring($Index + $Request.old_text.Length)
            }
            $OutputBytes = $Utf8.GetBytes($Updated)
            $Stream.Position = 0
            $Stream.Write($OutputBytes)
            $Stream.SetLength($OutputBytes.Length)
            @{ replacements = $Count; bytes = $OutputBytes.Length; remote_path = $File.FullName }
        } finally { $Stream.Dispose() }
    } -ArgumentList $Request
}

function Invoke-RemoteExecute($Request, $Session) {
    $Result = Invoke-Command -Session $Session -FilePath "$PSScriptRoot/ps_execute.ps1" -ArgumentList $Request.command -ErrorAction Stop
    return @{ exit_code = $Result.exit_code; stdout = $Result.stdout; stderr = $Result.stderr; cwd = $Result.cwd }
}

function Invoke-RemoteRequest($Request, $Session) {
    $ErrorActionPreference = "Stop"
    try {
        $Result = switch ($Request.action) {
            ping {
                Invoke-Command -Session $Session -ScriptBlock { $null } | Out-Null
                @{}
            }
            copy { Invoke-RemoteCopy $Request $Session }
            write { Invoke-RemoteWrite $Request $Session }
            edit { Invoke-RemoteEdit $Request $Session }
            execute { Invoke-RemoteExecute $Request $Session }
            default { throw "Unknown action '$($Request.action)'." }
        }
        $Payload = @{ type = "result"; id = $Request.id; success = $true }
        foreach ($Key in $Result.Keys) { $Payload[$Key] = $Result[$Key] }
        if ($Request.action -eq "execute") { $Payload.success = ($Result.exit_code -eq 0) }
        Emit $Payload
    } catch {
        Emit @{
            type = "result"; id = $Request.id; success = $false; exit_code = 1
            stderr = $_.Exception.Message; connection_lost = ($Session.State -ne "Opened")
        }
    }
}
