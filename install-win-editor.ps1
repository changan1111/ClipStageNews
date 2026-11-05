# =====================================================================================================
#  ClipStage - ONE-COMMAND INSTALLER for an EDITOR PC (Windows 10/11)
#
#  The middle machine's installer prints the exact line to paste. It looks like this:
#
#    cmdkey /add:<middle-machine-ip> /user:<NAME>\clipstage-editor /pass:<password>; `
#      powershell -ExecutionPolicy Bypass -File \\<middle-machine-ip>\clipstage-setup\install-win-editor.ps1
#
#  What it does on this PC:
#    1. lets Windows follow the staged links that live on the middle machine's share
#    2. stores the logins (Windows Credential Manager) and connects C:\Volumes\<share> to the NAS shares,
#       the same names as on the middle machine - staged clips are links to those paths
#    3. installs the "ClipStage" desktop app (drag staged clips into Premiere / Resolve / Avid) with a
#       desktop shortcut. Skip with -NoApp and use the browser instead.
#
#  Options:  -NoApp   skip the desktop app        -Reconfigure   ask for the NAS login again
# =====================================================================================================
[CmdletBinding()]
param([switch]$NoApp, [switch]$Reconfigure)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
if (-not (Test-Admin)) {
    # Copy the kit to a local folder first: the elevated window cannot see a network connection made
    # by the normal window, so it must not depend on \\server\... paths.
    $local = Join-Path $env:TEMP 'ClipStage-EditorKit'
    if (Test-Path -LiteralPath $local) { Remove-Item -LiteralPath $local -Recurse -Force }
    New-Item -ItemType Directory -Path $local -Force | Out-Null
    Copy-Item -Path (Join-Path $PSScriptRoot '*') -Destination $local -Recurse -Force
    Write-Host 'Asking for administrator rights...' -ForegroundColor Yellow
    $a = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-NoExit', '-File', ('"' + (Join-Path $local 'install-win-editor.ps1') + '"'))
    if ($NoApp) { $a += '-NoApp' }
    if ($Reconfigure) { $a += '-Reconfigure' }
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $a
    return
}

