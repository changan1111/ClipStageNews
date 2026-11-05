# =====================================================================================
#  ClipStage - one command for everything on the middle machine (Windows).
#
#    .\clipstage.ps1 status             is everything running?
#    .\clipstage.ps1 start | stop | restart
#    .\clipstage.ps1 logs [api|index|typesense]
#    .\clipstage.ps1 index [VOLUME ..]  index now (all volumes, or only those named)
#    .\clipstage.ps1 stop-index        stop a running index safely (same as the Stop button)
#    .\clipstage.ps1 thumbs [N]         make up to N new thumbnails (default 2000)
#    .\clipstage.ps1 backup             copy clipstage.db to backups\db now
#    .\clipstage.ps1 open               open the app in the browser
#    .\clipstage.ps1 editor-kit         (re)build the editor kit share (see install-win-host.ps1)
#
#  Used by the scheduled tasks:  api-run | typesense-run | nightly | mountwatch
# =====================================================================================
param(
    [Parameter(Position = 0)][string]$Command = 'status',
    [Parameter(Position = 1, ValueFromRemainingArguments = $true)][string[]]$Rest
)
. (Join-Path $PSScriptRoot 'clipstage_env.ps1')
$port = Get-ClipEnv 'CLIPSTAGE_API_PORT' '8000'

