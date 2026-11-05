#!/usr/bin/env python3
"""
ClipStage - back up clipstage.db (notes, details, use counts, audit log) and editors.json.

    python backup_db.py            writes  <backup dir>/clipstage-YYYY-MM-DD.db  (+ editors-YYYY-MM-DD.json)

Uses SQLite's online-backup API, so it is safe while the API is running. Keeps the newest
CLIPSTAGE_BACKUP_KEEP (default 14) days. Backup folder: CLIPSTAGE_BACKUP_DIR (default ./backups/db).
Point CLIPSTAGE_BACKUP_DIR at another disk or a NAS share for a real off-machine copy.
The nightly job runs this automatically.
"""
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import clipstage_dotenv  # noqa: F401  (loads .env)
import store
from clipstage_config import BASE_DIR, env_int


def main() -> int:
    dest = Path(os.environ.get("CLIPSTAGE_BACKUP_DIR") or (BASE_DIR / "backups" / "db"))
    keep = max(1, env_int("CLIPSTAGE_BACKUP_KEEP", 14))
    dest.mkdir(parents=True, exist_ok=True)
    src = Path(store.DB_PATH)
    if not src.exists():
        print(f"No database yet at {src} - nothing to back up.")
        return 0
    day = time.strftime("%Y-%m-%d")
    target = dest / f"clipstage-{day}.db"
    tmp = dest / f".clipstage-{day}.db.tmp"
    tmp.unlink(missing_ok=True)
    con_src = sqlite3.connect(str(src))
    con_dst = sqlite3.connect(str(tmp))
    try:
        con_src.backup(con_dst)
    finally:
        con_dst.close()
        con_src.close()
    os.replace(tmp, target)
    editors = BASE_DIR / "editors.json"
    if editors.exists():
        shutil.copyfile(editors, dest / f"editors-{day}.json")
    for pattern in ("clipstage-*.db", "editors-*.json"):
        for old in sorted(dest.glob(pattern))[:-keep]:
            old.unlink(missing_ok=True)
    print(f"Backup written: {target} ({target.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
