# =====================================================================================================
#  ClipStage - ONE-COMMAND INSTALLER for the MIDDLE MACHINE (Windows 10/11, Windows Server 2022+)
#
#      powershell -ExecutionPolicy Bypass -File .\install-win-host.ps1
#
#  (Run it from any PowerShell window - it asks for administrator rights by itself.)
#
#  Installs and configures everything: Python, ffmpeg, the search engine (SQLite FTS5 built into Python - nothing
#  extra; or, only with SEARCH_BACKEND=typesense, Typesense inside a small dedicated WSL2 distro,
#  because Typesense has no native Windows build), the .env file, NAS access, the staging share, the
#  firewall rules, the background tasks (API + Typesense at logon, nightly index + backup, NAS watchdog)
#  and an "editor kit" for the editors' PCs.  Then it checks that it all works.
#
#  Safe to run again at any time (repairs / upgrades; keeps your .env, notes and index).
#  If Windows has to restart once to switch on WSL2, the installer continues by itself after sign-in.
#
#  Options:  -Reconfigure    ask every question again
#            -NoIndex        do not start the first index at the end
#            -Yes            never ask; use the existing .env / defaults
#            -WslDistro X    use an existing WSL distro (Ubuntu/Debian) instead of creating "ClipStage"
#            -EditorKitOnly  only rebuild the editor kit (add -IncludeNas to put the NAS login into it)
# =====================================================================================================
[CmdletBinding()]
param(
    [switch]$Reconfigure,
    [switch]$NoIndex,
    [switch]$Yes,
    [switch]$Resume,
    [switch]$EditorKitOnly,
    [switch]$IncludeNas,
    [string]$WslDistro = ''
)
$UserDistro = $WslDistro          # clipstage_env.ps1 also defines a variable of that name, so keep our own copy
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }

# ---------------------------------------------------------------------------------------- administrator
function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
if (-not (Test-Admin)) {
    Write-Host 'Asking for administrator rights...' -ForegroundColor Yellow
    $a = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-NoExit', '-File', ('"' + $PSCommandPath + '"'))
    if ($Reconfigure) { $a += '-Reconfigure' }
    if ($NoIndex) { $a += '-NoIndex' }
    if ($Yes) { $a += '-Yes' }
    if ($EditorKitOnly) { $a += '-EditorKitOnly' }
    if ($IncludeNas) { $a += '-IncludeNas' }
    if ($WslDistro) { $a += @('-WslDistro', $WslDistro) }
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $a
    return
}

# ---------------------------------------------------------------------------------------- constants
$Dest         = 'C:\ClipStage'
$ToolsDir     = 'C:\ClipStage-tools'
$StagingDir   = 'C:\ClipStage-Staging'
$WslHome      = 'C:\ClipStage-wsl'
$VolumesRoot  = 'C:\Volumes'
$StagingShare = 'staging'
$KitShare     = 'clipstage-setup'
$EditorUser   = 'clipstage-editor'
$ResumeTask   = 'ClipStage-Install-Resume'
$TypesenseVersions = @('30.2', '29.1', '27.1')
if ($env:TYPESENSE_VERSION) { $TypesenseVersions = @($env:TYPESENSE_VERSION) }
$Total = 12
$script:StepNo = 0
$script:Warnings = New-Object System.Collections.Generic.List[string]

# ---------------------------------------------------------------------------------------- small UI helpers
function Step([string]$t) { $script:StepNo++; Write-Host ''; Write-Host ("[{0}/{1}] {2}" -f $script:StepNo, $Total, $t) -ForegroundColor Cyan }
function Ok([string]$t)   { Write-Host '  [ok] ' -ForegroundColor Green -NoNewline; Write-Host $t }
function Info([string]$t) { Write-Host "       $t" }
function Warn([string]$t) { Write-Host '  [!]  ' -ForegroundColor Yellow -NoNewline; Write-Host $t; $script:Warnings.Add($t) }
function Die([string]$t)  { throw $t }

function Ask([string]$q, [string]$def = '') {
    if ($Yes) { return $def }
    $suffix = ''
    if ($def) { $suffix = " [$def]" }
    $a = Read-Host "  $q$suffix"
    if ([string]::IsNullOrWhiteSpace($a)) { return $def }
    return $a.Trim()
}
function AskSecret([string]$q) {
    if ($Yes) { return '' }
    $s = Read-Host "  $q" -AsSecureString
    $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
}
function YesNo([string]$q, [bool]$defYes = $true) {
    if ($Yes) { return $defYes }
    $hint = 'Y/n'; if (-not $defYes) { $hint = 'y/N' }
    $a = Read-Host "  $q [$hint]"
    if ([string]::IsNullOrWhiteSpace($a)) { return $defYes }
    return ($a.Trim().ToLower().StartsWith('y'))
}

function Invoke-Quiet([string]$Exe, [string[]]$Arguments) {
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try {
        $out = & $Exe @Arguments 2>&1
        $code = $LASTEXITCODE
        return [pscustomobject]@{ Code = $code; Output = (($out | Out-String).Trim()) }
    } finally { $ErrorActionPreference = $old }
}

function Get-RandomInt([int]$Max) {
    $b = New-Object byte[] 4
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($b) } finally { $rng.Dispose() }
    return [int]([BitConverter]::ToUInt32($b, 0) % [uint32]$Max)
}
function New-HexKey([int]$Bytes = 24) {
    $b = New-Object byte[] $Bytes
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($b) } finally { $rng.Dispose() }
    return (($b | ForEach-Object { $_.ToString('x2') }) -join '')
}
function New-StrongPassword([int]$Length = 20) {
    $sets = @('ABCDEFGHJKLMNPQRSTUVWXYZ', 'abcdefghijkmnopqrstuvwxyz', '23456789', '-_.')
    $all = ($sets -join '')
    $chars = New-Object System.Collections.Generic.List[char]
    foreach ($s in $sets) { $chars.Add($s[(Get-RandomInt $s.Length)]) }
    while ($chars.Count -lt $Length) { $chars.Add($all[(Get-RandomInt $all.Length)]) }
    for ($i = $chars.Count - 1; $i -gt 0; $i--) {
        $j = Get-RandomInt ($i + 1)
        $t = $chars[$i]; $chars[$i] = $chars[$j]; $chars[$j] = $t
    }
    return (-join $chars)
}

# Locale-independent names for built-in accounts (Administrators, SYSTEM, Users)
function Get-AccountName([string]$Sid) {
    return (New-Object Security.Principal.SecurityIdentifier $Sid).Translate([Security.Principal.NTAccount]).Value
}
# Lock a folder to administrators + SYSTEM (everyone else only if listed). Prevents local users from
# editing scripts / programs that run elevated at logon.
function Protect-Folder([string]$Path, [string[]]$ReadSids = @(), [string[]]$ReadNames = @()) {
    if (-not (Test-Path -LiteralPath $Path)) { New-Item -ItemType Directory -Path $Path -Force | Out-Null }
    $grants = @('*S-1-5-32-544:(OI)(CI)F', '*S-1-5-18:(OI)(CI)F')
    foreach ($s in $ReadSids) { $grants += "*${s}:(OI)(CI)RX" }
    foreach ($n in $ReadNames) { $grants += "${n}:(OI)(CI)RX" }
    $r = Invoke-Quiet 'icacls.exe' (@($Path, '/inheritance:r', '/grant:r') + $grants)
    if ($r.Code -ne 0) { Warn "Could not restrict permissions on $Path : $($r.Output)" }
}

