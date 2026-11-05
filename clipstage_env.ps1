# =====================================================================================
#  ClipStage - shared PowerShell setup (Windows).  Dot-sourced by clipstage.ps1 and by
#  the scheduled tasks.  Windows port of clipstage_env.sh:
#    * reads KEY=value lines from .env SAFELY (never executes anything in the file)
#    * a variable that is already set in the environment wins over the file
#    * finds Python, talks to Typesense (which runs inside WSL2), mounts the NAS, raises alerts
# =====================================================================================
$ClipDir = $PSScriptRoot
Set-Location -LiteralPath $ClipDir
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }

function Import-ClipEnv {
    $f = Join-Path $ClipDir '.env'
    if (-not (Test-Path -LiteralPath $f)) { return }
    foreach ($line in [IO.File]::ReadAllLines($f)) {
        $t = $line.Trim()
        if ($t -eq '' -or $t.StartsWith('#') -or $t.IndexOf('=') -lt 1) { continue }
        $i = $t.IndexOf('=')
        $k = $t.Substring(0, $i).Trim()
        $v = $t.Substring($i + 1).Trim()
        if ($k -notmatch '^[A-Za-z0-9_]+$') { continue }
        if ($v.Length -ge 2 -and (($v[0] -eq '"' -and $v[$v.Length - 1] -eq '"') -or ($v[0] -eq "'" -and $v[$v.Length - 1] -eq "'"))) {
            $v = $v.Substring(1, $v.Length - 2)
        }
        if (-not [Environment]::GetEnvironmentVariable($k, 'Process')) {
            [Environment]::SetEnvironmentVariable($k, $v, 'Process')
        }
    }
}

function Get-ClipEnv([string]$Name, [string]$Default = '') {
    $v = [Environment]::GetEnvironmentVariable($Name, 'Process')
    if ([string]::IsNullOrEmpty($v)) { return $Default }
    return $v
}

# Update/append KEY=VALUE lines in .env (UTF-8, no BOM, LF). Values are written verbatim.
function Set-ClipEnv {
    param([Parameter(Mandatory)][hashtable]$Values, [string]$File = (Join-Path $ClipDir '.env'))
    $lines = New-Object System.Collections.Generic.List[string]
    if (Test-Path -LiteralPath $File) {
        foreach ($l in [IO.File]::ReadAllLines($File)) { $lines.Add($l) }
    }
    while ($lines.Count -gt 0 -and $lines[$lines.Count - 1] -eq '') { $lines.RemoveAt($lines.Count - 1) }
    foreach ($k in $Values.Keys) {
        if ($k -notmatch '^[A-Za-z0-9_]+$') { throw "bad .env key: $k" }
        $v = [string]$Values[$k]
        if ($v.Contains("`n") -or $v.Contains("`r")) { throw "value for $k contains a line break" }
        $new = "$k=$v"
        $found = $false
        for ($i = 0; $i -lt $lines.Count; $i++) {
            if ($lines[$i].StartsWith("$k=")) { $lines[$i] = $new; $found = $true; break }
        }
        if (-not $found) { $lines.Add($new) }
        [Environment]::SetEnvironmentVariable($k, $v, 'Process')
    }
    [IO.File]::WriteAllText($File, (($lines -join "`n") + "`n"), (New-Object System.Text.UTF8Encoding($false)))
}

function Get-ClipEnvFromFile([string]$Name, [string]$File = (Join-Path $ClipDir '.env')) {
    if (-not (Test-Path -LiteralPath $File)) { return '' }
    foreach ($l in [IO.File]::ReadAllLines($File)) {
        if ($l.StartsWith("$Name=")) { return $l.Substring($Name.Length + 1).Trim() }
    }
    return ''
}

Import-ClipEnv

# ---- Python --------------------------------------------------------------------------
$Python = Get-ClipEnv 'CLIPSTAGE_PYTHON'
if (-not $Python -or -not (Test-Path -LiteralPath $Python)) { $Python = Join-Path $ClipDir '.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $Python)) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $Python = $cmd.Source }
}

