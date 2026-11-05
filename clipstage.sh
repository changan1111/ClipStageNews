#!/bin/bash
# ClipStage - one command for everyday tasks on the middle Mac.
#   ./clipstage.sh status            is everything running?
#   ./clipstage.sh restart           restart the API (and Typesense if needed)
#   ./clipstage.sh stop | start
#   ./clipstage.sh logs [api|index]  follow a log   (default: index)
#   ./clipstage.sh index [VOLUME..]  index now (all volumes, or only those named)
#   ./clipstage.sh stop-index        stop a running index safely (same as the Stop button)
#   ./clipstage.sh thumbs [N]        make up to N new thumbnails (default 2000)
#   ./clipstage.sh backup            copy clipstage.db to backups/db now
#   ./clipstage.sh open              open the app in the browser
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/clipstage_env.sh" || exit 1
PORT="${CLIPSTAGE_API_PORT:-8000}"
DOMAIN="gui/$(id -u)"
cmd="${1:-status}"; shift || true

case "$cmd" in
  status)
    printf 'API (port %s):  ' "$PORT";  curl -sf -m 3 "http://127.0.0.1:$PORT/health" >/dev/null && echo running || echo "NOT running"
    printf 'Typesense:        ';        typesense_up && echo running || echo "NOT running"
    printf 'Nightly job:      ';        launchctl print "$DOMAIN/com.clipstage.indexer" >/dev/null 2>&1 && echo installed || echo "not installed"
    printf 'API service:      ';        launchctl print "$DOMAIN/com.clipstage.api" >/dev/null 2>&1 && echo installed || echo "not installed"
    if [ -f index_status.json ]; then
        "$PYTHON" - <<'PYEOF'
import json, time
s = json.load(open("index_status.json"))
print("Index:            %s%s" % (s.get("state", "?"), (" - " + s["phase"]) if s.get("phase") and s.get("state") == "running" else ""))
if s.get("finished_at"):
    print("Last finished:    %s" % time.strftime("%Y-%m-%d %H:%M", time.localtime(s["finished_at"])))
if s.get("last_warning"):
    print("WARNING:          %s" % s["last_warning"])
PYEOF
    fi
    ;;
  restart|start)
    if launchctl print "$DOMAIN/com.clipstage.api" >/dev/null 2>&1; then
        launchctl kickstart -k "$DOMAIN/com.clipstage.api" && echo "API restarted."
    else
        echo "API service not installed - run: bash install_api_service.sh"; exit 1
    fi ;;
  stop)
    launchctl bootout "$DOMAIN/com.clipstage.api" 2>/dev/null; pkill -f "uvicorn api:app" 2>/dev/null
    echo "API stopped (run ./clipstage.sh start, or bash install_api_service.sh, to bring it back)." ;;
  logs)
    case "${1:-index}" in api) tail -f api.log ;; *) tail -f indexer.log ;; esac ;;
  index)
    exec bash ./manual_index.sh "$@" ;;
  stop-index)
    if "$PYTHON" -c "import json,sys; sys.exit(0 if json.load(open('index_status.json')).get('state') in ('running','stalled') else 1)" 2>/dev/null; then
        date +%s > index_stop.flag
        echo "Stop requested - the index ends at its next checkpoint (within seconds). Nothing is deleted."
    else
        echo "No index is running."
    fi ;;
  thumbs)
    exec "$PYTHON" generate_thumbs.py --limit "${1:-2000}" ;;
  backup)
    exec "$PYTHON" backup_db.py ;;
  open)
    open "http://127.0.0.1:$PORT" ;;
  *)
    sed -n 2,12p "$0"; exit 2 ;;
esac
