#!/bin/bash
# Run any ClipStage Python tool with .env loaded (the Python scripts do not read .env themselves):
#   ./clipstage_run.sh indexer.py --dry
#   ./clipstage_run.sh migrate_to_v2.py
#   ./clipstage_run.sh generate_thumbs.py --limit 2000
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/clipstage_env.sh" || exit 1
exec "$PYTHON" "$@"