# ---- Typesense (runs inside WSL2) ----------------------------------------------------
$TsHost   = Get-ClipEnv 'TYPESENSE_HOST' 'localhost'
$TsPort   = Get-ClipEnv 'TYPESENSE_PORT' '8108'
function Get-Distro { return (Get-ClipEnv 'CLIPSTAGE_WSL_DISTRO' 'ClipStage') }
$TsTask   = 'ClipStage-Typesense'
# Search engine: 'sqlite' (default on Windows: built into Python, nothing extra to run) or 'typesense'
# (the older option: runs inside WSL2). Same web page and results either way.
function Use-Typesense { return ((Get-ClipEnv 'SEARCH_BACKEND' 'sqlite').Trim().ToLower() -eq 'typesense') }
$ApiTask  = 'ClipStage-API'

function Test-Typesense {
    if (-not (Use-Typesense)) { return $true }          # SQLite search needs no server
    try {
        $r = Invoke-RestMethod -Uri "http://${TsHost}:${TsPort}/health" -TimeoutSec 3
        return [bool]$r.ok
    } catch { return $false }
}

function Test-ClipApi {
    try {
        $r = Invoke-RestMethod -Uri ("http://127.0.0.1:" + (Get-ClipEnv 'CLIPSTAGE_API_PORT' '8000') + "/health") -TimeoutSec 3
        return ($r.status -eq 'ok')
    } catch { return $false }
}

# Runs typesense-server in the foreground inside WSL2. The scheduled task keeps this process
# alive, which is also what keeps the WSL2 virtual machine running.
function Start-TypesenseForeground {
    $log = Join-Path $ClipDir 'typesense.log'
    $exe = $env:ComSpec
    $inner = 'wsl.exe -d ' + (Get-Distro) + ' -u root --exec /opt/typesense/typesense-server --config=/etc/typesense/typesense-server.ini >> "' + $log + '" 2>&1'
    $p = Start-Process -FilePath $exe -ArgumentList ('/c "' + $inner + '"') -WindowStyle Hidden -PassThru -Wait
    return $p.ExitCode
}

# Make sure Typesense answers. It is normally started by its own scheduled task at logon.
function Ensure-Typesense([int]$WaitSeconds = 60) {
    if (-not (Use-Typesense)) { return $true }          # SQLite search needs no server
    if (Test-Typesense) { return $true }
    $task = Get-ScheduledTask -TaskName $TsTask -ErrorAction SilentlyContinue
    if ($task) {
        if ($task.State -ne 'Running') { Start-ScheduledTask -TaskName $TsTask }
    } else {
        Start-Process -FilePath 'wsl.exe' -ArgumentList @('-d', (Get-Distro), '-u', 'root', '--exec', '/opt/typesense/typesense-server', '--config=/etc/typesense/typesense-server.ini') -WindowStyle Hidden | Out-Null
    }
    for ($i = 0; $i -lt $WaitSeconds; $i++) {
        if (Test-Typesense) { return $true }
        Start-Sleep -Seconds 1
    }
    return $false
}

# ---- logging / alerts ----------------------------------------------------------------
function Write-ClipLog([string]$Message, [string]$File = 'indexer.log') {
    $line = $Message
    Add-Content -LiteralPath (Join-Path $ClipDir $File) -Value $line -Encoding UTF8
}

function Send-ClipAlert([string]$Message) {
    $msg = $Message.Replace('"', "'")
    Write-Output "ALERT: $msg"
    $hook = Get-ClipEnv 'CLIPSTAGE_ALERT_WEBHOOK'
    if ($hook) {
        try {
            $body = @{ text = "ClipStage: $msg" } | ConvertTo-Json -Compress
            Invoke-RestMethod -Uri $hook -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 15 | Out-Null
        } catch { }
    }
}

function Assert-ClipKey {
    if ((Use-Typesense) -and -not (Get-ClipEnv 'TYPESENSE_KEY')) {
        Send-ClipAlert "TYPESENSE_KEY is not set - put it in $ClipDir\.env"
        exit 1
    }
}

# Run an exe with stdout+stderr appended (raw bytes) to a log file, hidden, and return its exit code.
function Invoke-Logged {
    param([string]$Exe, [string]$Arguments, [string]$Log, [switch]$NoWait)
    $inner = '"' + $Exe + '" ' + $Arguments + ' >> "' + $Log + '" 2>&1'
    $p = Start-Process -FilePath $env:ComSpec -ArgumentList ('/c "' + $inner + '"') -WindowStyle Hidden -PassThru -Wait:(-not $NoWait) -WorkingDirectory $ClipDir
    if ($NoWait) { return 0 }
    return $p.ExitCode
}