$AppDir     = 'C:\ClipStage-Editor'
$VolumesRoot = 'C:\Volumes'
$script:Warnings = New-Object System.Collections.Generic.List[string]
function Step([string]$t) { Write-Host ''; Write-Host $t -ForegroundColor Cyan }
function Ok([string]$t)   { Write-Host '  [ok] ' -ForegroundColor Green -NoNewline; Write-Host $t }
function Info([string]$t) { Write-Host "       $t" }
function Warn([string]$t) { Write-Host '  [!]  ' -ForegroundColor Yellow -NoNewline; Write-Host $t; $script:Warnings.Add($t) }
function Die([string]$t)  { throw $t }
function Ask([string]$q, [string]$def = '') {
    $suffix = ''; if ($def) { $suffix = " [$def]" }
    $a = Read-Host "  $q$suffix"
    if ([string]::IsNullOrWhiteSpace($a)) { return $def }
    return $a.Trim()
}
function AskSecret([string]$q) {
    $s = Read-Host "  $q" -AsSecureString
    $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
}
function Invoke-Quiet([string]$Exe, [string[]]$Arguments) {
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try {
        $out = & $Exe @Arguments 2>&1
        $code = $LASTEXITCODE
        return [pscustomobject]@{ Code = $code; Output = (($out | Out-String).Trim()) }
    } finally { $ErrorActionPreference = $old }
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

Start-Transcript -Path (Join-Path $env:TEMP 'clipstage-editor-install.log') -Append | Out-Null
try {
    Write-Host ''
    Write-Host '  ClipStage - editor PC installer (Windows)' -ForegroundColor White

    # ---- configuration from the middle machine ----
    $cfgFile = Join-Path $PSScriptRoot 'editor-config.json'
    if (-not (Test-Path -LiteralPath $cfgFile)) { Die "editor-config.json is missing next to this script. Run the one-line command printed by the middle machine's installer." }
    $cfg = Get-Content -LiteralPath $cfgFile -Raw | ConvertFrom-Json
    $hostIp = [string]$cfg.host
    $share = [string]$cfg.shareName
    Write-Host "  Middle machine: $hostIp  (http://${hostIp}:8000)"

    # ---- what will happen ----
    $haveNode = [bool](Get-Command node.exe -ErrorAction SilentlyContinue) -or (Test-Path -LiteralPath "$env:ProgramFiles\nodejs\node.exe")
    Write-Host ''
    Write-Host '  What this installer will do' -ForegroundColor White
    Write-Host ''
    Write-Host '    Allow Windows to follow the staged links (a Windows network-security setting is changed)'
    Write-Host '    Save the staging-share login and the NAS login in Windows Credential Manager'
    Write-Host '    Create C:\Volumes\<share> links to the NAS shares (same names as on the middle machine)'
    if (-not $NoApp -and (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'clipstage-fcp-drag'))) {
        if ($haveNode) { Write-Host '    ClipStage desktop app: Node.js already installed' }
        else { Write-Host '    ClipStage desktop app: Node.js WILL BE INSTALLED (winget, or nodejs.org) - use -NoApp to skip the app' }
    }
    Write-Host ''
    $go = Read-Host '  Continue? [Y/n]'
    if ($go -and $go.Trim().ToLower().StartsWith('n')) { Write-Host '  Nothing was changed.'; return }

    # ---- 1. symbolic links on remote shares ----
    Step '[1/4] Allowing Windows to follow staged links'
    $r = Invoke-Quiet 'fsutil.exe' @('behavior', 'set', 'SymlinkEvaluation', 'L2L:1', 'L2R:1', 'R2L:1', 'R2R:1')
    if ($r.Code -eq 0) { Ok 'link evaluation enabled (local + remote)' } else { Warn "Could not change the link policy: $($r.Output)" }

    # ---- 2. staging share ----
    Step '[2/4] Connecting to the staging share'
    $user = "$($cfg.hostName)\$($cfg.shareUser)"
    [void](Invoke-Quiet 'cmdkey.exe' @("/add:$hostIp", "/user:$user", "/pass:$($cfg.sharePassword)"))
    if (Test-Reachable "\\$hostIp\$share") { Ok "\\$hostIp\$share is reachable (login saved in Credential Manager)" }
    else { Warn "Cannot open \\$hostIp\$share - is the middle machine on, and are you on the same network?" }

    # ---- 3. NAS volumes ----
    Step '[3/4] NAS volumes (C:\Volumes\<share>)'
    $nas = [string]$cfg.nasHost
    $nasUser = [string]$cfg.nasUser; $nasPass = [string]$cfg.nasPassword; $nasDomain = [string]$cfg.nasDomain
    $askNas = { 
        Write-Host '  Enter the login for the NAS (your own, or the shared one). Leave the username empty to skip'
        Write-Host '  (only if this PC already has access to the NAS shares).'
        $script:nasDomain = Ask 'NAS domain (empty if none)' $script:nasDomain
        $script:nasUser = Ask 'NAS username' $script:nasUser
        if ($script:nasUser) {
            $p = AskSecret 'NAS password (hidden; Enter keeps the saved one)'
            if ($p) { $script:nasPass = $p }
        } else { $script:nasPass = '' }
    }
    if ($nas -and ($Reconfigure -or -not $nasUser)) { & $askNas }
    if ($nas) {
        $tries = 0
        while ($true) {
            $reach = 'NOT reachable on port 445 - check the address'
            if (Test-TcpPort $nas 445) { $reach = 'reachable' }
            $who = $nasUser; if ($nasDomain) { $who = "$nasDomain\$nasUser" }
            if (-not $who) { $who = '(none)' }
            $pw = '(none)'; if ($nasPass) { $pw = '******** (saved)' }
            Write-Host ''
            Write-Host '  Please check your entries' -ForegroundColor White
            Write-Host ''
            Write-Host "    Middle machine     $hostIp"
            Write-Host "    NAS                $nas   [$reach]"
            Write-Host "    NAS login          $who   password: $pw"
            Write-Host "    Volumes            $(@($cfg.volumes) -join ', ')"
            Write-Host ''
            $a = Ask 'Is this correct?  C = confirm and continue,  E = edit (asks every entry again)' 'C'
            if ($a.ToUpper().StartsWith('E')) { & $askNas } else { break }
            if (++$tries -gt 20) { Die 'Too many edits - is a terminal attached?' }
        }
    }
    if ($nas -and $nasUser) {
        $full = $nasUser; if ($nasDomain) { $full = "$nasDomain\$nasUser" }
        [void](Invoke-Quiet 'cmdkey.exe' @("/add:$nas", "/user:$full", "/pass:$nasPass"))
        Ok "NAS login stored for $nas"
    }
    if ($nas) {
        if (-not (Test-Path -LiteralPath $VolumesRoot)) { New-Item -ItemType Directory -Path $VolumesRoot -Force | Out-Null }
        $reach = Test-TcpPort $nas 445
        foreach ($vol in @($cfg.volumes)) {
            $link = Join-Path $VolumesRoot $vol
            $unc = "\\$nas\$vol"
            $item = Get-Item -LiteralPath $link -Force -ErrorAction SilentlyContinue
            $isLink = $item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)
            if ($item -and -not $isLink) { Warn "$link is a normal folder, not a link to the NAS - leaving it alone."; continue }
            if ($isLink -and ([string]$item.Target) -ne $unc) { cmd.exe /c rmdir "$link" | Out-Null; $isLink = $false }
            if (-not $isLink) { cmd.exe /c mklink /D "$link" "$unc" | Out-Null }
            if ($reach -and (Test-Reachable "$link\")) { Ok "$vol -> $unc" } else { Warn "$vol -> $unc is not reachable yet (NAS login / share name?)" }
        }
        [void](Invoke-Quiet 'icacls.exe' @($VolumesRoot, '/inheritance:r', '/grant:r', '*S-1-5-32-544:(OI)(CI)F', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-545:(OI)(CI)RX'))
    } else {
        Info 'No NAS address in the kit - make sure the NAS shares are available as C:\Volumes\<share> on this PC.'
    }

    # ---- 4. desktop app ----
    Step '[4/4] ClipStage desktop app'
    $appSrc = Join-Path $PSScriptRoot 'clipstage-fcp-drag'
    if ($NoApp -or -not (Test-Path -LiteralPath $appSrc)) {
        Info ('Skipped - open http://' + $hostIp + ':8000 in your browser instead.')
    } else {
        function Find-NodeDir {
            foreach ($d in @("$env:ProgramFiles\nodejs", "${env:ProgramFiles(x86)}\nodejs", (Join-Path $AppDir 'node'))) {
                if (Test-Path -LiteralPath (Join-Path $d 'node.exe')) { return $d }
            }
            $c = Get-Command node.exe -ErrorAction SilentlyContinue
            if ($c) { return (Split-Path $c.Source) }
            return $null
        }
        $nodeDir = Find-NodeDir
        if (-not $nodeDir -and (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
            Info 'installing Node.js (winget)...'
            [void](Invoke-Quiet 'winget.exe' @('install', '-e', '--id', 'OpenJS.NodeJS.LTS', '--silent', '--accept-package-agreements', '--accept-source-agreements'))
            $nodeDir = Find-NodeDir
        }
        if (-not $nodeDir) {
            Info 'downloading Node.js from nodejs.org...'
            $sums = (Invoke-WebRequest -Uri 'https://nodejs.org/dist/latest-v22.x/SHASUMS256.txt' -UseBasicParsing).Content
            if ($sums -is [byte[]]) { $sums = [Text.Encoding]::UTF8.GetString($sums) }
            $m = [regex]::Match([string]$sums, 'node-v22\.[0-9.]+-win-x64\.zip')
            if (-not $m.Success) { Die 'Could not find a Node.js download.' }
            $zip = Join-Path $env:TEMP 'node.zip'
            Invoke-WebRequest -Uri ('https://nodejs.org/dist/latest-v22.x/' + $m.Value) -OutFile $zip -UseBasicParsing
            $tmp = Join-Path $env:TEMP 'node-extract'
            if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force }
            Expand-Archive -LiteralPath $zip -DestinationPath $tmp -Force
            New-Item -ItemType Directory -Path (Join-Path $AppDir 'node') -Force | Out-Null
            Copy-Item -Path (Join-Path (Get-ChildItem -LiteralPath $tmp -Directory | Select-Object -First 1).FullName '*') -Destination (Join-Path $AppDir 'node') -Recurse -Force
            Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
            $nodeDir = Find-NodeDir
        }
        if (-not $nodeDir) { Die ('Node.js could not be installed. Use the browser (http://' + $hostIp + ':8000) or run this installer again with internet access.') }
        Ok "Node.js: $nodeDir"
        $env:Path = "$nodeDir;$env:Path"

        $app = Join-Path $AppDir 'app'
        if (Test-Path -LiteralPath $app) { Remove-Item -LiteralPath $app -Recurse -Force }
        New-Item -ItemType Directory -Path $app -Force | Out-Null
        Copy-Item -Path (Join-Path $appSrc '*') -Destination $app -Recurse -Force
        $appCfg = '{ "serverUrl": "http://' + $hostIp + ':8000", "stagingMount": "", "shareName": "' + $share + '" }'
        [IO.File]::WriteAllText((Join-Path $app 'config.json'), $appCfg, (New-Object System.Text.UTF8Encoding($false)))

        Info 'downloading the app components (Electron, ~1-2 minutes)...'
        Push-Location $app
        try { $n = Invoke-Quiet (Join-Path $nodeDir 'npm.cmd') @('install', '--no-audit', '--no-fund') } finally { Pop-Location }
        $electron = Join-Path $app 'node_modules\electron\dist\electron.exe'
        if (-not (Test-Path -LiteralPath $electron)) { Die "npm install did not produce Electron: $($n.Output)" }

        $ws = New-Object -ComObject WScript.Shell
        foreach ($dir in @([Environment]::GetFolderPath('CommonDesktopDirectory'), [Environment]::GetFolderPath('CommonPrograms'))) {
            $lnk = $ws.CreateShortcut((Join-Path $dir 'ClipStage.lnk'))
            $lnk.TargetPath = $electron
            $lnk.Arguments = '"' + $app + '"'
            $lnk.WorkingDirectory = $app
            $lnk.IconLocation = "$electron,0"
            $lnk.Description = 'ClipStage - find and stage newsroom footage'
            $lnk.Save()
        }
        [void](Invoke-Quiet 'icacls.exe' @($AppDir, '/inheritance:r', '/grant:r', '*S-1-5-32-544:(OI)(CI)F', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-545:(OI)(CI)RX'))
        Ok 'installed - "ClipStage" shortcut is on the desktop and in the Start menu'
    }

    # ---- final check ----
    Write-Host ''
    try { [void](Invoke-RestMethod -Uri "http://${hostIp}:8000/health" -TimeoutSec 5); Ok "ClipStage server answers at http://${hostIp}:8000" }
    catch { Warn "The ClipStage server at http://${hostIp}:8000 does not answer yet." }
    Write-Host ''
    Write-Host '  Done. Open ClipStage from the desktop shortcut, sign in, pick your name, stage clips,' -ForegroundColor Green
    Write-Host '  then drag them from the right-hand panel into your editing software (leave files in place).' -ForegroundColor Green
    if ($script:Warnings.Count -gt 0) {
        Write-Host ''
        Write-Host '  Things to look at:' -ForegroundColor Yellow
        foreach ($w in $script:Warnings) { Write-Host "   - $w" -ForegroundColor Yellow }
    }
    Write-Host ''
} catch {
    Write-Host ''
    Write-Host "INSTALLATION STOPPED: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "  Log: $(Join-Path $env:TEMP 'clipstage-editor-install.log')" -ForegroundColor Red
} finally {
    try { Stop-Transcript | Out-Null } catch { }
}
