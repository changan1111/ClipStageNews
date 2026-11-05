# ClipStage - uninstaller (Windows middle machine).
#
#   powershell -ExecutionPolicy Bypass -File .\uninstall_clipstage.ps1 -DryRun    show what would happen, change nothing
#   powershell -ExecutionPolicy Bypass -File .\uninstall_clipstage.ps1            interactive: asks before every optional step
#
# ALWAYS (after one confirmation): stops the API, the indexer and Typesense and removes the scheduled
#   tasks ClipStage-API, -Typesense, -Nightly, -MountWatch and -Install-Resume.
# OPTIONAL (each asked separately, default = No):
#   1. final backup bundle (index snapshot + database + .env + editors + thumbnails)
#   2. remove the shares "staging" and "clipstage-setup" and the local account clipstage-editor
#   3. remove the firewall rule "ClipStage API"
#   4. delete the staged links folder  C:\ClipStage-Staging   (only the links, never the footage)
#   5. remove the NAS folder links C:\Volumes\<share> and the stored NAS login
#   6. delete the search index (the ClipStage WSL Linux and its data)
#   7. delete C:\ClipStage-tools (ffmpeg copy, Typesense download)
#   8. delete the whole ClipStage folder  C:\ClipStage  (database, .env, thumbnails, backups, .venv)
#
# NEVER touched: NAS volumes and their files, Supabase, Python / ffmpeg installed by winget,
#   the Windows WSL feature, the editors' PCs.
#
# Links are removed one by one and never followed, so a link to the NAS can not delete NAS files.

param([switch]$DryRun)

$ErrorActionPreference = 'Continue'

# ---- administrator ------------------------------------------------------------------------------
$me = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = (New-Object Security.Principal.WindowsPrincipal $me).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host 'Asking for administrator rights...'
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $PSCommandPath + '"'))
    if ($DryRun) { $argList += '-DryRun' }
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $argList
    exit
}

# ---- names used by install-win-host.ps1 ---------------------------------------------------------
$Tasks        = @('ClipStage-API', 'ClipStage-Typesense', 'ClipStage-Nightly', 'ClipStage-MountWatch', 'ClipStage-Install-Resume')
$StagingShare = 'staging'
$KitShare     = 'clipstage-setup'
$EditorUser   = 'clipstage-editor'
$FwRule       = 'ClipStage API'
$StagingDir   = 'C:\ClipStage-Staging'
$ToolsDir     = 'C:\ClipStage-tools'
$WslHome      = 'C:\ClipStage-wsl'
$VolumesRoot  = 'C:\Volumes'
$InstallLog   = 'C:\ClipStage-install.log'

if ($PSScriptRoot -and (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'api.py'))) { $Dir = $PSScriptRoot } else { $Dir = 'C:\ClipStage' }

# ---- small helpers ------------------------------------------------------------------------------
function Say([string]$t)  { Write-Host $t }
function Ok([string]$t)   { Write-Host '  [ok] ' -ForegroundColor Green -NoNewline; Write-Host $t }
function Skip([string]$t) { Write-Host '  [--] ' -ForegroundColor DarkGray -NoNewline; Write-Host $t }
function Warn([string]$t) { Write-Host '  [!!] ' -ForegroundColor Yellow -NoNewline; Write-Host $t }

function AskYn([string]$q) {
    $a = Read-Host ($q + ' [y/N]')
    return ($a -match '^(y|yes)$')
}
function AskWord([string]$q, [string]$word) {
    $a = Read-Host $q
    return ($a -ceq $word)
}

# Run a block, or only print its description in -DryRun mode.
function Do([string]$what, [scriptblock]$block) {
    if ($DryRun) { Write-Host ('  (dry-run) ' + $what); return $true }
    try { & $block; return $true } catch { Warn ($what + ' - ' + $_.Exception.Message); return $false }
}