# ---------------------------------------------------------------------------------------- transcript
Start-Transcript -Path 'C:\ClipStage-install.log' -Append | Out-Null
function Show-Fatal($err) {
    Write-Host ''
    Write-Host "INSTALLATION STOPPED: $($err.Exception.Message)" -ForegroundColor Red
    Write-Host '  Fix the problem above and run the same command again - it continues where it stopped.' -ForegroundColor Red
    Write-Host '  Full log: C:\ClipStage-install.log' -ForegroundColor Red
}

# =======================================================================================================
# 1. project files
# =======================================================================================================
# SQLite search is the default on Windows (nothing to install). Typesense inside WSL2 is only used when
# SEARCH_BACKEND=typesense is set (environment variable or C:\ClipStage\.env).
$script:SearchChoice = ''          # 'sqlite' or 'typesense' once chosen (question below, env var, or saved .env)
function Get-SavedSearchBackend {
    $f = Join-Path $Dest '.env'
    if (Test-Path -LiteralPath $f) {
        $m = Select-String -LiteralPath $f -Pattern '^SEARCH_BACKEND=\s*(sqlite|typesense)\s*$' | Select-Object -First 1
        if ($m) { return $m.Matches[0].Groups[1].Value.ToLower() }
    }
    return ''
}
# Asked once at the start (before the plan is shown, because Typesense needs WSL2 and SQLite needs nothing).
# Not asked when SEARCH_BACKEND is already set in the environment or saved in C:\ClipStage\.env,
# unless -Reconfigure is used. -Yes takes the default: sqlite.
function Step-ChooseSearch {
    $envVal = "$env:SEARCH_BACKEND".Trim().ToLower()
    $saved = Get-SavedSearchBackend
    $cur = $saved; if (-not $cur) { $cur = $envVal }
    if (($cur -eq 'sqlite' -or $cur -eq 'typesense') -and -not $Reconfigure) { $script:SearchChoice = $cur; return }
    if (-not $cur) { $cur = 'sqlite' }
    Write-Host ''
    Write-Host '  Search engine: sqlite is the default on Windows (built into Python, nothing to install).'
    Write-Host '  typesense needs WSL2 (a small Linux inside Windows) and a Typesense API key.'
    $tries = 0
    while ($true) {
        $a = (Ask 'Search engine (sqlite or typesense)' $cur).ToLower()
        if ($a -eq 'sqlite' -or $a -eq 'typesense') { $script:SearchChoice = $a; return }
        if ($Yes) { Die 'SEARCH_BACKEND must be sqlite or typesense.' }
        if (++$tries -gt 15) { Die 'Too many invalid entries - is a terminal attached?' }
        Write-Host '  Type sqlite or typesense.'
    }
}
function Want-Typesense {
    if ($script:SearchChoice) { return ($script:SearchChoice -eq 'typesense') }
    if ("$env:SEARCH_BACKEND".Trim().ToLower() -eq 'typesense') { return $true }
    $f = Join-Path $Dest '.env'
    if (Test-Path -LiteralPath $f) { return [bool](Select-String -LiteralPath $f -Pattern '^SEARCH_BACKEND=typesense\s*$' -Quiet) }
    return $false
}

