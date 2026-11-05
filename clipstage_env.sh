#!/bin/bash
# ClipStage v5.0 - shared shell setup. SOURCED by start_clipstage.sh, nightly_index.sh,
# manual_index.sh and install_nightly.sh:   . "$(dirname "$0")/clipstage_env.sh"
#
#  * works from wherever the project lives (no hard-coded /Users/... path)
#  * reads KEY=value lines from ./.env SAFELY: values with spaces ("SHARE FOLDER") are fine,
#    nothing in the file is ever executed. A variable already set in the environment wins.
#  * finds python / typesense-server (overrides: CLIPSTAGE_PYTHON, TYPESENSE_BIN)
#  * launchd starts jobs with an EMPTY environment, so every script loads its own settings.

CLIPSTAGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$CLIPSTAGE_DIR"
export CLIPSTAGE_DIR
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
cd "$CLIPSTAGE_DIR" || exit 1

_clipstage_load_env() {
    local f="$1" line key val
    [ -f "$f" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in ''|\#*) continue ;; esac
        case "$line" in *=*) ;; *) continue ;; esac
        key="${line%%=*}"
        val="${line#*=}"
        case "$key" in ''|*[!A-Za-z0-9_]*) continue ;; esac
        val="${val%\"}"; val="${val#\"}"; val="${val%\'}"; val="${val#\'}"
        if [ -z "$(printenv "$key")" ]; then
            export "$key=$val"
        fi
    done < "$f"
}
_clipstage_load_env "$CLIPSTAGE_DIR/.env"

# Python: CLIPSTAGE_PYTHON, else the project venv, else python3 on PATH.
if [ -n "${CLIPSTAGE_PYTHON:-}" ] && [ -x "$CLIPSTAGE_PYTHON" ]; then
    PYTHON="$CLIPSTAGE_PYTHON"
elif [ -x "$CLIPSTAGE_DIR/.venv/bin/python" ]; then
    PYTHON="$CLIPSTAGE_DIR/.venv/bin/python"
else
    PYTHON="$(command -v python3)"
fi
CLIPSTAGE_PYTHON="$PYTHON"

TS_HOST="${TYPESENSE_HOST:-localhost}"
TS_PORT="${TYPESENSE_PORT:-8108}"
TS_DATA_DIR="${TYPESENSE_DATA_DIR:-$HOME/typesense-data}"
if [ -z "${TYPESENSE_BIN:-}" ]; then
    for _c in /opt/homebrew/bin/typesense-server /usr/local/bin/typesense-server \
              /opt/homebrew/Cellar/typesense-server@*/*/bin/typesense-server; do
        if [ -x "$_c" ]; then TYPESENSE_BIN="$_c"; break; fi
    done
    [ -z "${TYPESENSE_BIN:-}" ] && TYPESENSE_BIN="$(command -v typesense-server || true)"
fi
TS_BIN="$TYPESENSE_BIN"

# ---- helpers -----------------------------------------------------------------

# Print a message, POST it to CLIPSTAGE_ALERT_WEBHOOK (Slack/Teams/Discord-style {"text": ...})
# when set, and raise a macOS notification.
clipstage_alert() {
    local msg="${1//\"/\'}"
    echo "ALERT: $msg"
    if [ -n "${CLIPSTAGE_ALERT_WEBHOOK:-}" ]; then
        curl -s -m 15 -X POST -H 'Content-Type: application/json' \
             -d "{\"text\":\"ClipStage: $msg\"}" "$CLIPSTAGE_ALERT_WEBHOOK" >/dev/null 2>&1 || true
    fi
    if command -v osascript >/dev/null 2>&1; then
        osascript -e "display notification \"$msg\" with title \"ClipStage\"" >/dev/null 2>&1 || true
    fi
}

clipstage_require_key() {
    if [ -z "${TYPESENSE_KEY:-}" ]; then
        clipstage_alert "TYPESENSE_KEY is not set - put it in $CLIPSTAGE_DIR/.env"
        exit 1
    fi
}

mount_volumes() {
    if [ -f "$CLIPSTAGE_DIR/mount_volumes.sh" ]; then
        bash "$CLIPSTAGE_DIR/mount_volumes.sh"
        local rc=$?
        sleep 2
        return $rc
    else
        echo "(no mount_volumes.sh - assuming the NAS volumes are already mounted)"
        return 0
    fi
}

typesense_up() {
    curl -sf "http://$TS_HOST:$TS_PORT/health" | grep -q true
}

start_typesense() {
    typesense_up && return 0
    if [ -z "$TS_BIN" ] || [ ! -x "$TS_BIN" ]; then
        echo "ERROR: Typesense is not running and typesense-server was not found." >&2
        echo "       Set TYPESENSE_BIN in .env (e.g. /opt/homebrew/bin/typesense-server)." >&2
        return 1
    fi
    echo "Starting Typesense..."
    mkdir -p "$TS_DATA_DIR"
    # Listen on THIS Mac only. By default Typesense binds to every interface, which would
    # expose it to the whole LAN with the API key as its only protection.
    nohup "$TS_BIN" --data-dir="$TS_DATA_DIR" --api-key="$TYPESENSE_KEY" \
        --listen-address="${TYPESENSE_LISTEN_ADDRESS:-127.0.0.1}" \
        --listen-port="$TS_PORT" > /tmp/typesense.log 2>&1 &
    local i
    for i in $(seq 1 30); do
        typesense_up && return 0
        sleep 1
    done
    echo "ERROR: Typesense did not become healthy within 30s (see /tmp/typesense.log)" >&2
    return 1
}