function Stop-ClipApiProcess {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'uvicorn\s+api:app' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Show-Status {
    function Line($label, $ok, $okText = 'running', $badText = 'NOT running') {
        $color = 'Red'; $text = $badText
        if ($ok) { $color = 'Green'; $text = $okText }
        Write-Host ($label.PadRight(18)) -NoNewline
        Write-Host $text -ForegroundColor $color
    }
    Line "API (port $port):" (Test-ClipApi)
    if (Use-Typesense) { Line 'Typesense:' (Test-Typesense) } else { Line 'Search engine:' $true 'SQLite (built in)' }
    $taskNames = @($ApiTask)
    if (Use-Typesense) { $taskNames += $TsTask }
    $taskNames += @('ClipStage-Nightly', 'ClipStage-MountWatch')
    foreach ($t in $taskNames) {
        $task = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
        if ($task) { Line "$t`:" $true ([string]$task.State) } else { Line "$t`:" $false '' 'not installed' }
    }
    $statusFile = Join-Path $ClipDir 'index_status.json'
    if (Test-Path -LiteralPath $statusFile) {
        try {
            $s = Get-Content -LiteralPath $statusFile -Raw | ConvertFrom-Json
            $extra = ''
            if ($s.state -eq 'running' -and $s.phase) { $extra = " - $($s.phase)" }
            Write-Host ('Index:'.PadRight(18) + "$($s.state)$extra")
            if ($s.finished_at) {
                $when = [DateTimeOffset]::FromUnixTimeSeconds([long]$s.finished_at).LocalDateTime
                Write-Host ('Last finished:'.PadRight(18) + $when.ToString('yyyy-MM-dd HH:mm'))
            }
            if ($s.last_warning) { Write-Host ('WARNING:'.PadRight(18) + $s.last_warning) -ForegroundColor Yellow }
        } catch { }
    }
}

switch ($Command.ToLower()) {
    'status' { Show-Status }

    'start' {
        Start-ScheduledTask -TaskName $TsTask -ErrorAction SilentlyContinue
        Start-ScheduledTask -TaskName $ApiTask
        Write-Host 'Starting... (give it ~30 seconds, then run: .\clipstage.ps1 status)'
    }
    'stop' {
        Stop-ScheduledTask -TaskName $ApiTask -ErrorAction SilentlyContinue
        Stop-ClipApiProcess
        Write-Host 'API stopped. Typesense keeps running (use "stop-all" to stop it too).'
    }
    'stop-all' {
        Stop-ScheduledTask -TaskName $ApiTask -ErrorAction SilentlyContinue
        Stop-ClipApiProcess
        Stop-ScheduledTask -TaskName $TsTask -ErrorAction SilentlyContinue
        & wsl.exe -t (Get-Distro) | Out-Null
        Write-Host 'Everything stopped.'
    }
    'restart' {
        Stop-ScheduledTask -TaskName $ApiTask -ErrorAction SilentlyContinue
        Stop-ClipApiProcess
        Start-Sleep -Seconds 2
        Start-ScheduledTask -TaskName $ApiTask
        Write-Host 'API restarted.'
    }

    'logs' {
        $which = 'index'
        if ($Rest -and $Rest.Count -gt 0) { $which = $Rest[0] }
        $file = switch ($which) { 'api' { 'api.log' } 'typesense' { 'typesense.log' } default { 'indexer.log' } }
        $path = Join-Path $ClipDir $file
        if (-not (Test-Path -LiteralPath $path)) { New-Item -ItemType File -Path $path | Out-Null }
        Get-Content -LiteralPath $path -Tail 40 -Wait
    }

    'index' {
        Assert-ClipKey
        $m = Get-MountResult
        $m.Lines | ForEach-Object { Write-Host $_ }
        if (-not (Ensure-Typesense)) { Send-ClipAlert 'Typesense did not start - index not run'; exit 1 }
        $argList = @('indexer.py', '--prune')
        if ($Rest -and $Rest.Count -gt 0) { $argList += @('--only', (($Rest | ForEach-Object { $_.Trim(',') }) -join ',')) }
        $ErrorActionPreference = 'Continue'
        & $Python @argList
        exit $LASTEXITCODE
    }
    'stop-index' {
        $running = $false
        try { $running = @('running', 'stalled') -contains ((Get-Content -LiteralPath (Join-Path $ClipDir 'index_status.json') -Raw | ConvertFrom-Json).state) } catch { }
        if ($running) {
            [IO.File]::WriteAllText((Join-Path $ClipDir 'index_stop.flag'), [string][DateTimeOffset]::UtcNow.ToUnixTimeSeconds())
            Write-Host 'Stop requested - the index ends at its next checkpoint (within seconds). Nothing is deleted.'
        } else { Write-Host 'No index is running.' }
    }
    'thumbs' {
        Assert-ClipKey
        $n = '2000'
        if ($Rest -and $Rest.Count -gt 0) { $n = $Rest[0] }
        $ErrorActionPreference = 'Continue'
        & $Python generate_thumbs.py --limit $n
        exit $LASTEXITCODE
    }
    'backup' {
        $ErrorActionPreference = 'Continue'
        & $Python backup_db.py
        exit $LASTEXITCODE
    }
    'open' { Start-Process "http://127.0.0.1:$port" }

    # ---------------- used by scheduled tasks -----------------------------------------
    'typesense-run' {
        # blocks for as long as Typesense runs; the task restarts it if it ever stops
        exit (Start-TypesenseForeground)
    }
    'api-run' {
        Assert-ClipKey
        $log = Join-Path $ClipDir 'api.log'
        $m = Get-MountResult
        $m.Lines | ForEach-Object { Write-ClipLog $_ 'api.log' }
        if (-not (Ensure-Typesense 90)) {
            Send-ClipAlert 'Typesense is not running - API not started'
            Write-ClipLog 'Typesense is not running - API not started' 'api.log'
            exit 1
        }
        Stop-ClipApiProcess
        $bind = Get-ClipEnv 'CLIPSTAGE_BIND_HOST' '127.0.0.1'
        $fwd = Get-ClipEnv 'CLIPSTAGE_FORWARDED_ALLOW_IPS' '127.0.0.1'
        Write-ClipLog ("=== Starting ClipStage API on ${bind}:${port} " + (Get-Date)) 'api.log'
        # ONE worker only: edit locks and rate limits are kept in memory.
        $a = '-m uvicorn api:app --host ' + $bind + ' --port ' + $port + ' --proxy-headers --forwarded-allow-ips ' + $fwd
        exit (Invoke-Logged -Exe $Python -Arguments $a -Log $log)
    }
    'nightly' {
        $log = Join-Path $ClipDir 'indexer.log'
        Write-ClipLog ("=== Scheduled index " + (Get-Date))
        Assert-ClipKey
        $m = Get-MountResult
        $m.Lines | ForEach-Object { Write-ClipLog $_ }
        if (-not (Ensure-Typesense 90)) {
            Send-ClipAlert 'Nightly index NOT run - Typesense did not start'
            Write-ClipLog '=== FAILED: Typesense did not start'
            exit 1
        }
        $rc = Invoke-Logged -Exe $Python -Arguments 'indexer.py --prune' -Log $log
        if ($rc -ne 0) {
            Send-ClipAlert "Nightly index FAILED (exit $rc) - see indexer.log"
        } else {
            try {
                $s = Get-Content -LiteralPath (Join-Path $ClipDir 'index_status.json') -Raw | ConvertFrom-Json
                if ($s.last_warning) { Send-ClipAlert "Index warning: $($s.last_warning)" }
            } catch { }
            if ((Get-ClipEnv 'CLIPSTAGE_THUMBS' '1') -eq '1') {
                $lim = Get-ClipEnv 'CLIPSTAGE_THUMBS_LIMIT' '5000'
                $trc = Invoke-Logged -Exe $Python -Arguments ('generate_thumbs.py --limit ' + $lim) -Log $log
                if ($trc -ne 0) { Send-ClipAlert 'Thumbnail run failed - see indexer.log' }
            }
        }
        $brc = Invoke-Logged -Exe $Python -Arguments 'backup_db.py' -Log $log
        if ($brc -ne 0) { Send-ClipAlert 'clipstage.db backup failed - see indexer.log' }
        Write-ClipLog ("=== Finished (exit $rc) " + (Get-Date))
        exit $rc
    }
    'mountwatch' {
        $log = 'mount_watch.log'
        Write-ClipLog ('--- ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' ---') $log
        $m = Get-MountResult
        $lines = @($m.Lines)
        $lines | ForEach-Object { Write-ClipLog $_ $log }
        $failed = ($lines | Where-Object { $_ -match ': FAILED' } | ForEach-Object { ($_ -split ':')[0].Trim() }) -join ','
        $recon = ($lines | Where-Object { $_ -match ': RECONNECTED' } | ForEach-Object { ($_ -split ':')[0].Trim() }) -join ','
        if ($failed) { Send-ClipAlert "NAS volume(s) NOT reachable: $failed - see mount_watch.log" }
        elseif ($recon) { Send-ClipAlert "NAS volume(s) reconnected: $recon" }
        if ($m.Failed -gt 0) { exit 1 } else { exit 0 }
    }

    'editor-kit' {
        & (Join-Path $ClipDir 'install-win-host.ps1') -EditorKitOnly
    }

    default {
        Get-Content -LiteralPath $PSCommandPath -TotalCount 15 | ForEach-Object { $_ -replace '^#\s?', '' }
        exit 2
    }
}
