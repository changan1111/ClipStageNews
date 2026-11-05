#!/usr/bin/env python3
"""
ClipStage - write a snapshot of the search index (durations included) to backups/clips_<time>.jsonl.

    ./clipstage_run.sh snapshot_index.py

Restore on a new machine (after install + same volume names):
    ./clipstage_run.sh indexer.py --restore backups/clips_YYYYmmdd_HHMMSS.jsonl

Keeps the newest 7 snapshots. Refuses to write an empty snapshot.
"""
import sys

import indexer  # importing does not start a scan (main code is under __main__)

docs = indexer._load_existing_docs()
if not docs:
    print("Index is empty or unreachable - no snapshot written.")
    sys.exit(1)
indexer.backup_existing(docs, keep=7)
