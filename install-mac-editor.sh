#!/bin/bash
# =============================================================================================
#  ClipStage - ONE-COMMAND INSTALLER for an EDITOR MACHINE (macOS)
#
#      bash install-mac-editor.sh --host 10.1.10.203
#
#  What it does on this Mac:
#    1. mounts the NAS shares under /Volumes/<same names> (staged clips are links to those paths)
#       and keeps them mounted (every 5 minutes + at login)
#    2. mounts the middle machine's staging share at /Volumes/staging
#    3. installs the "ClipStage" desktop app into /Applications (drag staged clips into Final Cut Pro)
#
#  Options:  --host IP        address of the middle machine (asked if missing)
#            --no-app         skip the desktop app (use the browser instead)
#            --reconfigure    ask the questions again
# =============================================================================================
set -uo pipefail
HOST=""; NO_APP=0; RECONFIGURE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --host) HOST="${2:-}"; shift 2 ;;
        --no-app) NO_APP=1; shift ;;
        --reconfigure) RECONFIGURE=1; shift ;;
        -h|--help) sed -n 2,16p "$0"; exit 0 ;;
        *) echo "Unknown option: $1"; exit 2 ;;
    esac
done

B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; C=$'\033[36m'; N=$'\033[0m'
step() { printf '\n%s%s%s\n' "$B$C" "$1" "$N"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$1"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$1"; }
die()  { printf '\n%s✗ %s%s\n' "$R" "$1" "$N" >&2; exit 1; }
TTY=/dev/tty; { : < "$TTY"; } 2>/dev/null || TTY=/dev/stdin     # no terminal (pipes, CI): read answers from stdin
ask()        { local a=""; read -r -p "  $1${2:+ [$2]}: " a < "$TTY" || die "Input ended (no terminal). Run this in a Terminal window."; REPLY_VAL="${a:-${2:-}}"; }
ask_secret() { local a=""; read -r -s -p "  $1: " a < "$TTY" || die "Input ended (no terminal)."; echo; REPLY_VAL="$a"; }

[ "$(uname -s)" = "Darwin" ] || die "This installer is for macOS. On Windows use install-win-editor.ps1."
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/ClipStage-Editor"
printf '%s\n  ClipStage - editor machine installer (macOS)\n%s' "$B" "$N"
# ---- what will happen ----
for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$b" ] && eval "$("$b" shellenv)"; done
HAVE_NODE=0; HAVE_BREW=0
command -v node >/dev/null 2>&1 && HAVE_NODE=1
command -v brew >/dev/null 2>&1 && HAVE_BREW=1
printf '\n%s  What this installer will do%s\n\n' "$B" "$N"
printf '    Mount the NAS shares under /Volumes (same names as on the middle machine) and keep them mounted\n'
printf '    Mount the middle machine'"'"'s staging share at /Volumes/staging\n'
printf '    Add one background job that re-mounts them every 5 minutes\n'
if [ "$NO_APP" -eq 0 ] && [ -d "$SRC/clipstage-fcp-drag" ]; then
    if [ "$HAVE_NODE" -eq 1 ]; then printf '    ClipStage desktop app: Node.js already installed\n'
    elif [ "$HAVE_BREW" -eq 1 ]; then printf '    ClipStage desktop app: Node.js WILL BE INSTALLED with Homebrew\n'
    else printf '    ClipStage desktop app: needs Node.js, which needs HOMEBREW - both WILL BE INSTALLED (say No below to skip the app)\n'; fi
fi
printf '\n'
read -r -p "  Continue? [Y/n]: " _go < "$TTY" || die "Input ended (no terminal)."
case "${_go:-Y}" in [Nn]*) echo "  Nothing was changed."; exit 0 ;; esac
if [ "$NO_APP" -eq 0 ] && [ "$HAVE_NODE" -eq 0 ] && [ "$HAVE_BREW" -eq 0 ] && [ -d "$SRC/clipstage-fcp-drag" ]; then
    read -r -p "  Homebrew is required to install the desktop app. Install Homebrew? (N = skip the app, use the browser) [Y/n]: " _hb < "$TTY" || die "Input ended (no terminal)."
    case "${_hb:-Y}" in [Nn]*) NO_APP=1 ;; esac
fi

mkdir -p "$DEST"


# ---- settings (kept in ~/ClipStage-Editor/.env) ----
cp -f "$SRC/clipstage_env.sh" "$SRC/mount_volumes.sh" "$SRC/mount_watch.sh" "$SRC/install_mountwatch.sh" \
      "$SRC/com.clipstage.mountwatch.plist" "$DEST/" || die "Run this from the ClipStage folder (files are missing)."
