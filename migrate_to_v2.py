#!/usr/bin/env python3
"""
One-time migration: old Typesense collection ("clips") -> new "clips_v2".

What it does (the old collection is NOT modified, so you can roll back):
  1. Copies notes + use_count from the old collection into SQLite (clipstage.db).
  2. Creates clips_v2 with the new schema (token_separators, hidden, custom metadata).
  3. Copies every document across (no NAS rescan needed), merging SQLite user data.

Afterwards run a normal `python3 indexer.py --prune` to pick up anything that changed.

    python3 migrate_to_v2.py             # migrate
    python3 migrate_to_v2.py --replace   # drop an existing clips_v2 first and redo it
"""
import json
import os
import sys

import store
from indexer import BATCH_SIZE, COLLECTION, SCHEMA, USER_FIELDS, backend

OLD = os.environ.get("CLIPSTAGE_OLD_COLLECTION", "clips")


def main():
    if backend.name != "typesense":
        sys.exit("migrate_to_v2.py is only for the Typesense engine (old 'clips' collection). "
                 "With SEARCH_BACKEND=sqlite just run indexer.py.")
    client = backend.client
    if OLD == COLLECTION:
        sys.exit(f"Old and new collection are both '{COLLECTION}'. Set CLIPSTAGE_COLLECTION=clips_v2.")
    store.init()

    print(f"Reading old collection '{OLD}' …")
    try:
        raw = client.collections[OLD].documents.export()
    except Exception as e:
        sys.exit(f"Cannot read '{OLD}': {e}")
    old_docs = [json.loads(line) for line in raw.splitlines() if line.strip()]
    print(f"  {len(old_docs):,} documents")

    n = store.import_legacy((d["id"], d.get("path", ""), d.get("notes", ""), d.get("use_count", 0))
                            for d in old_docs)
    print(f"  {n:,} clips with notes/use_count copied into SQLite")

    exists = True
    try:
        client.collections[COLLECTION].retrieve()
    except Exception:
        exists = False
    if exists:
        if "--replace" not in sys.argv:
            sys.exit(f"'{COLLECTION}' already exists. Re-run with --replace to drop and rebuild it.")
        client.collections[COLLECTION].delete()
        print(f"  dropped existing '{COLLECTION}'")
    client.collections.create(SCHEMA)
    print(f"Created '{COLLECTION}' with token_separators {SCHEMA['token_separators']}")

    user = store.all_user_rows()
    out = []
    for d in old_docs:
        d = dict(d)
        d.pop("tags", None)
        u = user.get(d["id"]) or {}
        for k in USER_FIELDS:
            v = u.get(k) or d.get(k) or ""
            if v:
                d[k] = v
            else:
                d.pop(k, None)
        uc = u.get("use_count") or d.get("use_count") or 0
        if uc:
            d["use_count"] = int(uc)
        d["hidden"] = bool(u.get("hidden"))
        out.append(d)

    failed = 0
    for i in range(0, len(out), BATCH_SIZE):
        res = client.collections[COLLECTION].documents.import_(out[i:i + BATCH_SIZE], {"action": "emplace"})
        failed += sum(1 for r in res if not r.get("success"))
        print(f"  imported {min(i + BATCH_SIZE, len(out)):,}/{len(out):,}", end="\r")
    print(f"\nDone. {len(out) - failed:,} documents copied, {failed} failed.")
    if failed:
        sys.exit(1)
    print("Next: start the API (it now searches Typesense directly) and run indexer.py --prune once.")


if __name__ == "__main__":
    main()