function Read-EnvValue([string]$name) {
    $f = Join-Path $Dir '.env'
    if (-not (Test-Path -LiteralPath $f)) { return '' }
    foreach ($line in (Get-Content -LiteralPath $f -ErrorAction SilentlyContinue)) {
        if ($line -match ('^\s*' + [regex]::Escape($name) + '\s*=\s*(.*)$')) { return $Matches[1].Trim().Trim('"').Trim("'") }
    }
    return ''
}

function Test-IsLink([string]$path) {
    try {
        $it = Get-Item -LiteralPath $path -Force -ErrorAction Stop
        return (($it.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
    } catch { return $false }
}

# Delete a link (file or folder) WITHOUT following it. Never touches what it points to.
function Remove-OneLink([string]$path) {
    $it = Get-Item -LiteralPath $path -Force -ErrorAction Stop
    if ($it.PSIsContainer) {
        try { [IO.Directory]::Delete($path) }
        catch { & cmd.exe /c ('rmdir "' + $path + '"') | Out-Null }
    } else {
        [IO.File]::Delete($path)
    }
}

# Delete a folder tree. Every link inside is removed as a link; real folders are walked, links are not.
function Remove-TreeSafe([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return }
    if (Test-IsLink $path) { Remove-OneLink $path; return }
    foreach ($child in @(Get-ChildItem -LiteralPath $path -Force -ErrorAction SilentlyContinue)) {
        if (($child.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            try { Remove-OneLink $child.FullName } catch { Warn ('could not remove link ' + $child.FullName) }
        } elseif ($child.PSIsContainer) {
            Remove-TreeSafe $child.FullName
        } else {
            try { [IO.File]::SetAttributes($child.FullName, [IO.FileAttributes]::Normal); [IO.File]::Delete($child.FullName) }
            catch { Warn ('could not delete ' + $child.FullName) }
        }
    }
    try { [IO.Directory]::Delete($path) } catch { Warn ('could not remove folder ' + $path + ' (in use?)') }
}

function Get-WslNames {
    $out = & wsl.exe --list --quiet 2>$null
    $names = @()
    foreach ($l in @($out)) { $n = ([string]$l -replace "`0", '').Trim(); if ($n) { $names += $n } }
    return $names
}

# ---- start --------------------------------------------------------------------------------------
Set-Location -LiteralPath $env:SystemDrive
Say ''
Say 'ClipStage uninstaller (Windows)'
Say ('  ClipStage folder: ' + $Dir)
if ($DryRun) { Say '  DRY RUN - nothing will be changed.' }
if (-not (Test-Path -LiteralPath $Dir)) { Warn ('Folder ' + $Dir + ' not found - only services and shares will be cleaned.') }
Say ''
Say 'This will stop ClipStage for everyone and remove its scheduled tasks.'
Say 'If this PC is being replaced, confirm the NEW machine works first.'
Say ''
if (-not (AskYn 'Stop ClipStage and remove its tasks?')) { Say 'Nothing changed.'; Read-Host 'Press Enter to close'; exit 0 }

$DbFile  = Join-Path $Dir 'clipstage.db'
$Distro  = Read-EnvValue 'CLIPSTAGE_WSL_DISTRO'
if (-not $Distro) { $Distro = 'ClipStage' }
$NasHost = Read-EnvValue 'CLIPSTAGE_NAS_HOST'
$Bundle  = ''

# ---- 1. optional final backup (BEFORE anything is stopped: the snapshot needs Typesense) --------
Say ''
Say '1. Final backup'
if (AskYn '   Save a final backup bundle first? (recommended)') {
    $Bundle = Join-Path $env:USERPROFILE ('ClipStage-final-backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    [void](Do ('create ' + $Bundle) { New-Item -ItemType Directory -Path $Bundle -Force | Out-Null })
    $snap = Join-Path $Dir 'snapshot_index.py'
    if (Test-Path -LiteralPath $snap) {
        [void](Do 'index snapshot (snapshot_index.py)' {
            Push-Location -LiteralPath $Dir
            try {
                . (Join-Path $Dir 'clipstage_env.ps1')
                & $Python 'snapshot_index.py'
                if ($LASTEXITCODE -ne 0) { throw 'snapshot_index.py failed (is Typesense running?)' }
            } finally { Pop-Location }
            Ok ('index snapshot written to ' + (Join-Path $Dir 'backups'))
        })
    } else { Skip 'snapshot_index.py not found - index snapshot skipped' }
    foreach ($f in @($DbFile, ($DbFile + '-wal'), ($DbFile + '-shm'))) {
        if (Test-Path -LiteralPath $f) { [void](Do ('copy ' + $f) { Copy-Item -LiteralPath $f -Destination $Bundle -Force }) }
    }
    if (Test-Path -LiteralPath $DbFile) { Ok 'database copied' } else { Skip 'no clipstage.db found' }
    $envF = Join-Path $Dir '.env'
    if (Test-Path -LiteralPath $envF) { [void](Do 'copy .env' { Copy-Item -LiteralPath $envF -Destination (Join-Path $Bundle 'env.backup') -Force }); Ok '.env copied (as env.backup)' }
    $edF = Join-Path $Dir 'editors.json'
    if (Test-Path -LiteralPath $edF) { [void](Do 'copy editors.json' { Copy-Item -LiteralPath $edF -Destination $Bundle -Force }); Ok 'editors.json copied' }
    $bk = Join-Path $Dir 'backups'
    if (Test-Path -LiteralPath $bk) { [void](Do 'copy backups folder' { Copy-Item -LiteralPath $bk -Destination (Join-Path $Bundle 'backups') -Recurse -Force }); Ok 'backups folder copied' }
    $th = Join-Path $Dir 'static\thumbs'
    if (Test-Path -LiteralPath $th) {
        if (AskYn '   Also copy the thumbnails folder? (can be large)') {
            [void](Do 'copy thumbnails' { Copy-Item -LiteralPath $th -Destination (Join-Path $Bundle 'thumbs') -Recurse -Force }); Ok 'thumbnails copied'
        } else { Skip 'thumbnails not copied' }
    }
    Say ('   Bundle: ' + $Bundle)
    Say '   It contains .env (passwords). Copy it off this PC (NAS or USB disk) and keep it private.'
} else { Skip 'no final backup' }

# ---- 2. stop and remove the tasks (always) -------------------------------------------------------
Say ''
Say '2. Stopping and removing tasks'
foreach ($t in $Tasks) {
    if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
        [void](Do ('remove task ' + $t) {
            Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction Stop
        })
        Ok ('removed ' + $t)
    } else { Skip ($t + ' was not installed') }
}
[void](Do 'stop API, indexer and thumbnail processes' {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'uvicorn\s+api:app|indexer\.py|generate_thumbs\.py|snapshot_index\.py' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
})
if ((Get-WslNames) -contains $Distro) {
    [void](Do ('stop WSL distro ' + $Distro) { & wsl.exe --terminate $Distro | Out-Null })
}
Ok 'API, indexer, thumbnail job and Typesense stopped'

# ---- 3. shares and editor account ----------------------------------------------------------------
Say ''
Say '3. Staging share, setup share and the editors'' account'
Say '   Editors can no longer open staged clips after this.'
if (AskYn '   Remove both shares and the local account clipstage-editor?') {
    foreach ($pair in @(@($StagingShare, $StagingDir), @($KitShare, (Join-Path $Dir 'editor-kit')))) {
        $s = Get-SmbShare -Name $pair[0] -ErrorAction SilentlyContinue
        if ($s) {
            if ($s.Path.TrimEnd('\') -ieq $pair[1].TrimEnd('\')) {
                [void](Do ('remove share ' + $pair[0]) { Remove-SmbShare -Name $pair[0] -Force })
                Ok ('share ' + $pair[0] + ' removed')
            } else { Warn ('share ' + $pair[0] + ' points to ' + $s.Path + ', not a ClipStage folder - left alone') }
        } else { Skip ('no share named ' + $pair[0]) }
    }
    if (Get-LocalUser -Name $EditorUser -ErrorAction SilentlyContinue) {
        [void](Do ('remove local user ' + $EditorUser) { Remove-LocalUser -Name $EditorUser })
        Ok ('account ' + $EditorUser + ' removed')
    } else { Skip ('no account ' + $EditorUser) }
} else { Skip 'shares and account left in place' }

# ---- 4. firewall ---------------------------------------------------------------------------------
Say ''
Say '4. Firewall rule'
if (AskYn ('   Remove the rule "' + $FwRule + '"?')) {
    if (Get-NetFirewallRule -DisplayName $FwRule -ErrorAction SilentlyContinue) {
        [void](Do 'remove firewall rule' { Get-NetFirewallRule -DisplayName $FwRule | Remove-NetFirewallRule })
        Ok 'firewall rule removed'
    } else { Skip 'no such rule' }
} else { Skip 'firewall left as is' }

# ---- 5. staged links -----------------------------------------------------------------------------
Say ''
Say ('5. Staged clips folder  (' + $StagingDir + ')')
if (Test-Path -LiteralPath $StagingDir) {
    Say '   It holds only links to clips on the NAS. Deleting it removes the links, never the footage.'
    if (AskYn '   Delete it?') {
        if ($DryRun) { Say ('  (dry-run) remove links in ' + $StagingDir + ' one by one, then the folder') }
        else { Remove-TreeSafe $StagingDir; if (-not (Test-Path -LiteralPath $StagingDir)) { Ok 'staging folder removed' } }
    } else { Skip 'staging folder kept' }
} else { Skip 'no staging folder' }

# ---- 6. NAS links and stored login ---------------------------------------------------------------
Say ''
Say ('6. NAS folder links  (' + $VolumesRoot + '\<share>)  and stored NAS login')
if (Test-Path -LiteralPath $VolumesRoot) {
    Say '   Each entry is a link to a NAS share. Only the link is removed, never the NAS files.'
    if (AskYn '   Remove the links?') {
        foreach ($c in @(Get-ChildItem -LiteralPath $VolumesRoot -Force -ErrorAction SilentlyContinue)) {
            if (($c.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                [void](Do ('remove link ' + $c.FullName) { Remove-OneLink $c.FullName })
                Ok ('link removed: ' + $c.Name)
            } else { Warn ($c.FullName + ' is a real folder, not a link - left alone') }
        }
        if (-not $DryRun) {
            $left = @(Get-ChildItem -LiteralPath $VolumesRoot -Force -ErrorAction SilentlyContinue)
            if ($left.Count -eq 0) { try { [IO.Directory]::Delete($VolumesRoot); Ok 'empty C:\Volumes removed' } catch { } }
            else { Skip 'C:\Volumes still has other content - kept' }
        }
    } else { Skip 'links left in place' }
} else { Skip 'no C:\Volumes folder' }
if ($NasHost) {
    if (AskYn ('   Also remove the stored Windows login for ' + $NasHost + '? (skip if other programs use it)')) {
        [void](Do ('cmdkey /delete:' + $NasHost) { & cmdkey.exe ('/delete:' + $NasHost) | Out-Null })
        Ok 'stored NAS login removed'
    } else { Skip 'NAS login kept' }
}

# ---- 7. search index (WSL) -----------------------------------------------------------------------
Say ''
Say '7. Search index data (Typesense inside WSL)'
$wsl = Get-WslNames
if ($wsl -contains $Distro) {
    if ($Distro -ieq 'ClipStage') {
        Say '   The dedicated Linux "ClipStage" holds the index. It can be rebuilt from the NAS by a full scan'
        Say '   (hours for a big NAS) or restored from a snapshot.'
        if (AskWord '   Type DELETE to remove it, anything else keeps it:' 'DELETE') {
            [void](Do 'wsl --unregister ClipStage' { & wsl.exe --unregister $Distro | Out-Null })
            if (-not $DryRun) {
                if ((Get-WslNames) -contains $Distro) { Warn 'the WSL distro is still listed' } else { Ok 'WSL distro ClipStage deleted' }
                if (Test-Path -LiteralPath $WslHome) { Remove-TreeSafe $WslHome }
            }
        } else { Skip 'index data kept' }
    } else {
        Say ('   ClipStage used your own WSL distro "' + $Distro + '". The distro itself is NOT deleted.')
        if (AskWord '   Type DELETE to remove only the Typesense files inside it:' 'DELETE') {
            [void](Do 'remove Typesense files inside the distro' {
                & wsl.exe -d $Distro -u root --exec bash -c 'rm -rf /opt/typesense /var/lib/typesense /var/log/typesense /etc/typesense'
            })
            Ok 'Typesense files removed from the distro'
        } else { Skip 'index data kept' }
    }
} else { Skip ('no WSL distro "' + $Distro + '" found') }

# ---- 8. tools ------------------------------------------------------------------------------------
Say ''
Say ('8. Tools folder  (' + $ToolsDir + ')')
if (Test-Path -LiteralPath $ToolsDir) {
    Say '   ffmpeg copy, Typesense download and the Linux image used for setup.'
    if (AskYn '   Delete it?') {
        if ($DryRun) { Say ('  (dry-run) delete ' + $ToolsDir) } else { Remove-TreeSafe $ToolsDir; if (-not (Test-Path -LiteralPath $ToolsDir)) { Ok 'tools folder removed' } }
    } else { Skip 'tools folder kept' }
} else { Skip 'no tools folder' }

# ---- 9. the project folder -----------------------------------------------------------------------
Say ''
Say ('9. ClipStage folder  (' + $Dir + ')')
Say '   Contains clipstage.db (notes, details, audit), .env (keys, passwords), thumbnails, local backups and .venv.'
if (-not $Bundle) { Warn 'you did not make a final backup bundle in this run' }
if (Test-Path -LiteralPath $Dir) {
    if (AskWord '   Type DELETE-ALL to delete this whole folder, anything else keeps it:' 'DELETE-ALL') {
        if ($DryRun) { Say ('  (dry-run) delete ' + $Dir + ' and ' + $InstallLog) }
        else {
            Remove-TreeSafe $Dir
            if (Test-Path -LiteralPath $InstallLog) { Remove-Item -LiteralPath $InstallLog -Force -ErrorAction SilentlyContinue }
            if (-not (Test-Path -LiteralPath $Dir)) { Ok 'ClipStage folder deleted' }
        }
    } else { Skip 'folder kept' }
} else { Skip 'no ClipStage folder' }

# ---- check what is left --------------------------------------------------------------------------
Say ''
Say 'Check - what is still on this PC:'
$left = @()
foreach ($t in $Tasks) { if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) { $left += ('task ' + $t) } }
foreach ($s in @($StagingShare, $KitShare)) { $x = Get-SmbShare -Name $s -ErrorAction SilentlyContinue; if ($x -and $x.Path -match 'ClipStage') { $left += ('share ' + $s) } }
if (Get-LocalUser -Name $EditorUser -ErrorAction SilentlyContinue) { $left += ('account ' + $EditorUser) }
if (Get-NetFirewallRule -DisplayName $FwRule -ErrorAction SilentlyContinue) { $left += 'firewall rule' }
if ((Get-WslNames) -contains 'ClipStage') { $left += 'WSL distro ClipStage' }
foreach ($p in @($StagingDir, $ToolsDir, $WslHome, $Dir, $VolumesRoot)) { if (Test-Path -LiteralPath $p) { $left += $p } }
if ($left.Count -eq 0) { Ok 'nothing from ClipStage is left' }
else { foreach ($l in $left) { Skip ('still present: ' + $l + '  (kept because you chose so, or in -DryRun)') } }

Say ''
Say 'Not touched: NAS volumes and files, Supabase, Python / ffmpeg installed by winget, the Windows WSL feature,'
Say 'the "allow symbolic links" setting, and the editors'' PCs (they have their own ClipStage app, links and logins).'
if ($Bundle) { Say ('Final backup bundle: ' + $Bundle) }
Say ''
Read-Host 'Press Enter to close'
