#!/bin/bash
# ClipStage - uninstaller (macOS host).
#
#   bash uninstall_clipstage.sh --dry-run     show what would happen, change nothing
#   bash uninstall_clipstage.sh               interactive: asks before every optional step
#
# ALWAYS (after one confirmation):  stops the API, Typesense and the scheduled jobs and removes
#   the three launchd services (com.clipstage.api / .indexer / .mountwatch).
# OPTIONAL (each asked separately, default = No):
#   1. final backup bundle (index snapshot + database + .env + editors + thumbnails)
#   2. remove the "staging" file share        (the staging FOLDER and its files stay)
#   3. remove the firewall entry for ClipStage's Python
#   4. delete the Typesense search-index data
#   5. delete the whole ClipStage folder      (database, .env, thumbnails, backups, .venv)
#
# NEVER touched: NAS volumes and their files, the staging folder contents, Supabase,
#   Homebrew packages (typesense-server, ffmpeg, python - other things may use them),
#   Caddy, and the editors' Macs / PCs.
#
# Run it as your normal user (not with sudo). It asks for your password only for steps 2 and 3.

DRY=0
case "${1:-}" in
    --dry-run|-n) DRY=1 ;;
    "") ;;
    *) echo "Usage: bash uninstall_clipstage.sh [--dry-run]"; exit 2 ;;
esac

say()  { printf '%s\n' "$*"; }
ok()   { printf '  [ok] %s\n' "$*"; }
skip() { printf '  [--] %s\n' "$*"; }
warn() { printf '  [!!] %s\n' "$*"; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# Run a command, or only print it in --dry-run mode.
run() {
    if [ "$DRY" -eq 1 ]; then printf '  (dry-run) %s\n' "$*"; return 0; fi
    "$@"
}

ask_yn() {      # ask_yn "question"  -> 0 = yes, default No
    local a
    printf '%s [y/N] ' "$1"
    read -r a || return 1
    case "$a" in y|Y|yes|YES|Yes) return 0 ;; *) return 1 ;; esac
}

ask_word() {    # ask_word "question" WORD  -> 0 only if the exact word is typed
    local a
    printf '%s ' "$1"
    read -r a || return 1
    [ "$a" = "$2" ]
}

# ---- safety checks --------------------------------------------------------------------------
[ "$(id -u)" -ne 0 ] || die "Do not run this with sudo. Run it as your normal user."
if [ "$DRY" -eq 0 ]; then
    [ "$(uname)" = "Darwin" ] || die "This uninstaller is for macOS."
fi
[ -t 0 ] || die "Run this in Terminal (it asks questions)."

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$DIR/api.py" ] && [ -f "$DIR/clipstage_env.sh" ] \
    || die "This does not look like the ClipStage folder ($DIR). Keep this script inside it."
case "$DIR" in
    /|"$HOME"|"$HOME/"|/Users|/Users/|/Users/Shared|/Applications|/System|/Library) die "Refusing: $DIR is not a ClipStage project folder." ;;
esac

# Read the same settings the services use (paths for Typesense data, database, backups).
. "$DIR/clipstage_env.sh" >/dev/null 2>&1 || true
cd "$DIR" || die "Cannot enter $DIR"
TS_DIR="${TS_DATA_DIR:-${TYPESENSE_DATA_DIR:-$HOME/typesense-data}}"
STAGING_DIR="${CLIPSTAGE_STAGING_PATH:-/Users/Shared/staging}"
THUMB_DIR_RES="${CLIPSTAGE_THUMB_DIR:-$DIR/static/thumbs}"
DB_FILE="${CLIPSTAGE_DB:-$DIR/clipstage.db}"
BACKUP_ROOT="${CLIPSTAGE_BACKUP_DIR:-$DIR/backups/db}"
DOMAIN="gui/$(id -u)"
LABELS="com.clipstage.api com.clipstage.indexer com.clipstage.mountwatch"

say ""
say "ClipStage uninstaller"
[ "$DRY" -eq 1 ] && say "*** DRY RUN: nothing will be changed ***"
say "  Project folder : $DIR"
say "  Typesense data : $TS_DIR"
say "  Staging folder : $STAGING_DIR (its files are never deleted)"
say ""
say "This will stop ClipStage for everyone and remove its background services."
say "If this Mac is being replaced, confirm the NEW machine works first."
say ""
ask_yn "Stop ClipStage and remove its services?" || { say "Nothing changed."; exit 0; }

# ---- 1. optional final backup (BEFORE anything is stopped: the snapshot needs Typesense) ----------
say ""
say "1. Final backup"
BUNDLE=""
if ask_yn "   Save a final backup bundle first? (recommended)"; then
    BUNDLE="$HOME/ClipStage-final-backup-$(date +%Y%m%d-%H%M%S)"
    run mkdir -p "$BUNDLE" || die "Cannot create $BUNDLE"
    if [ -f "$DIR/snapshot_index.py" ] && [ -f "$DIR/clipstage_run.sh" ]; then
        if run bash "$DIR/clipstage_run.sh" snapshot_index.py; then ok "index snapshot written to $DIR/backups"; else warn "index snapshot failed (is Typesense running?) - continuing"; fi
    else
        skip "snapshot_index.py not found - index snapshot skipped"
    fi
    if [ -f "$DB_FILE" ]; then
        for f in "$DB_FILE" "$DB_FILE-wal" "$DB_FILE-shm"; do [ -f "$f" ] && run cp -p "$f" "$BUNDLE/"; done
        ok "database copied"
    else
        skip "no clipstage.db found"
    fi
    [ -f "$DIR/.env" ]           && run cp -p "$DIR/.env" "$BUNDLE/env.backup" && ok ".env copied (as env.backup)"
    [ -f "$DIR/editors.json" ]   && run cp -p "$DIR/editors.json" "$BUNDLE/" && ok "editors.json copied"
    [ -d "$DIR/backups" ]        && run cp -Rp "$DIR/backups" "$BUNDLE/backups" && ok "backups folder copied"
    if [ -d "$THUMB_DIR_RES" ]; then
        if ask_yn "   Also copy the thumbnails folder? (can be large)"; then
            run cp -Rp "$THUMB_DIR_RES" "$BUNDLE/thumbs" && ok "thumbnails copied"
        else
            skip "thumbnails not copied"
        fi
    fi
    say "   Bundle: $BUNDLE"
    say "   Copy it off this Mac (NAS or USB disk) before deleting anything."
