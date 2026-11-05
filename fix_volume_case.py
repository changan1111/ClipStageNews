#!/usr/bin/env python3
"""
ClipStage - merge duplicate clips whose volume name differs only by upper/lower case
(e.g. "playout" vs "PLAYOUT"). The clip id is md5(path), so the same file indexed under
/Volumes/playout/... and /Volumes/PLAYOUT/... became two clips.

What it does, per wrong-case clip:
  1. moves its notes / tags / description / reporter / location / use_count / hidden to
     the clip under the correct spelling (merged, nothing overwritten, nothing lost)
  2. copies its thumbnail if the correct one is missing
  3. deletes the wrong-case clip from the search index

Run:   ./clipstage_run.sh fix_volume_case.py            (dry run - changes nothing)
       ./clipstage_run.sh fix_volume_case.py --apply    (does it; backs up first)
Then run the indexer once (Sync Index button) so the correct-case clips are present and
their notes are pushed into the search index.
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path

import store
from search_backend import get_backend
from clipstage_config import BASE_DIR, COLLECTION, THUMB_DIR, VOLUMES_ROOT, clip_uid, norm_path

APPLY = "--apply" in sys.argv
USER_TEXT = ("notes", "tags_custom", "description", "reporter", "location")


def mounted_names() -> dict:
    return {p.name.casefold(): p.name for p in VOLUMES_ROOT.iterdir() if p.is_dir()}


def plan(docs: dict, canon: dict):
    """-> [(old_doc, new_path, new_uid)] for docs under a wrong-case volume spelling."""
    out, odd = [], 0
    for d in docs.values():
        vol = d.get("volume") or ""
        good = canon.get(vol.casefold())
        if not good or vol == good:
            continue
        old_prefix = norm_path(VOLUMES_ROOT / vol) + "/"
        path = d.get("path", "")
        if not path.startswith(old_prefix):
            odd += 1
            continue
        new_path = norm_path(VOLUMES_ROOT / good) + "/" + path[len(old_prefix):]
        out.append((d, new_path, clip_uid(new_path)))
    return out, odd


def merge_user_data(old_id: str, new_id: str, new_path: str, rows: dict) -> str:
    """Copy old clip's SQLite user data onto new clip. Returns 'none'|'moved'|'merged'."""
    o = rows.get(old_id)
    if not o:
        return "none"
    has_data = o["use_count"] or o["hidden"] or any(o[k] for k in USER_TEXT)
    if not has_data:
        return "none"
    c = store._conn()
    n = rows.get(new_id)
    with c:
        if not n:
            c.execute("INSERT INTO clip_meta (clip_id, path, notes, use_count, hidden, tags_custom,"
                      " description, reporter, location, meta_updated_by, meta_updated_at, version)"
                      " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (new_id, new_path, o["notes"], o["use_count"], o["hidden"], o["tags_custom"],
                       o["description"], o["reporter"], o["location"], o["meta_updated_by"],
                       o["meta_updated_at"], o["version"] + 1))
            return "moved"
        vals = {}
        for k in USER_TEXT:
            a, b = (n[k] or "").strip(), (o[k] or "").strip()
            vals[k] = a if (not b or b == a) else (b if not a else f"{a} / {b}")
        c.execute("UPDATE clip_meta SET notes=?, tags_custom=?, description=?, reporter=?, location=?,"
                  " use_count=?, hidden=?, version=version+1 WHERE clip_id=?",
                  (vals["notes"], vals["tags_custom"], vals["description"], vals["reporter"],
                   vals["location"], n["use_count"] + o["use_count"],
                   1 if (n["hidden"] or o["hidden"]) else 0, new_id))
        return "merged"


def main():
    backend = get_backend(120)          # Typesense on Mac, SQLite FTS5 on Windows (SEARCH_BACKEND)
    docs = backend.export_all()
    raw = "\n".join(json.dumps(d, ensure_ascii=False) for d in docs.values())
    canon = mounted_names()
    todo, odd = plan(docs, canon)
    dup = sum(1 for _, _, nid in todo if nid in docs)
    print(f"Clips in index: {len(docs):,}")
    print(f"Wrong-case volume spelling: {len(todo):,}  "
          f"(already also present under the right spelling: {dup:,}, not yet indexed: {len(todo) - dup:,})")
    if odd:
        print(f"Skipped {odd:,} clips whose path does not start with their volume (left alone).")
    by_vol = {}
    for d, _, _ in todo:
        by_vol[d["volume"]] = by_vol.get(d["volume"], 0) + 1
    for v, n in sorted(by_vol.items()):
        print(f"   {v!r:20} -> {canon[v.casefold()]!r:16} {n:,} clips")
    if not todo:
        return
    store.init()
    rows = store.all_user_rows()
    with_data = sum(1 for d, _, _ in todo if d["id"] in rows)
    print(f"Of these, {with_data:,} have a row in the notes database (will be moved/merged, not lost).")
    if not APPLY:
        print("\nDRY RUN - nothing changed. Re-run with --apply to do it.")
        return

    stamp = time.strftime("%Y%m%d_%H%M%S")
    bdir = BASE_DIR / "backups"
    bdir.mkdir(exist_ok=True)
    bk = bdir / f"clips_{stamp}_before_case_fix.jsonl"
    bk.write_text(raw)
    if store.DB_PATH.exists():
        shutil.copy2(store.DB_PATH, bdir / f"clipstage_{stamp}_before_case_fix.db")
    print(f"Backup written: {bk}  (+ copy of clipstage.db)")

    res = {"none": 0, "moved": 0, "merged": 0}
    thumbs = 0
    for d, new_path, new_id in todo:
        res[merge_user_data(d["id"], new_id, new_path, rows)] += 1
        old_t, new_t = Path(THUMB_DIR) / (d["id"] + ".jpg"), Path(THUMB_DIR) / (new_id + ".jpg")
        if old_t.exists() and not new_t.exists():
            shutil.copy2(old_t, new_t)
            thumbs += 1
    # data is safely on the new ids -> drop the old rows, then the old Typesense docs
    c = store._conn()
    with c:
        for d, _, _ in todo:
            c.execute("DELETE FROM clip_meta WHERE clip_id=?", (d["id"],))
    ids = [d["id"] for d, _, _ in todo]
    for i in range(0, len(ids), 100):
        backend.delete_ids(ids[i:i + 100])
    print(f"Done. Removed {len(ids):,} wrong-case clips. User data: {res['moved']} moved, "
          f"{res['merged']} merged, {res['none']} had none. Thumbnails copied: {thumbs}.")
    print("Now run Sync Index (or ./manual_index.sh) once.")


if __name__ == "__main__":
    main()
