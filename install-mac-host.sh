#!/bin/bash
# =============================================================================================
#  ClipStage - ONE-COMMAND INSTALLER for the MIDDLE MACHINE (macOS)
#
#      bash install-mac-host.sh
#
#  Installs and configures everything: Homebrew, Python, ffmpeg, Typesense, the Python
#  environment, the .env file, the NAS mounts, the staging share, the background services
#  (API at login, nightly index + backup, NAS mount watchdog) and then checks it all works.
#
#  Safe to run again at any time (repairs / upgrades; keeps your .env, notes and index).
#
#  Options:   --reconfigure   ask every question again (default: only ask what is missing)
#             --no-index      do not start the first index at the end
#             --yes           never ask; use existing .env / exported variables / defaults
# =============================================================================================
set -uo pipefail

RECONFIGURE=0; SKIP_INDEX=0; ASSUME_YES=0
for a in "$@"; do
    case "$a" in
        --reconfigure) RECONFIGURE=1 ;;
        --no-index)    SKIP_INDEX=1 ;;
        --yes|-y)      ASSUME_YES=1 ;;
        -h|--help)     sed -n 2,20p "$0"; exit 0 ;;
        *) echo "Unknown option: $a"; exit 2 ;;
    esac
done

# Typesense versions to try, newest first (override: TYPESENSE_VERSION=29.1 bash install-mac-host.sh)
TYPESENSE_VERSIONS="${TYPESENSE_VERSION:-30.2 29.1 27.1}"
STAGING_DIR="${CLIPSTAGE_STAGING_PATH:-/Users/Shared/staging}"
STAGING_SHARE="staging"