# ---- NAS volumes ---------------------------------------------------------------------
# macOS mounts shares at /Volumes/<share>. Here the same layout is C:\Volumes\<share>, a
# directory link to \\NAS\<share>, so indexed paths and staged links look the same everywhere.
function Get-ClipVolumes {
    $raw = Get-ClipEnv 'CLIPSTAGE_SCAN_VOLUMES' 'EDIT,EDIT2,INGEST,PLAYOUT,DIGITAL,SHARE FOLDER,TRANSCODER'
    return @($raw.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

function Test-Reachable([string]$Path) {
    try { return [bool](Test-Path -LiteralPath $Path -ErrorAction Stop) } catch { return $false }
}

function Test-TcpPort([string]$HostName, [int]$Port, [int]$TimeoutMs = 3000) {
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $iar = $c.BeginConnect($HostName, $Port, $null, $null)
        $ok = $iar.AsyncWaitHandle.WaitOne($TimeoutMs, $false) -and $c.Connected
        $c.Close()
        return $ok
    } catch { return $false }
}

function Get-NasUser {
    $u = Get-ClipEnv 'CLIPSTAGE_NAS_USER'
    $d = Get-ClipEnv 'CLIPSTAGE_NAS_DOMAIN'
    if ($d) { return "$d\$u" }
    return $u
}

# Creates/repairs C:\Volumes\<share> -> \\NAS\<share> and reports one line per share, in the same
# format as mount_volumes.sh ("  EDIT: OK (already mounted)" / RECONNECTED / FAILED - reason).
# Returns the number of failed shares.
function Invoke-MountVolumes {
    $nas = Get-ClipEnv 'CLIPSTAGE_NAS_HOST'
    $root = (Get-ClipEnv 'CLIPSTAGE_VOLUMES_ROOT' 'C:/Volumes').Replace('/', '\')
    if (-not $nas) {
        Write-Output "mount: CLIPSTAGE_NAS_HOST is not set - assuming C:\Volumes\<share> already exists."
        return 0
    }
    if (-not (Test-Path -LiteralPath $root)) { New-Item -ItemType Directory -Path $root -Force | Out-Null }
    $reachable = Test-TcpPort $nas 445
    $failed = 0; $ok = 0
    foreach ($vol in (Get-ClipVolumes)) {
        $link = Join-Path $root $vol
        $unc = "\\$nas\$vol"
        $item = Get-Item -LiteralPath $link -Force -ErrorAction SilentlyContinue
        $isLink = $item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)
        if ($item -and -not $isLink) {
            Write-Output "  ${vol}: FAILED - $link exists but is a normal folder, not a link to the NAS"
            $failed++; continue
        }
        $created = $false
        if ($isLink -and ([string]$item.Target) -ne $unc) {
            cmd.exe /c rmdir "$link" | Out-Null
            $isLink = $false
        }
        if (-not $isLink) {
            cmd.exe /c mklink /D "$link" "$unc" | Out-Null
            $created = $true
        }
        if (-not $reachable) {
            Write-Output "  ${vol}: FAILED - NAS $nas is not answering on port 445"
            $failed++; continue
        }
        $was = (Test-Reachable "$link\")
        if (-not $was) {
            $user = Get-NasUser; $pw = Get-ClipEnv 'CLIPSTAGE_NAS_PASSWORD'
            if ($user -and $pw) { & cmdkey.exe /add:$nas /user:$user /pass:$pw | Out-Null }
            Start-Sleep -Milliseconds 500
        }
        if (Test-Reachable "$link\") {
            if ($was -and -not $created) { Write-Output "  ${vol}: OK (already mounted)" }
            else { Write-Output "  ${vol}: RECONNECTED (was missing, reachable now)" }
            $ok++
        } else {
            Write-Output "  ${vol}: FAILED - cannot open $unc (check the NAS login / share name)"
            $failed++
        }
    }
    Write-Output "mount: $ok reachable, $failed failed"
    return $failed
}

# Wraps Invoke-MountVolumes: .Lines = the per-share report lines, .Failed = number of failed shares.
function Get-MountResult {
    $out = @(Invoke-MountVolumes)
    $failed = 0
    $lines = @()
    if ($out.Count -gt 0) {
        $failed = [int]$out[$out.Count - 1]
        if ($out.Count -gt 1) { $lines = @($out[0..($out.Count - 2)]) }
    }
    return [pscustomobject]@{ Lines = $lines; Failed = $failed }
}