chmod +x "$DEST"/*.sh
[ -f "$DEST/.env" ] || : > "$DEST/.env"
getv() { grep -s "^$1=" "$DEST/.env" | head -1 | cut -d= -f2-; }
setv() { # setv KEY VALUE
    local f="$DEST/.env" tmp; tmp="$(mktemp)"
    grep -v "^$1=" "$f" > "$tmp" 2>/dev/null; printf '%s=%s\n' "$1" "$2" >> "$tmp"; mv "$tmp" "$f"; chmod 600 "$f"
}

# ---- settings ----
step "Settings"
[ -n "$HOST" ] || HOST="$(getv CLIPSTAGE_STAGING_HOST)"
ST_USER="$(getv CLIPSTAGE_STAGING_USER)"; ST_PASS="$(getv CLIPSTAGE_STAGING_PASSWORD)"
NAS_HOST="$(getv CLIPSTAGE_NAS_HOST)"; NAS_DOMAIN="$(getv CLIPSTAGE_NAS_DOMAIN)"; NAS_USER="$(getv CLIPSTAGE_NAS_USER)"; NAS_PASS="$(getv CLIPSTAGE_NAS_PASSWORD)"
VOLS="$(getv CLIPSTAGE_SCAN_VOLUMES)"
ASK_ALL=$RECONFIGURE

collect_answers() {
    local tries=0
    if [ "$ASK_ALL" -eq 1 ] || [ -z "$HOST" ]; then
        while [ -z "${HOST:-}" ] || [ "$ASK_ALL" -eq 1 ]; do
            ask "Address of the middle machine (e.g. 10.1.10.203)" "$HOST"; HOST="$REPLY_VAL"
            [ -n "$HOST" ] && break
            tries=$((tries + 1)); [ "$tries" -gt 10 ] && die "The middle machine address is required."
        done
    fi
    if [ "$ASK_ALL" -eq 1 ] || [ -z "$ST_USER" ]; then
        echo "  Login for the middle machine's staging share (the Mac account that shares the staging folder):"
        ask "Username" "$ST_USER"; ST_USER="$REPLY_VAL"
        ask_secret "Password (hidden; Enter keeps the saved one)"; [ -n "$REPLY_VAL" ] && ST_PASS="$REPLY_VAL"
    fi
    if [ "$ASK_ALL" -eq 1 ] || [ -z "$VOLS" ]; then
        echo "  NAS login (your own, or the shared one). Leave the address empty if your NAS shares are already mounted."
        ask "NAS address" "$NAS_HOST"; NAS_HOST="$REPLY_VAL"
        if [ -n "$NAS_HOST" ]; then
            ask "NAS domain (empty if none)" "$NAS_DOMAIN"; NAS_DOMAIN="$REPLY_VAL"
            ask "NAS username" "$NAS_USER"; NAS_USER="$REPLY_VAL"
            ask_secret "NAS password (hidden; Enter keeps the saved one)"; [ -n "$REPLY_VAL" ] && NAS_PASS="$REPLY_VAL"
        else
            NAS_DOMAIN=""; NAS_USER=""; NAS_PASS=""
        fi
        ask "NAS volumes (comma separated, same names as on the middle machine)" "${VOLS:-EDIT,EDIT2,INGEST,PLAYOUT,DIGITAL,SHARE FOLDER,TRANSCODER}"; VOLS="$REPLY_VAL"
    fi
}

show_summary() {
    local mm nas
    mm="NOT reachable - check the address or the network"; nas=""
    if command -v nc >/dev/null 2>&1 && nc -z -w 3 "$HOST" 445 >/dev/null 2>&1; then mm="reachable"; fi
    printf '\n%s  Please check your entries%s\n\n' "$B" "$N"
    printf '    Middle machine     %s   [%s]\n' "$HOST" "$mm"
    printf '    Staging login      %s   password: %s\n' "${ST_USER:-(none)}" "$([ -n "$ST_PASS" ] && echo '******** (saved)' || echo '(none)')"
    if [ -n "$NAS_HOST" ]; then
        if command -v nc >/dev/null 2>&1 && nc -z -w 3 "$NAS_HOST" 445 >/dev/null 2>&1; then nas="reachable"; else nas="NOT reachable on port 445"; fi
        printf '    NAS                %s   [%s]\n' "$NAS_HOST" "$nas"
        printf '    NAS login          %s%s   password: %s\n' "${NAS_DOMAIN:+$NAS_DOMAIN\\}" "${NAS_USER:-(none)}" "$([ -n "$NAS_PASS" ] && echo '******** (saved)' || echo '(none)')"
    else
        printf '    NAS                (not set - the shares must already be mounted)\n'
    fi
    printf '    Volumes            %s\n\n' "$VOLS"
}

collect_answers
while true; do
    show_summary
    ask "Is this correct?  C = confirm and continue,  E = edit (asks every entry again)" "C"
    case "$REPLY_VAL" in [Ee]*) ASK_ALL=1; collect_answers ;; *) break ;; esac
done
[ -n "$HOST" ] || die "The middle machine address is required."
setv CLIPSTAGE_STAGING_HOST "$HOST"; setv CLIPSTAGE_STAGING_USER "$ST_USER"; setv CLIPSTAGE_STAGING_PASSWORD "$ST_PASS"
setv CLIPSTAGE_STAGING_SHARE staging
setv CLIPSTAGE_NAS_HOST "$NAS_HOST"; setv CLIPSTAGE_NAS_DOMAIN "$NAS_DOMAIN"; setv CLIPSTAGE_NAS_USER "$NAS_USER"; setv CLIPSTAGE_NAS_PASSWORD "$NAS_PASS"
setv CLIPSTAGE_SCAN_VOLUMES "$VOLS"
ok "saved to $DEST/.env (owner-only)"

# ---- mount now + keep mounted ----
step "Mounting NAS volumes and the staging share"
out="$(cd "$DEST" && bash -c '. ./clipstage_env.sh && mount_volumes' 2>&1)"; echo "$out" | sed 's/^/  /'
echo "$out" | grep -q FAILED && warn "Some shares failed to mount - check the logins above (run again with --reconfigure)."
(cd "$DEST" && bash ./install_mountwatch.sh >/dev/null 2>&1) && ok "shares are re-mounted automatically (every 5 minutes)" || warn "Could not install the auto-mount job."

# ---- desktop app ----
if [ "$NO_APP" -eq 0 ] && [ -d "$SRC/clipstage-fcp-drag" ]; then
    step "ClipStage desktop app (drag clips into Final Cut Pro)"
    if [ -d /Applications/ClipStage.app ] && [ "$RECONFIGURE" -eq 0 ]; then
        ok "already installed in /Applications (use --reconfigure to rebuild)"
    else
        for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$b" ] && eval "$("$b" shellenv)"; done
        if ! command -v brew >/dev/null 2>&1; then
            echo "  installing Homebrew (this also installs Apple's command line tools - it can take a while)..."
            NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" \
                || die "Homebrew installation failed. Use the browser instead (http://$HOST:8000) or see https://brew.sh."
            for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$b" ] && eval "$("$b" shellenv)"; done
            command -v brew >/dev/null 2>&1 || die "Homebrew was installed but 'brew' was not found."
        fi
        command -v node >/dev/null 2>&1 || { echo "  installing Node.js..."; brew install node >/dev/null 2>&1 || die "brew install node failed"; }
        BUILD="$(mktemp -d)"; cp -R "$SRC/clipstage-fcp-drag/." "$BUILD/"
        cat > "$BUILD/config.json" <<JSON
{ "serverUrl": "http://$HOST:8000", "stagingMount": "/Volumes/staging", "shareName": "staging" }
JSON
        ARCH="arm64"; [ "$(uname -m)" = "x86_64" ] && ARCH="x64"
        ( cd "$BUILD" && echo "  downloading Electron (about 1-2 minutes)..." && npm install --no-audit --no-fund >/dev/null 2>&1 \
            && npx --yes @electron/packager . ClipStage --platform=darwin --arch="$ARCH" --overwrite >/dev/null 2>&1 ) \
            || die "Building the app failed. You can still use ClipStage in the browser: http://$HOST:8000"
        APP="$(find "$BUILD" -maxdepth 3 -name ClipStage.app -type d | head -1)"
        [ -n "$APP" ] || die "ClipStage.app was not produced."
        rm -rf /Applications/ClipStage.app; cp -R "$APP" /Applications/ || die "Could not copy to /Applications."
        xattr -dr com.apple.quarantine /Applications/ClipStage.app 2>/dev/null
        rm -rf "$BUILD"
        ok "installed /Applications/ClipStage.app"
    fi
fi

printf '\n%s  Done.%s  Open ClipStage from Applications (or browse to http://%s:8000).\n' "$B$G" "$N" "$HOST"
echo "  Final Cut Pro: Settings -> Import -> Files -> \"Leave files in place\" (once)."
echo
