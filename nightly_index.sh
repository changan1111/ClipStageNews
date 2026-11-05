#!/bin/bash
# ClipStage v5.0 - scheduled index (run by launchd, see com.clipstage.indexer.plist /
# install_nightly.sh). Everything is written to indexer.log, including startup failures,
# so "it did not run" always leaves a line to read.
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec >> "$APP_DIR/indexer.log" 2>&1
echo "=== Scheduled index $(date) ==="

. "$APP_DIR/clipstage_env.sh" || { echo "=== FAILED to load environment $(date) ==="; exit 1; }
clipstage_require_key

mount_volumes
start_typesense || { clipstage_alert "Nightly index NOT run - Typesense did not start"; exit 1; }

"$PYTHON" indexer.py --prune
rc=$?

if [ $rc -ne 0 ]; then
    clipstage_alert "Nightly index FAILED (exit $rc) - see indexer.log"
else
    # A prune refused by the safety guard is not an error, but someone should look.
    warn="$("$PYTHON" -c 'import json;print(json.load(open("index_status.json")).get("last_warning",""))' 2>/dev/null)"
    [ -n "$warn" ] && clipstage_alert "Index warning: $warn"

    # New thumbnails, capped per run so the job stays predictable. CLIPSTAGE_THUMBS=0 skips.
    if [ "${CLIPSTAGE_THUMBS:-1}" = "1" ]; then
        "$PYTHON" generate_thumbs.py --limit "${CLIPSTAGE_THUMBS_LIMIT:-5000}" \
            || clipstage_alert "Thumbnail run failed - see indexer.log"
    fi
fi
# Nightly snapshot of the index (for quick rebuild on a new Mac)
"$PYTHON" snapshot_index.py || clipstage_alert "Index snapshot failed - see indexer.log"

# Nightly copy of clipstage.db (notes/details/audit - cannot be rebuilt). See backup_db.py.
"$PYTHON" backup_db.py || clipstage_alert "clipstage.db backup failed - see indexer.log"

echo "=== Finished (exit $rc) $(date) ==="
exit $rc
