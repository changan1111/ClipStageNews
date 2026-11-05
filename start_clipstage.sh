#!/bin/bash
# ClipStage v5.0 - one-click launcher.  Starts: mount -> Typesense -> API (uvicorn).
# Indexing is NOT done here: it runs on the schedule (nightly_index.sh) and from the
# "Sync Index" button in the UI.
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/clipstage_env.sh" || exit 1
clipstage_require_key

echo "-> Mounting NAS volumes..."
mount_volumes

echo "-> Starting Typesense (if not already running)..."
start_typesense || { clipstage_alert "Typesense is not running - API not started"; exit 1; }

BIND="${CLIPSTAGE_BIND_HOST:-127.0.0.1}"
PORT="${CLIPSTAGE_API_PORT:-8000}"
case "$BIND" in
    127.*|localhost|::1) ;;
    *) echo "WARNING: API listening on $BIND:$PORT. Without an HTTPS proxy, passwords and login"
       echo "         cookies cross the network unencrypted (see README-RUNNING-MACHINE.md)." ;;
esac

echo "-> Starting ClipStage API on $BIND:$PORT ..."
pkill -f "uvicorn api:app" 2>/dev/null
# ONE worker only: edit locks and rate limits are kept in memory.
exec "$PYTHON" -m uvicorn api:app --host "$BIND" --port "$PORT" \
     --proxy-headers --forwarded-allow-ips "${CLIPSTAGE_FORWARDED_ALLOW_IPS:-127.0.0.1}"
