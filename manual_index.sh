#!/bin/bash
#
# Manual (on-demand) index run.
#   ./manual_index.sh              full delta index - every configured volume
#   ./manual_index.sh PLAYOUT      index PLAYOUT only (that volume + its subfolders)
#   ./manual_index.sh EDIT2 PLAYOUT   index just those volumes (any number; commas also work)
#
# The scheduled run (nightly_index.sh) is untouched and always indexes everything.
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/clipstage_env.sh" || exit 1
clipstage_require_key

VOLUME="$(IFS=,; echo "$*")"      # all arguments -> "A,B,C"
echo "=== Manual ClipStage index started${VOLUME:+ (volumes: $VOLUME only)}: $(date) ==="
mount_volumes
start_typesense || { clipstage_alert "Typesense did not start - index not run"; exit 1; }

if [ -n "$VOLUME" ]; then
    "$PYTHON" indexer.py --prune --only "$VOLUME"
else
    "$PYTHON" indexer.py --prune
fi
rc=$?
echo "=== Manual ClipStage index finished (exit $rc): $(date) ==="
exit $rc