function Step-CopyProject {
    Step 'Copying ClipStage to C:\ClipStage'
    $src = (Resolve-Path -LiteralPath $PSScriptRoot).Path.TrimEnd('\')
    if ($src -ieq $Dest) { Ok "already running from $Dest"; return }
    New-Item -ItemType Directory -Path $Dest -Force | Out-Null
    $hadEditors = Test-Path -LiteralPath (Join-Path $Dest 'editors.json')
    $xf = @('.env', 'clipstage.db', 'clipstage.db-*', 'editors.json', 'index_status.json', 'index_dircache.json', '*.log')
    $xd = @('.git', '.venv', 'node_modules', 'thumbs', 'backups', 'editor-kit')
    & robocopy.exe $src $Dest /E /XF $xf /XD $xd /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { Die "Could not copy the project to $Dest (robocopy exit $LASTEXITCODE)." }
    if (-not $hadEditors -and (Test-Path -LiteralPath (Join-Path $src 'editors.json'))) {
        Copy-Item -LiteralPath (Join-Path $src 'editors.json') -Destination $Dest
    }
    Get-ChildItem -LiteralPath $Dest -Recurse -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue
    Ok "project files are in $Dest"
}

# =======================================================================================================
# 2. Python
# =======================================================================================================
function Test-PythonOk([string]$Exe) {
    try {
        & $Exe -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}
function Find-Python {
    $c = New-Object System.Collections.Generic.List[string]
    foreach ($v in '313', '312', '311', '310') {
        $c.Add("$env:ProgramFiles\Python$v\python.exe")
        $c.Add("$env:LOCALAPPDATA\Programs\Python\Python$v\python.exe")
    }
    if (Get-Command py.exe -ErrorAction SilentlyContinue) {
        try { $p = (& py.exe -3 -c 'import sys;print(sys.executable)' 2>$null | Select-Object -First 1); if ($p) { $c.Add(([string]$p).Trim()) } } catch { }
    }
    foreach ($x in @(Get-Command python.exe -All -ErrorAction SilentlyContinue)) {
        if ($x.Source -notmatch 'WindowsApps') { $c.Add($x.Source) }
    }
    foreach ($p in $c) { if ((Test-Path -LiteralPath $p) -and (Test-PythonOk $p)) { return $p } }
    return $null
}
function Install-Python {
    if (Get-Command winget.exe -ErrorAction SilentlyContinue) {
        Info 'installing Python 3.12 with winget...'
        [void](Invoke-Quiet 'winget.exe' @('install', '-e', '--id', 'Python.Python.3.12', '--scope', 'machine', '--silent', '--accept-package-agreements', '--accept-source-agreements'))
        $p = Find-Python; if ($p) { return $p }
    }
    Info 'downloading the Python 3.12 installer from python.org...'
    $f = Join-Path $env:TEMP 'python-3.12-installer.exe'
    Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe' -OutFile $f -UseBasicParsing
    $proc = Start-Process -FilePath $f -ArgumentList '/quiet', 'InstallAllUsers=1', 'PrependPath=1', 'Include_test=0' -Wait -PassThru
    Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue
    if ($proc.ExitCode -ne 0) { Warn "The Python installer returned exit code $($proc.ExitCode)." }
    return (Find-Python)
}
function Step-Python {
    Step 'Python and the ClipStage environment'
    $base = Find-Python
    if (-not $base) { $base = Install-Python }
    if (-not $base) { Die 'Python 3.10+ could not be installed. Install it from https://www.python.org/downloads/ and run this installer again.' }
    Ok "Python: $base"
    $venvPy = Join-Path $Dest '.venv\Scripts\python.exe'
    if (-not ((Test-Path -LiteralPath $venvPy) -and (Test-PythonOk $venvPy))) {
        if (Test-Path -LiteralPath (Join-Path $Dest '.venv')) { Remove-Item -LiteralPath (Join-Path $Dest '.venv') -Recurse -Force }
        & $base -m venv (Join-Path $Dest '.venv')
        if ($LASTEXITCODE -ne 0) { Die 'Could not create the Python virtual environment.' }
    }
    $script:Python = $venvPy
    [void](Invoke-Quiet $venvPy @('-m', 'pip', 'install', '-q', '--upgrade', 'pip'))
    $r = Invoke-Quiet $venvPy @('-m', 'pip', 'install', '-q', '-r', (Join-Path $Dest 'requirements.txt'))
    if ($r.Code -ne 0) { Die "pip install failed (no internet?): $($r.Output)" }
    $r = Invoke-Quiet $venvPy @('-c', 'import fastapi, typesense, requests, uvicorn; print(1)')
    if ($r.Code -ne 0) { Die "Python packages did not import: $($r.Output)" }
    Ok 'fastapi, typesense, requests, uvicorn are installed'
}

# =======================================================================================================
# 3. questions -> .env
# =======================================================================================================
function Test-Placeholder([string]$v) {
    return ([string]::IsNullOrWhiteSpace($v) -or $v.StartsWith('CHANGE_ME') -or $v -like '*your-project*' -or $v -like 'your-supabase*' -or $v -eq '10.1.10.XXX' -or $v.StartsWith('<'))
}
function Get-LanIp {
    try {
        $cfg = Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq 'Up' } | Select-Object -First 1
        if ($cfg) { return ($cfg.IPv4Address | Select-Object -First 1).IPAddress }
    } catch { }
    return ''
}
function Read-Answers([hashtable]$S, [bool]$All) {
    $tries = 0
    if ($S.Engine -eq 'typesense' -and ($All -or $S.TsNew)) {
        Write-Host '  Typesense API key: a random one was generated for you. Press Enter to use it, or paste your own.'
        while ($true) {
            $p = AskSecret 'Typesense API key (hidden; Enter = use the generated key)'
            if ($p) { $S.TsKey = $p }
            if ($S.TsKey.Length -ge 16) { break }
            if (++$tries -gt 15) { Die 'Too many invalid entries - is a terminal attached?' }
            Write-Host '  The key must be at least 16 characters.'
        }
        $S.TsNew = $false
        $tries = 0
    }
    if ($All -or (Test-Placeholder $S.SbUrl) -or (Test-Placeholder $S.SbKey)) {
        Write-Host '  Supabase holds the logins. Project Settings -> API in the Supabase dashboard has both values.'
        while ($true) {
            $d = ''; if (-not (Test-Placeholder $S.SbUrl)) { $d = $S.SbUrl }
            $S.SbUrl = (Ask 'Supabase Project URL (https://xxxx.supabase.co)' $d).TrimEnd('/')
            if ($S.SbUrl.StartsWith('https://')) { break }
            if ($Yes) { Die 'SUPABASE_URL is missing in .env.' }
            if (++$tries -gt 15) { Die 'Too many invalid entries - is a terminal attached?' }
            Write-Host '  It must start with https://'
        }
        $tries = 0
        while ($true) {
            $d = ''; if (-not (Test-Placeholder $S.SbKey)) { $d = $S.SbKey }
            $S.SbKey = Ask 'Supabase anon / publishable key (NOT the service-role key)' $d
            if ($S.SbKey.Length -gt 20) { break }
            if ($Yes) { Die 'SUPABASE_ANON_KEY is missing in .env.' }
            if (++$tries -gt 15) { Die 'Too many invalid entries - is a terminal attached?' }
            Write-Host '  That looks too short.'
        }
    }
    if ($All -or ((-not $S.NasHost) -and (-not $Yes))) {
        Write-Host '  NAS: ClipStage reads your footage from NAS shares. Give the NAS login once; Windows keeps it'
        Write-Host '  (Credential Manager) and the shares come back by themselves after reboots.'
        $S.NasHost = Ask 'NAS address (IP or name; empty if this PC already reaches the shares)' $S.NasHost
        if ($S.NasHost) {
            $S.NasDomain = Ask 'NAS domain (empty if none)' $S.NasDomain
            $S.NasUser = Ask 'NAS username' $S.NasUser
            $p = AskSecret 'NAS password (hidden; Enter keeps the saved one)'
            if ($p) { $S.NasPass = $p }
        } else { $S.NasDomain = ''; $S.NasUser = ''; $S.NasPass = '' }
    }
    if ($All -or (Test-Placeholder $S.Vols)) {
        $d = $S.Vols; if (Test-Placeholder $S.Vols) { $d = 'EDIT,EDIT2,INGEST,PLAYOUT,DIGITAL,SHARE FOLDER,TRANSCODER' }
        $S.Vols = Ask 'Volumes to index, comma separated, exactly as the NAS share names' $d
    }
    if ($All -or (Test-Placeholder $S.Ip)) {
        $d = $S.Ip; if (Test-Placeholder $S.Ip) { $d = Get-LanIp }
        $S.Ip = Ask "This PC's address as editors will see it" $d
    }
    if (Test-Placeholder $S.Ip) { $S.Ip = Get-LanIp }

    $edFile = Join-Path $Dest 'editors.json'
    if ((-not (Test-Path -LiteralPath $edFile)) -or $All) {
        $now = ''
        try { $now = ((Get-Content -LiteralPath $edFile -Raw | ConvertFrom-Json).editors) -join ',' } catch { }
        if (-not $now) { $now = 'EDITOR1,EDITOR2' }
        $names = (Ask 'Editor names for the staging dropdown (comma separated)' $now).Split(',') |
            ForEach-Object { $_.Trim() } | Where-Object { $_ -and $_ -notmatch '[\\/]' }
        $json = '{ "editors": [' + (($names | ForEach-Object { '"' + $_.Replace('\', '\\').Replace('"', '\"') + '"' }) -join ', ') + '] }'
        [IO.File]::WriteAllText($edFile, $json, (New-Object System.Text.UTF8Encoding($false)))
    }
}

function Show-Summary([hashtable]$S) {
    $sb = 'NOT reachable - check the URL/key or the internet'
    try {
        $r = Invoke-WebRequest -Uri "$($S.SbUrl)/auth/v1/health" -Headers @{ apikey = $S.SbKey } -UseBasicParsing -TimeoutSec 8
        if ($r.StatusCode -eq 200) { $sb = 'reachable' }
    } catch { }
    $edNames = ''
    try { $edNames = ((Get-Content -LiteralPath (Join-Path $Dest 'editors.json') -Raw | ConvertFrom-Json).editors) -join ', ' } catch { }
    Write-Host ''
    Write-Host '  Please check your entries' -ForegroundColor White
    Write-Host ''
    Write-Host ("    Search engine      {0}" -f $S.Engine)
    if ($S.Engine -eq 'typesense') { Write-Host ("    Typesense key      {0}... ({1} characters)" -f $S.TsKey.Substring(0, [Math]::Min(4, $S.TsKey.Length)), $S.TsKey.Length) }
    Write-Host ("    Supabase URL       {0}   [{1}]" -f $S.SbUrl, $sb)
    Write-Host ("    Supabase key       {0}... ({1} characters)" -f $S.SbKey.Substring(0, [Math]::Min(6, $S.SbKey.Length)), $S.SbKey.Length)
    if ($S.NasHost) {
        $nas = 'NOT reachable on port 445 - check the address'
        if (Test-TcpPort $S.NasHost 445) { $nas = 'reachable' }
        $who = $S.NasUser; if ($S.NasDomain) { $who = "$($S.NasDomain)\$($S.NasUser)" }
        $pw = '(none)'; if ($S.NasPass) { $pw = '******** (saved)' }
        Write-Host ("    NAS                {0}   [{1}]" -f $S.NasHost, $nas)
        Write-Host ("    NAS login          {0}   password: {1}" -f $who, $pw)
    } else {
        Write-Host '    NAS                (not set - C:\Volumes\<share> must already exist)'
    }
    Write-Host ("    Volumes            {0}" -f $S.Vols)
    Write-Host ("    This PC's address  {0}" -f $S.Ip)
    Write-Host ("    Editors            {0}" -f $edNames)
    Write-Host ''
}

function Step-Settings {
    Step 'Settings (.env)'
    $envFile = Join-Path $Dest '.env'
    if (-not (Test-Path -LiteralPath $envFile)) { Copy-Item -LiteralPath (Join-Path $Dest '.env.sample') -Destination $envFile }
    Import-ClipEnv
    $cur = { param($k) Get-ClipEnvFromFile $k $envFile }

    $useTs = [bool](Want-Typesense)
    $tsKey = & $cur 'TYPESENSE_KEY'; $tsNew = $false
    if ($useTs -and (Test-Placeholder $tsKey)) { $tsKey = New-HexKey 24; $tsNew = $true }
    $editorPass = & $cur 'CLIPSTAGE_EDITOR_SHARE_PASSWORD'
    if (Test-Placeholder $editorPass) { $editorPass = New-StrongPassword 20 }

    $S = @{
        SbUrl = (& $cur 'SUPABASE_URL'); SbKey = (& $cur 'SUPABASE_ANON_KEY')
        NasHost = (& $cur 'CLIPSTAGE_NAS_HOST'); NasDomain = (& $cur 'CLIPSTAGE_NAS_DOMAIN')
        NasUser = (& $cur 'CLIPSTAGE_NAS_USER'); NasPass = (& $cur 'CLIPSTAGE_NAS_PASSWORD')
        Vols = (& $cur 'CLIPSTAGE_SCAN_VOLUMES'); Ip = (& $cur 'CLIPSTAGE_SMB_HOST')
        Engine = $(if ($useTs) { 'typesense' } else { 'sqlite' }); TsKey = $tsKey; TsNew = $tsNew
    }
    Read-Answers $S ([bool]$Reconfigure)
    while ($true) {
        Show-Summary $S
        if ($Yes) { break }
        $a = Ask 'Is this correct?  C = confirm and continue,  E = edit (asks every entry again)' 'C'
        if ($a.ToUpper().StartsWith('E')) { Read-Answers $S $true } else { break }
    }

    if ($S.Engine -eq 'typesense') {
        Set-ClipEnv @{ SEARCH_BACKEND = 'typesense'; TYPESENSE_KEY = $S.TsKey; TYPESENSE_HOST = 'localhost'; TYPESENSE_PORT = '8108' }
    } else {
        Set-ClipEnv @{ SEARCH_BACKEND = 'sqlite' }
        Ok 'search engine: SQLite (built into Python)'
    }
    Set-ClipEnv @{
        SUPABASE_URL = $S.SbUrl; SUPABASE_ANON_KEY = $S.SbKey
        CLIPSTAGE_SCAN_VOLUMES = $S.Vols; CLIPSTAGE_SMB_HOST = $S.Ip
        CLIPSTAGE_BIND_HOST = '0.0.0.0'; CLIPSTAGE_COLLECTION = 'clips_v2'
        CLIPSTAGE_VOLUMES_ROOT = 'C:/Volumes'; CLIPSTAGE_STAGING_PATH = 'C:/ClipStage-Staging'
        CLIPSTAGE_STAGING_SHARE = $StagingShare
        CLIPSTAGE_NAS_HOST = $S.NasHost; CLIPSTAGE_NAS_DOMAIN = $S.NasDomain
        CLIPSTAGE_NAS_USER = $S.NasUser; CLIPSTAGE_NAS_PASSWORD = $S.NasPass
        CLIPSTAGE_EDITOR_SHARE_USER = $EditorUser; CLIPSTAGE_EDITOR_SHARE_PASSWORD = $editorPass
    }
    # Read back what was just written - never continue with a missing backend / key.
    $be = (& $cur 'SEARCH_BACKEND').Trim().ToLower()
    if ($be -notin @('sqlite', 'typesense')) { Die 'SEARCH_BACKEND was not saved to .env' }
    if ($be -eq 'typesense' -and (Test-Placeholder (& $cur 'TYPESENSE_KEY'))) { Die 'TYPESENSE_KEY was not saved to .env' }
    Ok ".env written - SEARCH_BACKEND=$be"
    $edCount = 0
    try { $edCount = @((Get-Content -LiteralPath (Join-Path $Dest 'editors.json') -Raw | ConvertFrom-Json).editors).Count } catch { }
    Ok "editors.json: $edCount editor name(s)"
}

# =======================================================================================================
# 4. ffmpeg
# =======================================================================================================
function Step-Ffmpeg {
    Step 'ffmpeg'
    $bin = Join-Path $ToolsDir 'ffmpeg\bin'
    $ff = Join-Path $bin 'ffmpeg.exe'; $fp = Join-Path $bin 'ffprobe.exe'
    if (-not ((Test-Path -LiteralPath $ff) -and (Test-Path -LiteralPath $fp))) {
        $a = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
        $b = Get-Command ffprobe.exe -ErrorAction SilentlyContinue
        if ($a -and $b) { $ff = $a.Source; $fp = $b.Source }
        else {
            New-Item -ItemType Directory -Path $ToolsDir -Force | Out-Null
            $urls = @('https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip',
                      'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip')
            $done = $false
            foreach ($u in $urls) {
                try {
                    Info "downloading ffmpeg ($u) ..."
                    $zip = Join-Path $env:TEMP 'ffmpeg.zip'
                    Invoke-WebRequest -Uri $u -OutFile $zip -UseBasicParsing
                    $tmp = Join-Path $env:TEMP 'ffmpeg-extract'
                    if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force }
                    Expand-Archive -LiteralPath $zip -DestinationPath $tmp -Force
                    $exe = Get-ChildItem -LiteralPath $tmp -Recurse -Filter ffmpeg.exe | Select-Object -First 1
                    if ($exe) {
                        New-Item -ItemType Directory -Path $bin -Force | Out-Null
                        Copy-Item -Path (Join-Path $exe.DirectoryName '*.exe') -Destination $bin -Force
                        $done = $true
                    }
                    Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue
                    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
                    if ($done) { break }
                } catch { Warn "ffmpeg download failed ($($_.Exception.Message))" }
            }
            if (-not $done -and (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
                [void](Invoke-Quiet 'winget.exe' @('install', '-e', '--id', 'Gyan.FFmpeg', '--silent', '--accept-package-agreements', '--accept-source-agreements'))
                $a = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
                if ($a) { $ff = $a.Source; $fp = Join-Path (Split-Path $ff) 'ffprobe.exe'; $done = $true }
            }
            if (-not $done) { Die 'ffmpeg could not be installed. Download it from https://www.gyan.dev/ffmpeg/builds/ and unzip it to C:\ClipStage-tools\ffmpeg (so that ...\ffmpeg\bin\ffmpeg.exe exists), then run this installer again.' }
        }
    }
    Set-ClipEnv @{ CLIPSTAGE_FFMPEG = $ff.Replace('\', '/'); CLIPSTAGE_FFPROBE = $fp.Replace('\', '/') }
    Ok "ffmpeg: $ff"
}

# =======================================================================================================
# 5. WSL2 + Typesense
# =======================================================================================================
function Get-WslDistros {
    $old = [Console]::OutputEncoding
    $o = @()
    try {
        [Console]::OutputEncoding = [Text.Encoding]::Unicode
        $o = & wsl.exe --list --quiet 2>$null
    } catch { } finally { [Console]::OutputEncoding = $old }
    return @($o | ForEach-Object { (([string]$_) -replace "`0", '').Trim() } | Where-Object { $_ })
}
function Register-ResumeTask {
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $arg = '-NoProfile -ExecutionPolicy Bypass -NoExit -File "' + (Join-Path $Dest 'install-win-host.ps1') + '" -Resume -Yes'
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg -WorkingDirectory $Dest
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $me
    $principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Highest
    Register-ScheduledTask -TaskName $ResumeTask -Action $action -Trigger $trigger -Principal $principal -Force | Out-Null
}
function Get-UbuntuRootfsUrls {
    $urls = New-Object System.Collections.Generic.List[string]
    try {
        $c = (Invoke-WebRequest -Uri 'https://releases.ubuntu.com/noble/SHA256SUMS' -UseBasicParsing -TimeoutSec 30).Content
        if ($c -is [byte[]]) { $c = [Text.Encoding]::UTF8.GetString($c) }
        foreach ($m in [regex]::Matches([string]$c, '(ubuntu-[0-9.]+-wsl-amd64\.wsl)')) {
            $u = 'https://releases.ubuntu.com/noble/' + $m.Groups[1].Value
            if (-not $urls.Contains($u)) { $urls.Add($u) }
        }
    } catch { }
    $urls.Add('https://cloud-images.ubuntu.com/wsl/noble/current/ubuntu-noble-wsl-amd64-wsl.rootfs.tar.gz')
    $urls.Add('https://cloud-images.ubuntu.com/wsl/jammy/current/ubuntu-jammy-wsl-amd64-wsl.rootfs.tar.gz')
    return $urls
}
function Ensure-WslDistro {
    if ($UserDistro) {
        if ((Get-WslDistros) -notcontains $UserDistro) { Die "The WSL distro '$UserDistro' does not exist (see: wsl -l -v)." }
        return $UserDistro
    }
    $name = 'ClipStage'
    $saved = Get-ClipEnv 'CLIPSTAGE_WSL_DISTRO'          # chosen by an earlier run (or -WslDistro before a restart)
    if ($saved -and ((Get-WslDistros) -contains $saved)) { return $saved }
    if ((Get-WslDistros) -contains $name) { return $name }
    New-Item -ItemType Directory -Path $ToolsDir -Force | Out-Null
    $tar = Join-Path $ToolsDir 'rootfs.tar'
    $got = $false
    foreach ($u in (Get-UbuntuRootfsUrls)) {
        try {
            Info "downloading the Linux base image ($u) ..."
            Invoke-WebRequest -Uri $u -OutFile $tar -UseBasicParsing
            if ((Get-Item -LiteralPath $tar).Length -gt 100MB) { $got = $true; break }
        } catch { Info "  not available: $($_.Exception.Message)" }
    }
    if (-not $got) {
        Die ("Could not download a Linux base image for WSL. Do this once by hand, then run this installer again:`n" +
             "    wsl --install -d Ubuntu-24.04      (create any user when asked, then type exit)`n" +
             "    powershell -ExecutionPolicy Bypass -File .\install-win-host.ps1 -WslDistro Ubuntu-24.04")
    }
    New-Item -ItemType Directory -Path $WslHome -Force | Out-Null
    $r = Invoke-Quiet 'wsl.exe' @('--import', $name, $WslHome, $tar, '--version', '2')
    if ($r.Code -ne 0) {
        $hint = ''
        if ($r.Output -match '0x80370102|virtualization') { $hint = ' Switch on virtualization (Intel VT-x / AMD SVM) in the BIOS, or enable nested virtualization if this is a virtual machine.' }
        Die "wsl --import failed: $($r.Output)$hint"
    }
    Remove-Item -LiteralPath $tar -Force -ErrorAction SilentlyContinue
    Ok "created the WSL distro '$name'"
    return $name
}
function Convert-ToLf([string]$Text) { return $Text.Replace("`r`n", "`n") }
function Write-LfFile([string]$Path, [string]$Text) {
    [IO.File]::WriteAllText($Path, (Convert-ToLf $Text), (New-Object System.Text.UTF8Encoding($false)))
}
function Register-ClipTask {
    param([string]$Name, [string]$Command, $Trigger, [switch]$Restart)
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $arg = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + (Join-Path $Dest 'clipstage.ps1') + '" ' + $Command
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg -WorkingDirectory $Dest
    $principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Highest
    $p = @{ AllowStartIfOnBatteries = $true; DontStopIfGoingOnBatteries = $true; StartWhenAvailable = $true
            ExecutionTimeLimit = [TimeSpan]::Zero; MultipleInstances = 'IgnoreNew' }
    if ($Restart) { $p['RestartCount'] = 999; $p['RestartInterval'] = (New-TimeSpan -Minutes 1) }
    $settings = New-ScheduledTaskSettingsSet @p
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $Trigger -Principal $principal -Settings $settings -Force | Out-Null
}
function Step-Typesense {
    if (-not (Want-Typesense)) {
        Step 'Search engine (SQLite, built in)'
        Ok 'nothing to install: the search index is a file next to api.py (clipstage_search.db) - no WSL, no extra service'
        return
    }
    Step 'Typesense (search engine, inside WSL2)'
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $restart = $false
    foreach ($f in 'Microsoft-Windows-Subsystem-Linux', 'VirtualMachinePlatform') {
        $s = Get-WindowsOptionalFeature -Online -FeatureName $f
        if ($s.State -ne 'Enabled') {
            Info "switching on Windows feature $f ..."
            $r = Enable-WindowsOptionalFeature -Online -FeatureName $f -NoRestart -All
            if ($r.RestartNeeded) { $restart = $true }
        }
    }
    if ($restart) {
        Register-ResumeTask
        Warn 'Windows must restart once to finish switching on WSL2. After you sign in again this installer continues by itself.'
        if (YesNo 'Restart now?' $true) { Stop-Transcript | Out-Null; Restart-Computer -Force; exit 0 }
        Info 'Restart Windows when convenient and sign in - the installation resumes automatically.'
        Stop-Transcript | Out-Null
        exit 0
    }
    [void](Invoke-Quiet 'wsl.exe' @('--update'))
    [void](Invoke-Quiet 'wsl.exe' @('--set-default-version', '2'))

    $distro = Ensure-WslDistro
    $t = Invoke-Quiet 'wsl.exe' @('-d', $distro, '-u', 'root', '--exec', '/bin/echo', 'wsl-ok')
    if ($t.Output -notmatch 'wsl-ok') { Die "The WSL distro '$distro' does not start: $($t.Output)" }
    Set-ClipEnv @{ CLIPSTAGE_WSL_DISTRO = $distro }
    Ok "WSL2 distro '$distro' is working"
    Protect-Folder $WslHome

    $tsKey = Get-ClipEnv 'TYPESENSE_KEY'
    $port = Get-ClipEnv 'TYPESENSE_PORT' '8108'
    New-Item -ItemType Directory -Path $ToolsDir -Force | Out-Null
    $probe = Invoke-Quiet 'wsl.exe' @('-d', $distro, '-u', 'root', '--exec', 'test', '-x', '/opt/typesense/typesense-server')
    if ($probe.Code -ne 0) {
        $tgz = Join-Path $ToolsDir 'typesense.tar.gz'
        $ver = $null
        foreach ($v in $TypesenseVersions) {
            $u = "https://dl.typesense.org/releases/$v/typesense-server-$v-linux-amd64.tar.gz"
            try {
                Info "downloading Typesense $v ..."
                Invoke-WebRequest -Uri $u -OutFile $tgz -UseBasicParsing
                if ((Get-Item -LiteralPath $tgz).Length -gt 5MB) { $ver = $v; break }
            } catch { Info "  not available: $($_.Exception.Message)" }
        }
        if (-not $ver) { Die 'Could not download Typesense (https://dl.typesense.org). Check the internet connection and run the installer again.' }
        $tgzWsl = (Invoke-Quiet 'wsl.exe' @('-d', $distro, '-u', 'root', '--exec', 'wslpath', '-a', $tgz)).Output.Trim()
        $sh = Join-Path $ToolsDir 'setup-typesense.sh'
        $shWsl = (Invoke-Quiet 'wsl.exe' @('-d', $distro, '-u', 'root', '--exec', 'wslpath', '-a', $sh)).Output.Trim()
        Write-LfFile $sh ("#!/bin/bash`nset -e`nmkdir -p /opt/typesense /var/lib/typesense /var/log/typesense /etc/typesense`n" +
                          "tar -xzf '$tgzWsl' -C /opt/typesense`nchmod +x /opt/typesense/typesense-server`n/opt/typesense/typesense-server --version | head -1`n")
        $r = Invoke-Quiet 'wsl.exe' @('-d', $distro, '-u', 'root', '--exec', 'bash', $shWsl)
        Remove-Item -LiteralPath $sh, $tgz -Force -ErrorAction SilentlyContinue
        if ($r.Code -ne 0) { Die "Installing Typesense in WSL failed: $($r.Output)" }
        Ok "Typesense $ver installed"
    } else { Ok 'Typesense is already installed in WSL' }

    # config file with the key (kept inside WSL, readable by root only - never on a command line)
    $ini = Join-Path $ToolsDir 'typesense-server.ini'
    $addr = '127.0.0.1'
    Write-LfFile $ini "[server]`napi-key = $tsKey`ndata-dir = /var/lib/typesense`nlog-dir = /var/log/typesense`napi-address = $addr`napi-port = $port`n"
    try {
        $iniWsl = (Invoke-Quiet 'wsl.exe' @('-d', $distro, '-u', 'root', '--exec', 'wslpath', '-a', $ini)).Output.Trim()
        $r = Invoke-Quiet 'wsl.exe' @('-d', $distro, '-u', 'root', '--exec', 'bash', '-c', "cp '$iniWsl' /etc/typesense/typesense-server.ini && chmod 600 /etc/typesense/typesense-server.ini")
        if ($r.Code -ne 0) { Die "Could not write the Typesense config: $($r.Output)" }
    } finally { Remove-Item -LiteralPath $ini -Force -ErrorAction SilentlyContinue }

    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $me
    Register-ClipTask -Name 'ClipStage-Typesense' -Command 'typesense-run' -Trigger $trigger -Restart
    Stop-ScheduledTask -TaskName 'ClipStage-Typesense' -ErrorAction SilentlyContinue
    [void](Invoke-Quiet 'wsl.exe' @('--terminate', $distro))
    Start-ScheduledTask -TaskName 'ClipStage-Typesense'
    $up = $false
    for ($i = 0; $i -lt 45; $i++) { if (Test-Typesense) { $up = $true; break }; Start-Sleep -Seconds 1 }
    if (-not $up) {
        Warn 'Typesense did not answer on localhost - retrying with a different listen address.'
        [void](Invoke-Quiet 'wsl.exe' @('-d', $distro, '-u', 'root', '--exec', 'sed', '-i', 's/^api-address.*/api-address = 0.0.0.0/', '/etc/typesense/typesense-server.ini'))
        Stop-ScheduledTask -TaskName 'ClipStage-Typesense' -ErrorAction SilentlyContinue
        [void](Invoke-Quiet 'wsl.exe' @('--terminate', $distro))
        Start-ScheduledTask -TaskName 'ClipStage-Typesense'
        for ($i = 0; $i -lt 45; $i++) { if (Test-Typesense) { $up = $true; break }; Start-Sleep -Seconds 1 }
    }
    if ($up) { Ok 'Typesense answers on http://localhost:8108' }
    else { Die ('Typesense did not start. Read C:\ClipStage\typesense.log, and try: wsl -d ' + $distro + ' -u root --exec /opt/typesense/typesense-server --config=/etc/typesense/typesense-server.ini') }
}

# =======================================================================================================
# 6. NAS access
# =======================================================================================================
function Step-Nas {
    Step 'NAS volumes (C:\Volumes\<share>)'
    $nas = Get-ClipEnv 'CLIPSTAGE_NAS_HOST'
    $user = Get-NasUser; $pw = Get-ClipEnv 'CLIPSTAGE_NAS_PASSWORD'
    if ($nas -and $user -and $pw) {
        [void](Invoke-Quiet 'cmdkey.exe' @("/add:$nas", "/user:$user", "/pass:$pw"))
        Ok "NAS login stored in Windows Credential Manager for $nas"
    }
    # let programs run by the scheduled tasks create the links without a UAC prompt
    $k = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock'
    try {
        if (-not (Test-Path $k)) { New-Item -Path $k -Force | Out-Null }
        Set-ItemProperty -Path $k -Name 'AllowDevelopmentWithoutDevLicense' -Value 1 -Type DWord
    } catch { }
    $m = Get-MountResult
    $m.Lines | ForEach-Object { Info $_ }
    if ($m.Failed -gt 0) { Warn 'Some NAS volumes are not reachable yet - check the NAS address / login / share names (run the installer with -Reconfigure).' }
    elseif ($nas) { Ok 'all NAS volumes are reachable' }
    Protect-Folder $VolumesRoot @('S-1-5-32-545')
}

# =======================================================================================================
# 7. staging folder, share, firewall
# =======================================================================================================
function Step-Staging {
    Step 'Staging folder, network share and firewall'
    $admins = Get-AccountName 'S-1-5-32-544'
    $pass = Get-ClipEnv 'CLIPSTAGE_EDITOR_SHARE_PASSWORD'
    $sec = ConvertTo-SecureString $pass -AsPlainText -Force
    if (Get-LocalUser -Name $EditorUser -ErrorAction SilentlyContinue) {
        Set-LocalUser -Name $EditorUser -Password $sec
    } else {
        New-LocalUser -Name $EditorUser -Password $sec -PasswordNeverExpires -UserMayNotChangePassword -AccountNeverExpires -Description 'ClipStage editors (staging share)' | Out-Null
    }
    Ok "local account '$EditorUser' (read-only share access for editors)"

    Protect-Folder $StagingDir @() @("$env:COMPUTERNAME\$EditorUser")
    Get-SmbShare -Name $StagingShare -ErrorAction SilentlyContinue | Remove-SmbShare -Force
    New-SmbShare -Name $StagingShare -Path $StagingDir -ReadAccess "$env:COMPUTERNAME\$EditorUser" -FullAccess $admins -Description 'ClipStage staging (read-only for editors)' | Out-Null
    Ok "shared as \\$(Get-ClipEnv 'CLIPSTAGE_SMB_HOST')\$StagingShare"

    foreach ($p in @(Get-NetConnectionProfile -ErrorAction SilentlyContinue)) {
        if ($p.NetworkCategory -eq 'Public') {
            if (YesNo "The network '$($p.Name)' is set to Public, which blocks file sharing. Switch it to Private?" $true) {
                Set-NetConnectionProfile -InterfaceIndex $p.InterfaceIndex -NetworkCategory Private
            }
        }
    }
    try { Get-NetFirewallRule -Group '@FirewallAPI.dll,-28502' | Enable-NetFirewallRule } catch { Warn 'Could not enable the File and Printer Sharing firewall rules.' }
    $port = Get-ClipEnv 'CLIPSTAGE_API_PORT' '8000'
    Get-NetFirewallRule -DisplayName 'ClipStage API' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName 'ClipStage API' -Direction Inbound -Action Allow -Protocol TCP -LocalPort $port -Profile Domain, Private | Out-Null
    Ok "firewall: TCP $port and file sharing allowed on private/domain networks"

    # lock the program folders so ordinary users cannot edit what runs elevated at logon
    Protect-Folder $Dest
    Protect-Folder $ToolsDir @('S-1-5-32-545')
}

# =======================================================================================================
# 8. scheduled tasks
# =======================================================================================================
function Step-Tasks {
    Step 'Background tasks (start at sign-in, restart if they stop)'
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $t = New-ScheduledTaskTrigger -AtLogOn -User $me
    $t.Delay = 'PT20S'
    Register-ClipTask -Name 'ClipStage-API' -Command 'api-run' -Trigger $t -Restart
    Ok 'ClipStage-API (starts at sign-in)'
    Register-ClipTask -Name 'ClipStage-Nightly' -Command 'nightly' -Trigger (New-ScheduledTaskTrigger -Daily -At '17:00')
    Ok 'ClipStage-Nightly (index + thumbnails + database backup, daily 17:00)'
    if (Get-ClipEnv 'CLIPSTAGE_NAS_HOST') {
        $w = New-ScheduledTaskTrigger -AtLogOn -User $me
        $rep = New-ScheduledTaskTrigger -Once -At '00:00' -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
        $w.Repetition = $rep.Repetition
        Register-ClipTask -Name 'ClipStage-MountWatch' -Command 'mountwatch' -Trigger $w
        Ok 'ClipStage-MountWatch (checks the NAS every 5 minutes)'
    }
    Info 'These run while the installing user is signed in. For an unattended server, set Windows to sign in automatically (netplwiz).'
}

# =======================================================================================================
# 9. start + check
# =======================================================================================================
function Step-Start {
    Step 'Starting ClipStage and checking it works'
    Stop-ScheduledTask -TaskName 'ClipStage-API' -ErrorAction SilentlyContinue
    Start-ScheduledTask -TaskName 'ClipStage-API'
    $up = $false
    for ($i = 0; $i -lt 90; $i++) { if (Test-ClipApi) { $up = $true; break }; Start-Sleep -Seconds 1 }
    if ($up) { Ok 'API answers on http://127.0.0.1:8000' } else { Warn 'The API did not answer within 90 s. Read C:\ClipStage\api.log' }
    if (-not (Use-Typesense)) { Ok 'Search engine: SQLite (built in)' }
    elseif (Test-Typesense) { Ok 'Typesense is running' } else { Warn 'Typesense is not answering.' }
    $ip = Get-ClipEnv 'CLIPSTAGE_SMB_HOST'
    if ($up -and $ip) {
        try { [void](Invoke-RestMethod -Uri "http://${ip}:8000/health" -TimeoutSec 5); Ok "reachable on the network at http://${ip}:8000" }
        catch { Warn "http://${ip}:8000 is not reachable from this PC itself - check CLIPSTAGE_SMB_HOST and the firewall." }
    }
    $script:ApiUp = $up
}

# =======================================================================================================
# 10. editor kit
# =======================================================================================================
function Step-EditorKit {
    Step 'Editor kit for the editors'' PCs'
    $kit = Join-Path $Dest 'editor-kit'
    if (Test-Path -LiteralPath $kit) { Remove-Item -LiteralPath $kit -Recurse -Force }
    New-Item -ItemType Directory -Path $kit -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $Dest 'install-win-editor.ps1') -Destination $kit
    $app = Join-Path $kit 'clipstage-fcp-drag'
    New-Item -ItemType Directory -Path $app -Force | Out-Null
    foreach ($f in 'main.js', 'pathmap.js', 'preload.js', 'package.json', 'config.json') {
        Copy-Item -LiteralPath (Join-Path $Dest "clipstage-fcp-drag\$f") -Destination $app
    }
    $ip = Get-ClipEnv 'CLIPSTAGE_SMB_HOST'
    $cfg = [ordered]@{
        serverUrl = "http://${ip}:8000"; host = $ip; hostName = $env:COMPUTERNAME
        shareName = $StagingShare; shareUser = $EditorUser; sharePassword = (Get-ClipEnv 'CLIPSTAGE_EDITOR_SHARE_PASSWORD')
        nasHost = (Get-ClipEnv 'CLIPSTAGE_NAS_HOST'); nasDomain = ''; nasUser = ''; nasPassword = ''
        volumes = @(Get-ClipVolumes)
    }
    if ($IncludeNas) {
        $cfg.nasDomain = (Get-ClipEnv 'CLIPSTAGE_NAS_DOMAIN'); $cfg.nasUser = (Get-ClipEnv 'CLIPSTAGE_NAS_USER')
        $cfg.nasPassword = (Get-ClipEnv 'CLIPSTAGE_NAS_PASSWORD')
    }
    [IO.File]::WriteAllText((Join-Path $kit 'editor-config.json'), ($cfg | ConvertTo-Json -Depth 4), (New-Object System.Text.UTF8Encoding($false)))
    Protect-Folder $kit @() @("$env:COMPUTERNAME\$EditorUser")
    Get-SmbShare -Name $KitShare -ErrorAction SilentlyContinue | Remove-SmbShare -Force
    New-SmbShare -Name $KitShare -Path $kit -ReadAccess "$env:COMPUTERNAME\$EditorUser" -FullAccess (Get-AccountName 'S-1-5-32-544') -Description 'ClipStage editor setup (read-only)' | Out-Null
    Ok "editor kit shared as \\$ip\$KitShare"
}
function Show-EditorCommand {
    $ip = Get-ClipEnv 'CLIPSTAGE_SMB_HOST'
    $pw = Get-ClipEnv 'CLIPSTAGE_EDITOR_SHARE_PASSWORD'
    Write-Host ''
    Write-Host '  On EACH editor PC, paste this ONE line into PowerShell (no admin needed - it asks for it itself):' -ForegroundColor White
    Write-Host ''
    Write-Host ("  cmdkey /add:$ip /user:$env:COMPUTERNAME\$EditorUser /pass:$pw; powershell -ExecutionPolicy Bypass -File \\$ip\$KitShare\install-win-editor.ps1") -ForegroundColor Yellow
    Write-Host ''
    Write-Host '  (This line contains the editors'' read-only share password. It is also saved in C:\ClipStage\editor-kit\editor-config.json.)'
}

# =======================================================================================================
# 11. logins + first index
# =======================================================================================================
function Step-LoginsAndIndex {
    Step 'Logins and the first index'
    $sbUrl = Get-ClipEnv 'SUPABASE_URL'
    $sql = Join-Path $Dest 'supabase_profiles_setup.sql'
    if (Test-Path -LiteralPath $sql) { try { Set-Clipboard -Value (Get-Content -LiteralPath $sql -Raw); Ok 'supabase_profiles_setup.sql is on your clipboard' } catch { } }
    Info 'Do this once in the Supabase dashboard (the installer cannot - it never holds your admin keys):'
    Info '  1. Authentication -> Users -> Add user (you as admin, and each editor, "auto confirm")'
    Info '  2. SQL editor -> paste (Ctrl+V) -> Run   [creates the profiles table]'
    Info '  3. Give each user a role + expiry: see the examples at the bottom of supabase_profiles_setup.sql'
    if ($sbUrl -match 'https://([^.]+)\.') { try { Start-Process ("https://supabase.com/dashboard/project/" + $Matches[1] + "/sql/new") } catch { } }

    if ((-not $NoIndex) -and $script:ApiUp) {
        $missing = @()
        $root = (Get-ClipEnv 'CLIPSTAGE_VOLUMES_ROOT' 'C:/Volumes').Replace('/', '\')
        foreach ($v in (Get-ClipVolumes)) { if (-not (Test-Reachable (Join-Path $root $v))) { $missing += $v } }
        if ($missing.Count -gt 0) { Info ("Not indexing yet - not reachable: " + ($missing -join ', ')) }
        elseif (YesNo 'Start indexing the NAS now? (runs in the background; the app works while it indexes)' $true) {
            [void](Invoke-Logged -Exe $script:Python -Arguments 'indexer.py --prune' -Log (Join-Path $Dest 'indexer.log') -NoWait)
            Ok 'indexing started - follow it with:  .\clipstage.ps1 logs'
        }
    } else { Info 'First index skipped. Start it later with:  .\clipstage.ps1 index   (or the Sync Index button in the app)' }
}

# =======================================================================================================
# what is already here, and what this installer will install
# =======================================================================================================
function Show-Plan {
    $havePy = [bool](Find-Python)
    $haveFf = ((Test-Path -LiteralPath (Join-Path $ToolsDir 'ffmpeg\bin\ffmpeg.exe')) -or
               ((Get-Command ffmpeg.exe -ErrorAction SilentlyContinue) -and (Get-Command ffprobe.exe -ErrorAction SilentlyContinue)))
    $wslOn = $false; $haveDistro = $false
    try {
        $wslOn = ((Get-WindowsOptionalFeature -Online -FeatureName 'Microsoft-Windows-Subsystem-Linux').State -eq 'Enabled') -and
                 ((Get-WindowsOptionalFeature -Online -FeatureName 'VirtualMachinePlatform').State -eq 'Enabled')
        if ($wslOn) { $haveDistro = (@(Get-WslDistros) -contains 'ClipStage') }
    } catch { }
    function Word($have, $yes, $no) { if ($have) { return $yes } else { return $no } }
    Write-Host ''
    Write-Host '  What this installer will do' -ForegroundColor White
    Write-Host ''
    Write-Host ("    Python 3.10+                    {0}" -f (Word $havePy 'already installed' 'WILL BE INSTALLED (winget, or python.org)'))
    Write-Host ("    ffmpeg                          {0}" -f (Word $haveFf 'already installed' 'WILL BE DOWNLOADED (a Windows build of ffmpeg)'))
    if (Want-Typesense) {
        Write-Host ("    WSL2 (Windows' built-in Linux)  {0}" -f (Word $wslOn 'already switched on' 'WILL BE SWITCHED ON - Windows may restart once'))
        Write-Host ("    Linux image + Typesense         {0}" -f (Word $haveDistro 'already present' 'WILL BE DOWNLOADED (about 400 MB)'))
    } else {
        Write-Host '    Search engine                   SQLite (built into Python) - nothing extra to install, no WSL'
    }
    Write-Host '    Python packages                 installed into a private folder (.venv) from pypi.org'
    Write-Host ''
    if (Want-Typesense) {
        Write-Host '    WSL2 is required because Typesense (the search engine) has no Windows version; it runs inside'
        Write-Host '    a small dedicated Linux. It needs virtualization switched on in the BIOS.'
        Write-Host ''
    }
    Write-Host '    It will also: create the local account "clipstage-editor" (read-only share access), share the'
    Write-Host '    staging folder and the editor kit, open TCP 8000 and file sharing in the firewall (private and'
    Write-Host '    domain networks), add scheduled tasks, set the "developer mode" flag so links can be created,'
    Write-Host '    and lock C:\ClipStage and C:\ClipStage-tools to administrators.'
    Write-Host '    You need: internet and about 15-30 minutes.'
    Write-Host ''
    if (-not (YesNo 'Continue and install everything above?' $true)) {
        Write-Host '  Nothing was changed.'
        Stop-Transcript | Out-Null
        exit 0
    }
}

# =======================================================================================================
# main
# =======================================================================================================
try {
    Write-Host ''
    Write-Host '  ClipStage - middle machine installer (Windows)' -ForegroundColor White
    Write-Host '  This takes about 15-30 minutes the first time. You will be asked a few questions.'

    if ($EditorKitOnly) {
        $Total = 1
        . (Join-Path $Dest 'clipstage_env.ps1')
        Step-EditorKit
        Show-EditorCommand
        Stop-Transcript | Out-Null
        return
    }

    if (-not $Resume) { Step-ChooseSearch }
    $build = [Environment]::OSVersion.Version.Build
    if ((Want-Typesense) -and $build -lt 19041) { Die "Windows 10 version 2004 (build 19041) or newer is required for WSL2 (this is build $build)." }
    if (-not [Environment]::Is64BitOperatingSystem) { Die 'A 64-bit Windows is required.' }

    if (-not $Resume) { Show-Plan }
    Step-CopyProject
    Set-Location -LiteralPath $Dest
    . (Join-Path $Dest 'clipstage_env.ps1')
    Step-Python
    Step-Settings
    Step-Ffmpeg
    Step-Typesense
    Step-Nas
    Step-Staging
    Step-Tasks
    Step-Start
    Step-EditorKit
    Step-LoginsAndIndex

    Step 'Done'
    Get-ScheduledTask -TaskName $ResumeTask -ErrorAction SilentlyContinue | Unregister-ScheduledTask -Confirm:$false -ErrorAction SilentlyContinue
    $ip = Get-ClipEnv 'CLIPSTAGE_SMB_HOST'
    Write-Host ''
    Write-Host '  ClipStage is installed.' -ForegroundColor Green
    Write-Host ''
    Write-Host "  Open it:      http://${ip}:8000   (from any computer on the network)"
    Write-Host "  Day-to-day:   cd $Dest ; .\clipstage.ps1 status      (also: restart, logs, index, backup)"
    Show-EditorCommand
    Write-Host ''
    Write-Host '  Editors with a Mac: copy the ClipStage folder to the Mac and run  bash install-mac-editor.sh --host ' -NoNewline
    Write-Host $ip
    if ($script:Warnings.Count -gt 0) {
        Write-Host ''
        Write-Host '  Things to look at:' -ForegroundColor Yellow
        foreach ($w in $script:Warnings) { Write-Host "   - $w" -ForegroundColor Yellow }
    }
    Write-Host ''
} catch {
    Show-Fatal $_
} finally {
    try { Stop-Transcript | Out-Null } catch { }
}