# ---------- tiny UI helpers -------------------------------------------------------------------
B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; C=$'\033[36m'; N=$'\033[0m'
STEP=0; TOTAL=11
step() { STEP=$((STEP + 1)); printf '\n%s[%d/%d] %s%s\n' "$B$C" "$STEP" "$TOTAL" "$1" "$N"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$1"; }
info() { printf '    %s\n' "$1"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$1"; WARNINGS+=("$1"); }
die()  { printf '\n%s✗ %s%s\n' "$R" "$1" "$N" >&2; exit 1; }
WARNINGS=()

TTY=/dev/tty; { : < "$TTY"; } 2>/dev/null || TTY=/dev/stdin     # no terminal (pipes, CI): read answers from stdin
# ask "Question" "default" -> sets REPLY_VAL
ask() {
    local q="$1" def="${2:-}" ans=""
    if [ "$ASSUME_YES" -eq 1 ]; then REPLY_VAL="$def"; return; fi
    if [ -n "$def" ]; then read -r -p "  $q [$def]: " ans < "$TTY" || die "Input ended (no terminal). Run the installer in a Terminal window, or use --yes."
    else read -r -p "  $q: " ans < "$TTY" || die "Input ended (no terminal). Run the installer in a Terminal window, or use --yes."; fi
    REPLY_VAL="${ans:-$def}"
}
ask_secret() {   # ask_secret "Question" -> REPLY_VAL (not echoed)
    local ans=""
    if [ "$ASSUME_YES" -eq 1 ]; then REPLY_VAL=""; return; fi
    read -r -s -p "  $1: " ans < "$TTY" || die "Input ended (no terminal)."; echo
    REPLY_VAL="$ans"
}
yesno() {        # yesno "Question" default(Y/N) -> 0 for yes
    local ans="" def="${2:-Y}"
    if [ "$ASSUME_YES" -eq 1 ]; then [ "$def" = "Y" ]; return; fi
    read -r -p "  $1 [$def/$([ "$def" = Y ] && echo n || echo y)]: " ans < "$TTY" || die "Input ended (no terminal)."
    ans="${ans:-$def}"; case "$ans" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

[ "$(uname -s)" = "Darwin" ] || die "This installer is for macOS. On Windows use install-win-host.ps1."
[ "$(id -u)" -ne 0 ] || die "Do not run this with sudo. Run it as your normal user; it asks for your password when needed."

# ---------- locate the project (and move it out of Downloads/Desktop) -------------------------
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$SRC_DIR"
case "$SRC_DIR" in
    "$HOME/Downloads"*|"$HOME/Desktop"*)
        APP_DIR="$HOME/Documents/ClipStage"
        ;;
esac

printf '%s\n  ClipStage - middle machine installer (macOS)\n%s' "$B" "$N"
printf '  Project folder: %s\n' "$APP_DIR"
printf '  This takes about 10-20 minutes the first time. You will be asked a few questions.\n'

# ---------- what is already here, and what this installer will install ------------------------
brew_shellenv() {
    if [ -x /opt/homebrew/bin/brew ]; then eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [ -x /usr/local/bin/brew ]; then eval "$(/usr/local/bin/brew shellenv)"; fi
}
brew_shellenv
py_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; }
BASE_PY=""
find_python() {
    local c p
    for c in python3.12 python3.13 python3.11 python3.10 python3; do
        p="$(command -v "$c" 2>/dev/null || true)"
        [ -n "$p" ] || continue
        # /usr/bin/python3 is Apple's stub: running it without the command line tools pops up an installer
        if [ "$p" = "/usr/bin/python3" ] && ! xcode-select -p >/dev/null 2>&1; then continue; fi
        py_ok "$p" && { BASE_PY="$p"; return 0; }
    done
    return 1
}
TYPESENSE_BIN="$(grep -s '^TYPESENSE_BIN=' "$APP_DIR/.env" | head -1 | cut -d= -f2-)"
find_typesense() {
    local c
    if [ -n "${TYPESENSE_BIN:-}" ] && [ -x "$TYPESENSE_BIN" ]; then return 0; fi
    for c in /opt/homebrew/bin/typesense-server /usr/local/bin/typesense-server \
             /opt/homebrew/Cellar/typesense-server@*/*/bin/typesense-server \
             /usr/local/Cellar/typesense-server@*/*/bin/typesense-server \
             "$HOME"/typesense-bin/typesense-server; do
        [ -x "$c" ] && { TYPESENSE_BIN="$c"; return 0; }
    done
    c="$(command -v typesense-server 2>/dev/null || true)"
    [ -n "$c" ] && { TYPESENSE_BIN="$c"; return 0; }
    return 1
}
MISSING=()
HAVE_PY=0; HAVE_FF=0; HAVE_TS=0; HAVE_BREW=0
if find_python; then HAVE_PY=1; else MISSING+=("Python 3.12"); fi
if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then HAVE_FF=1; else MISSING+=("ffmpeg"); fi
if find_typesense; then HAVE_TS=1; else MISSING+=("Typesense"); fi
command -v brew >/dev/null 2>&1 && HAVE_BREW=1
NEED_BREW=0
if [ "$HAVE_BREW" -eq 0 ] && [ "${#MISSING[@]}" -gt 0 ]; then NEED_BREW=1; fi

status_word() { if [ "$1" -eq 1 ]; then echo "already installed"; else echo "WILL BE INSTALLED"; fi; }
printf '\n%s  What this installer will do%s\n\n' "$B" "$N"
if [ "$HAVE_BREW" -eq 1 ]; then brew_state="already installed"
elif [ "$NEED_BREW" -eq 1 ]; then brew_state="WILL BE INSTALLED first (it also installs Apple's command line tools)"
else brew_state="not needed"; fi
printf '    Homebrew (package manager)   %s\n' "$brew_state"
printf '    Python 3.10+                 %s\n' "$(status_word "$HAVE_PY")"
printf '    ffmpeg                       %s\n' "$(status_word "$HAVE_FF")"
printf '    Typesense (search engine)    %s\n' "$(status_word "$HAVE_TS")"
printf '    Python packages              installed into a private folder (.venv) from pypi.org\n'
printf '\n    It will also: write .env (owner-only access), create and share the staging folder, allow\n'
printf '    Python through the firewall, and add 3 background services that start at login.\n'
printf "    You need: this Mac's password (asked once), internet, and about 10-20 minutes.\n\n"
if [ "$NEED_BREW" -eq 1 ]; then
    missing_list="$(printf '%s, ' "${MISSING[@]}")"; missing_list="${missing_list%, }"
    printf '  %sHomebrew is required%s to install: %s.\n  Without it, install those yourself (see INSTALL-FRESH.md) and run this installer again.\n\n' "$B" "$N" "$missing_list"
fi
if ! yesno "Continue and install everything above?" Y; then
    echo "  Nothing was changed."; exit 0
fi

step "Administrator access"
info "macOS will ask for your password once (needed for Homebrew, file sharing and the firewall)."
sudo -v || die "Administrator access is required."
( while true; do sudo -n true; sleep 50; kill -0 "$$" 2>/dev/null || exit; done 2>/dev/null ) &
SUDO_KEEPALIVE=$!
trap 'kill "$SUDO_KEEPALIVE" 2>/dev/null' EXIT
ok "got it"

if [ "$APP_DIR" != "$SRC_DIR" ]; then
    info "Copying ClipStage to $APP_DIR (Downloads is not a good permanent home)..."
    mkdir -p "$APP_DIR"
    rsync -a --exclude '.git' --exclude '.venv' --exclude 'node_modules' "$SRC_DIR"/ "$APP_DIR"/ || die "Could not copy the project."
fi
cd "$APP_DIR" || die "Cannot enter $APP_DIR"
chmod +x ./*.sh 2>/dev/null

# ---------- 1. Homebrew + packages -----------------------------------------------------------
step "Python and ffmpeg"
export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_ENV_HINTS=1
if [ "$NEED_BREW" -eq 1 ]; then
    info "Installing Homebrew (this also installs Apple's command line tools - it can take a while)..."
    NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" \
        || die "Homebrew installation failed. See https://brew.sh and run this installer again."
    brew_shellenv
    command -v brew >/dev/null 2>&1 || die "Homebrew was installed but 'brew' is not found."
fi
if command -v brew >/dev/null 2>&1; then ok "Homebrew $(brew --version | head -1 | awk '{print $2}')"; fi
need_brew() { brew list --formula "$1" >/dev/null 2>&1 || { info "brew install $1 ..."; brew install "$1" || die "brew install $1 failed"; }; }

if [ -z "$BASE_PY" ]; then
    command -v brew >/dev/null 2>&1 || die "Python 3.10+ is missing and Homebrew is not available to install it."
    need_brew python@3.12
    BASE_PY="$(brew --prefix)/bin/python3.12"
    py_ok "$BASE_PY" || die "Python 3.10+ not available after installing python@3.12"
fi
ok "Python $("$BASE_PY" --version | awk '{print $2}')"
if ! { command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; }; then
    command -v brew >/dev/null 2>&1 || die "ffmpeg is missing and Homebrew is not available to install it."
    need_brew ffmpeg
fi
ok "ffmpeg $(ffmpeg -version | head -1 | awk '{print $3}')"

# ---------- 2. Typesense ----------------------------------------------------------------------
step "Typesense (search engine)"
if ! find_typesense && command -v brew >/dev/null 2>&1; then
    for v in $TYPESENSE_VERSIONS; do
        info "Trying Typesense $v via Homebrew..."
        if brew install "typesense/tap/typesense-server@$v" >/dev/null 2>&1; then
            cand="$(brew --prefix "typesense-server@$v" 2>/dev/null)/bin/typesense-server"
            if [ -x "$cand" ]; then TYPESENSE_BIN="$cand"; break; fi
            find_typesense && break
        fi
    done
fi
if ! find_typesense; then
    arch="$(uname -m)"; [ "$arch" = "x86_64" ] && arch="amd64"
    for v in $TYPESENSE_VERSIONS; do
        info "Trying the Typesense $v download for macOS ($arch)..."
        mkdir -p "$HOME/typesense-bin"
        if curl -fsSL "https://dl.typesense.org/releases/$v/typesense-server-$v-darwin-$arch.tar.gz" \
              | tar -xz -C "$HOME/typesense-bin" 2>/dev/null && [ -x "$HOME/typesense-bin/typesense-server" ]; then
            xattr -dr com.apple.quarantine "$HOME/typesense-bin" 2>/dev/null
            TYPESENSE_BIN="$HOME/typesense-bin/typesense-server"; break
        fi
    done
fi
find_typesense || die "Could not install Typesense automatically. Install it from https://typesense.org/docs/guide/install-typesense.html (macOS), then run this installer again."
ok "typesense-server: $TYPESENSE_BIN"

# ---------- 3. Python environment ------------------------------------------------------------
step "ClipStage Python environment"
[ -x .venv/bin/python ] && py_ok .venv/bin/python || { rm -rf .venv; "$BASE_PY" -m venv .venv || die "Could not create the virtual environment."; }
PY="$APP_DIR/.venv/bin/python"
"$PY" -m pip install -q --upgrade pip >/dev/null 2>&1
"$PY" -m pip install -q -r requirements.txt || die "pip install failed (no internet?)."
"$PY" -c "import fastapi, typesense, requests, uvicorn; print('ok')" >/dev/null || die "Python packages did not import."
ok "fastapi, typesense, requests, uvicorn installed in .venv"

# ---------- 4. Questions + .env --------------------------------------------------------------
step "Settings (.env)"
ENVT=("$PY" envtool.py)
"${ENVT[@]}" init >/dev/null
cur() { "${ENVT[@]}" get "$1"; }
is_placeholder() { case "$1" in ""|CHANGE_ME*|*your-project*|your-supabase*|10.1.10.XXX|"<"*) return 0 ;; *) return 1 ;; esac; }

# Typesense key: generate once, keep afterwards
TS_KEY="$(cur TYPESENSE_KEY)"
if is_placeholder "$TS_KEY"; then TS_KEY="$(openssl rand -hex 24)"; ok "generated a new random Typesense key"; fi

SB_URL="$(cur SUPABASE_URL)"; SB_KEY="$(cur SUPABASE_ANON_KEY)"
NAS_HOST="$(cur CLIPSTAGE_NAS_HOST)"; NAS_DOMAIN="$(cur CLIPSTAGE_NAS_DOMAIN)"; NAS_USER="$(cur CLIPSTAGE_NAS_USER)"; NAS_PASS="$(cur CLIPSTAGE_NAS_PASSWORD)"
VOLS="$(cur CLIPSTAGE_SCAN_VOLUMES)"; SMB_HOST="$(cur CLIPSTAGE_SMB_HOST)"
ASK_ALL=$RECONFIGURE        # 1 = ask every question (Edit / --reconfigure); 0 = only what is missing

detect_ip() {
    local i ip
    for i in en0 en1 en2 en3; do ip="$(ipconfig getifaddr "$i" 2>/dev/null)"; [ -n "$ip" ] && { echo "$ip"; return; }; done
}

collect_answers() {
    local d mounted now
    if [ "$ASK_ALL" -eq 1 ] || is_placeholder "$SB_URL" || is_placeholder "$SB_KEY"; then
        echo "  Supabase holds the logins. Project Settings -> API in the Supabase dashboard has both values."
        while true; do
            ask "Supabase Project URL (https://xxxx.supabase.co)" "$(is_placeholder "$SB_URL" || echo "$SB_URL")"; SB_URL="${REPLY_VAL%/}"
            case "$SB_URL" in https://*) break ;; *) [ "$ASSUME_YES" -eq 1 ] && die "SUPABASE_URL is missing."; echo "  It must start with https://" ;; esac
        done
        while true; do
            ask "Supabase anon / publishable key (NOT the service-role key)" "$(is_placeholder "$SB_KEY" || echo "$SB_KEY")"; SB_KEY="$REPLY_VAL"
            [ ${#SB_KEY} -gt 20 ] && break
            [ "$ASSUME_YES" -eq 1 ] && die "SUPABASE_ANON_KEY is missing."
            echo "  That looks too short."
        done
    fi

    if [ "$ASK_ALL" -eq 1 ] || { [ -z "$NAS_HOST" ] && [ "$ASSUME_YES" -eq 0 ]; }; then
        echo "  NAS: ClipStage reads your footage from NAS shares. Give the NAS login once and the Mac"
        echo "  will mount the shares itself (also after reboots). Leave the address empty if the shares"
        echo "  are already mounted by other means."
        ask "NAS address (IP or name)" "$NAS_HOST"; NAS_HOST="$REPLY_VAL"
        if [ -n "$NAS_HOST" ]; then
            ask "NAS domain (empty if none)" "$NAS_DOMAIN"; NAS_DOMAIN="$REPLY_VAL"
            ask "NAS username" "$NAS_USER"; NAS_USER="$REPLY_VAL"
            ask_secret "NAS password (hidden; Enter keeps the saved one)"; [ -n "$REPLY_VAL" ] && NAS_PASS="$REPLY_VAL"
        else
            NAS_DOMAIN=""; NAS_USER=""; NAS_PASS=""
        fi
    fi

    if [ "$ASK_ALL" -eq 1 ] || is_placeholder "$VOLS"; then
        mounted=""
        for d in /Volumes/*; do
            [ -d "$d" ] || continue
            [ "$(basename "$d")" = "Macintosh HD" ] && continue
            mounted="${mounted:+$mounted,}$(basename "$d")"
        done
        [ -n "$mounted" ] && echo "  Currently mounted: $mounted"
        if is_placeholder "$VOLS"; then d="EDIT,EDIT2,INGEST,PLAYOUT,DIGITAL,SHARE FOLDER,TRANSCODER"; else d="$VOLS"; fi
        ask "Volumes to index, comma separated, exactly as they appear in /Volumes" "$d"; VOLS="$REPLY_VAL"
    fi

    if [ "$ASK_ALL" -eq 1 ] || is_placeholder "$SMB_HOST"; then
        if is_placeholder "$SMB_HOST"; then d="$(detect_ip)"; else d="$SMB_HOST"; fi
        ask "This Mac's address as editors will see it" "$d"; SMB_HOST="$REPLY_VAL"
    fi
    [ -n "$SMB_HOST" ] || SMB_HOST="$(detect_ip)"

    now="$("$PY" -c "import json,sys;print(','.join(json.load(open('editors.json'))['editors']))" 2>/dev/null || true)"
    if [ ! -f editors.json ] || [ "$ASK_ALL" -eq 1 ]; then
        ask "Editor names for the staging dropdown (comma separated)" "${now:-EDITOR1,EDITOR2}"
        "$PY" - "$REPLY_VAL" <<'PYEOF'
import json, sys
names = [n.strip() for n in sys.argv[1].split(",") if n.strip() and not any(c in n for c in "/\\")]
json.dump({"editors": names}, open("editors.json", "w"), indent=2)
PYEOF
    fi
}

show_summary() {
    local sb_state="" nas_state="" code ed
    code="$(curl -s -m 8 -o /dev/null -w '%{http_code}' "$SB_URL/auth/v1/health" -H "apikey: $SB_KEY" 2>/dev/null)"; [ -n "$code" ] || code=000
    if [ "$code" = "200" ]; then sb_state="reachable"; else sb_state="NOT reachable (HTTP $code) - check the URL/key or the internet"; fi
    if [ -n "$NAS_HOST" ]; then
        if command -v nc >/dev/null 2>&1 && nc -z -w 3 "$NAS_HOST" 445 >/dev/null 2>&1; then nas_state="reachable"; else nas_state="NOT reachable on port 445 - check the address"; fi
    fi
    ed="$("$PY" -c "import json;print(', '.join(json.load(open('editors.json'))['editors']))" 2>/dev/null || true)"
    printf '\n%s  Please check your entries%s\n\n' "$B" "$N"
    printf '    Supabase URL       %s   [%s]\n' "$SB_URL" "$sb_state"
    printf '    Supabase key       %s... (%s characters)\n' "${SB_KEY:0:6}" "${#SB_KEY}"
    if [ -n "$NAS_HOST" ]; then
        printf '    NAS                %s   [%s]\n' "$NAS_HOST" "$nas_state"
        printf '    NAS login          %s%s   password: %s\n' "${NAS_DOMAIN:+$NAS_DOMAIN\\}" "${NAS_USER:-(none)}" "$([ -n "$NAS_PASS" ] && echo '******** (saved)' || echo '(none)')"
    else
        printf '    NAS                (not set - the shares must already be mounted)\n'
    fi
    printf '    Volumes            %s\n' "$VOLS"
    printf "    This Mac's address %s\n" "$SMB_HOST"
    printf '    Editors            %s\n\n' "$ed"
}

collect_answers
while true; do
    show_summary
    [ "$ASSUME_YES" -eq 1 ] && break
    ask "Is this correct?  C = confirm and continue,  E = edit (asks every entry again)" "C"
    case "$REPLY_VAL" in
        [Ee]*) ASK_ALL=1; collect_answers ;;
        *) break ;;
    esac
done

"${ENVT[@]}" set TYPESENSE_KEY "$TS_KEY" TYPESENSE_BIN "$TYPESENSE_BIN" SUPABASE_URL "$SB_URL" SUPABASE_ANON_KEY "$SB_KEY" \
    CLIPSTAGE_SCAN_VOLUMES "$VOLS" CLIPSTAGE_SMB_HOST "$SMB_HOST" CLIPSTAGE_BIND_HOST 0.0.0.0 \
    CLIPSTAGE_COLLECTION clips_v2 CLIPSTAGE_VOLUMES_ROOT /Volumes CLIPSTAGE_STAGING_PATH "$STAGING_DIR" \
    CLIPSTAGE_NAS_HOST "$NAS_HOST" CLIPSTAGE_NAS_DOMAIN "$NAS_DOMAIN" CLIPSTAGE_NAS_USER "$NAS_USER" CLIPSTAGE_NAS_PASSWORD "$NAS_PASS"
chmod 600 .env
ok ".env written (permissions: owner only)"
ok "editors.json: $("$PY" -c "import json;print(len(json.load(open('editors.json'))['editors']))") editor name(s)"

# ---------- 5. NAS + staging ------------------------------------------------------------------
step "NAS volumes and the staging share"
if [ -n "$NAS_HOST" ] && [ -n "$NAS_USER" ]; then
    out="$(bash -c '. ./clipstage_env.sh && mount_volumes' 2>&1)"; echo "$out" | sed 's/^/  /'
    echo "$out" | grep -q 'FAILED' && warn "Some NAS volumes did not mount - check the NAS address/login (see above)."
fi
missing=""
IFS=',' read -r -a _v <<< "$VOLS"
for v in "${_v[@]}"; do v="$(echo "$v" | sed 's/^ *//;s/ *$//')"; [ -n "$v" ] && [ ! -d "/Volumes/$v" ] && missing="$missing $v"; done
[ -n "$missing" ] && warn "Not mounted right now:$missing  (they are skipped until they appear in /Volumes)"
[ -z "$missing" ] && ok "all NAS volumes are mounted"

mkdir -p "$STAGING_DIR" && chmod 755 "$STAGING_DIR"
ok "staging folder: $STAGING_DIR"
if sudo sharing -l 2>/dev/null | grep -q "$STAGING_DIR"; then
    ok "staging folder is already shared"
else
    sudo sharing -a "$STAGING_DIR" -S "$STAGING_SHARE" -s 100 >/dev/null 2>&1 || true
    sudo launchctl load -w /System/Library/LaunchDaemons/com.apple.smbd.plist >/dev/null 2>&1 || true
    if sudo sharing -l 2>/dev/null | grep -q "$STAGING_DIR"; then
        ok "shared as smb://$SMB_HOST/$STAGING_SHARE"
    else
        warn "macOS did not let a script switch on File Sharing. One-time manual step: System Settings -> General -> Sharing -> File Sharing -> ON -> (i) -> add $STAGING_DIR -> name it '$STAGING_SHARE'."
        open "x-apple.systempreferences:com.apple.Sharing-Settings.extension" 2>/dev/null || true
    fi
fi

FW=/usr/libexec/ApplicationFirewall/socketfilterfw
if sudo "$FW" --getglobalstate 2>/dev/null | grep -qi 'enabled'; then
    PYREAL="$("$PY" -c 'import os,sys;print(os.path.realpath(sys.executable))')"
    sudo "$FW" --add "$PYREAL" >/dev/null 2>&1; sudo "$FW" --unblockapp "$PYREAL" >/dev/null 2>&1
    ok "firewall: Python allowed to accept connections"
fi

# ---------- 6. Background services ------------------------------------------------------------
step "Background services (start at login, restart if they crash)"
bash ./install_api_service.sh >/dev/null 2>&1 && ok "ClipStage API service installed" || warn "Could not install the API service (run: bash install_api_service.sh)"
bash ./install_nightly.sh >/dev/null 2>&1 && ok "nightly index + backup installed (17:00 daily)" || warn "Could not install the nightly job (run: bash install_nightly.sh)"
if [ -n "$NAS_HOST" ] && [ -n "$NAS_USER" ]; then
    bash ./install_mountwatch.sh >/dev/null 2>&1 && ok "NAS mount watchdog installed (every 5 min)" || warn "Could not install the mount watchdog"
fi

# ---------- 7. Health check -------------------------------------------------------------------
step "Checking that it works"
up=0
for _ in $(seq 1 60); do
    curl -sf -m 3 "http://127.0.0.1:8000/health" 2>/dev/null | grep -q ok && { up=1; break; }
    sleep 1
done
if [ "$up" -eq 1 ]; then ok "API answers on http://127.0.0.1:8000"; else warn "The API did not answer in 60 s. Read: tail -n 40 $APP_DIR/api.log"; fi
curl -sf -m 3 "http://localhost:8108/health" 2>/dev/null | grep -q true && ok "Typesense is running" || warn "Typesense is not answering yet (it is started by the API service)."
if [ "$up" -eq 1 ] && [ -n "$SMB_HOST" ]; then
    curl -sf -m 3 "http://$SMB_HOST:8000/health" >/dev/null 2>&1 && ok "reachable on the network at http://$SMB_HOST:8000" \
        || warn "http://$SMB_HOST:8000 is not reachable from this Mac itself - check CLIPSTAGE_SMB_HOST / firewall."
fi

# ---------- 8. First index --------------------------------------------------------------------
step "First index of the NAS"
if [ "$SKIP_INDEX" -eq 0 ] && [ "$up" -eq 1 ] && [ -z "$missing" ] && yesno "Start indexing the NAS now? (runs in the background; the app works while it indexes)" Y; then
    nohup ./manual_index.sh >> indexer.log 2>&1 &
    ok "indexing started - watch it with:  ./clipstage.sh logs"
else
    info "Skipped. Start it later with:  ./clipstage.sh index   (or the Sync Index button in the app)"
fi

# ---------- 9. Supabase users (the only part nobody can script) -------------------------------
step "Logins"
SB_REF="$(echo "$SB_URL" | sed -E 's#https://([^.]+)\..*#\1#')"
if [ -f supabase_profiles_setup.sql ]; then
    pbcopy < supabase_profiles_setup.sql 2>/dev/null && ok "supabase_profiles_setup.sql is on your clipboard"
fi
info "Do this once in the Supabase dashboard (the installer cannot - it never holds your admin keys):"
info "  1. Authentication -> Users -> Add user (you as admin, and each editor, 'auto confirm')"
info "  2. SQL editor -> paste (Cmd+V) -> Run   [creates the profiles table]"
info "  3. Give each user a role + expiry: see the examples at the bottom of supabase_profiles_setup.sql"
[ -n "$SB_REF" ] && open "https://supabase.com/dashboard/project/$SB_REF/sql/new" 2>/dev/null

# ---------- 10. Summary -----------------------------------------------------------------------
step "Done"
printf '\n%s  ClipStage is installed.%s\n\n' "$B$G" "$N"
printf '  Open it:            http://%s:8000   (from any computer on the network)\n' "${SMB_HOST:-127.0.0.1}"
printf '  Day-to-day:         cd %s && ./clipstage.sh status      (also: restart, logs, index, backup)\n' "$APP_DIR"
printf '  Editors'"'"' Macs:     copy this folder to each editor Mac and run:  bash install-mac-editor.sh --host %s\n' "${SMB_HOST:-<this-mac-ip>}"
printf '  Editors'"'"' Windows PCs: see INSTALL-QUICKSTART.md (install-win-editor.ps1)\n'
if [ "${#WARNINGS[@]}" -gt 0 ]; then
    printf '\n%s  Things to look at:%s\n' "$Y" "$N"
    for w in "${WARNINGS[@]}"; do printf '   - %s\n' "$w"; done
fi
echo