else
    skip "no final backup"
fi

# ---- 2. stop and remove the services (always) -----------------------------------------------------
say ""
say "2. Stopping and removing services"
for label in $LABELS; do
    plist="$HOME/Library/LaunchAgents/$label.plist"
    run launchctl bootout "$DOMAIN/$label" 2>/dev/null
    if [ -f "$plist" ]; then run rm -f "$plist" && ok "removed $label"; else skip "$label was not installed"; fi
done
run pkill -f "uvicorn api:app" 2>/dev/null
run pkill -f "indexer.py" 2>/dev/null
run pkill -f "generate_thumbs.py" 2>/dev/null
run pkill -f "typesense-server" 2>/dev/null
ok "API, indexer, thumbnail job and Typesense stopped"
if [ "$DRY" -eq 0 ]; then
    sleep 2
    left="$(launchctl list 2>/dev/null | grep -c clipstage)"
    if [ "${left:-0}" -eq 0 ]; then ok "launchctl shows no ClipStage services"; else warn "some ClipStage entries still listed: launchctl list | grep clipstage"; fi
fi

# ---- 3. staging file share ------------------------------------------------------------------------
say ""
say "3. Staging file share (smb://.../staging)"
if ask_yn "   Remove the share? (the folder $STAGING_DIR and its files stay)"; then
    if sudo -v; then
        if sudo sharing -l 2>/dev/null | grep -q "$STAGING_DIR"; then
            [ "$DRY" -eq 1 ] && say "  (dry-run) sudo sharing -r staging" || { sudo sharing -r "${CLIPSTAGE_STAGING_SHARE:-staging}" >/dev/null 2>&1 && ok "share removed" || warn "could not remove it - System Settings > General > Sharing > File Sharing"; }
        else
            skip "no share found for $STAGING_DIR"
        fi
    else
        warn "no administrator access - skipped"
    fi
else
    skip "share left in place"
fi

# ---- 4. firewall entry -----------------------------------------------------------------------------
say ""
say "4. Firewall entry for ClipStage's Python"
if ask_yn "   Remove it?"; then
    FW=/usr/libexec/ApplicationFirewall/socketfilterfw
    PYREAL=""
    [ -x "$DIR/.venv/bin/python" ] && PYREAL="$("$DIR/.venv/bin/python" -c 'import os,sys;print(os.path.realpath(sys.executable))' 2>/dev/null)"
    if [ -n "$PYREAL" ] && sudo -v; then
        [ "$DRY" -eq 1 ] && say "  (dry-run) sudo $FW --remove $PYREAL" || { sudo "$FW" --remove "$PYREAL" >/dev/null 2>&1 && ok "firewall entry removed"; }
    else
        skip "nothing to remove (no .venv python found, or no administrator access)"
    fi
else
    skip "firewall left as is"
fi

# ---- 5. Typesense data -----------------------------------------------------------------------------
say ""
say "5. Search index data (Typesense)"
if [ -d "$TS_DIR" ]; then
    sz="$(du -sh "$TS_DIR" 2>/dev/null | cut -f1)"
    say "   $TS_DIR  ($sz). It can be rebuilt from the NAS by a full scan (about 3 hours) or restored from a snapshot."
    case "$TS_DIR" in
        /|"$HOME"|"$HOME/"|"") warn "refusing to delete $TS_DIR" ;;
        *) if ask_word "   Type DELETE to remove it, anything else keeps it:" DELETE; then run rm -rf "$TS_DIR" && ok "Typesense data deleted"; else skip "Typesense data kept"; fi ;;
    esac
else
    skip "no Typesense data folder at $TS_DIR"
fi

# ---- 6. the project folder -------------------------------------------------------------------------
say ""
say "6. ClipStage folder  ($DIR)"
say "   Contains clipstage.db (notes, details, audit), .env (keys), thumbnails, local backups and .venv."
[ -z "$BUNDLE" ] && warn "you did not make a final backup bundle in this run"
DELETE_DIR=0
if ask_word "   Type DELETE-ALL to delete this whole folder, anything else keeps it:" DELETE-ALL; then
    DELETE_DIR=1
else
    skip "folder kept"
fi

# ---- summary ----------------------------------------------------------------------------------------
say ""
say "Left in place on purpose:"
say "  - NAS volumes (eject in Finder if you want them gone) and everything on them"
say "  - $STAGING_DIR and its files"
say "  - Homebrew packages. To remove them yourself:  brew uninstall ffmpeg   and   brew list | grep typesense"
say "  - Caddy, Supabase, and the editors' machines (their installs point at this host)"
[ -n "$BUNDLE" ] && say "  - Final backup bundle: $BUNDLE"
say ""

if [ "$DELETE_DIR" -eq 1 ]; then
    say "Deleting $DIR ..."
    cd "$HOME" || cd /
    run rm -rf "$DIR" && ok "ClipStage folder deleted"
fi

say "Done."
exit 0
