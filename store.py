"""
ClipStage — small SQLite store for USER data.

Typesense is the SEARCH index only (it can be wiped and rebuilt any time).
Everything people type or do lives here, so a reindex can never lose it:

  clip_meta   notes, use_count, hidden (soft-delete), custom metadata
  locks       "who is editing / using this clip" (auto-expire)
  audit_log   who staged / deleted / restored / started the indexer

The indexer reads this file to merge notes + use_count back into the search
documents it writes; the API writes here first and mirrors to Typesense.
"""
import os
import sqlite3
import threading
import time
from pathlib import Path

import clipstage_dotenv  # noqa: F401,E402  (loads .env so CLIPSTAGE_DB is honoured by every tool)

DB_PATH = Path(os.environ.get("CLIPSTAGE_DB", Path(__file__).parent / "clipstage.db"))
LOCK_TTL_SECONDS = 600

_local = threading.local()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clip_meta (
    clip_id         TEXT PRIMARY KEY,
    path            TEXT NOT NULL DEFAULT '',
    notes           TEXT NOT NULL DEFAULT '',
    use_count       INTEGER NOT NULL DEFAULT 0,
    hidden          INTEGER NOT NULL DEFAULT 0,
    tags_custom     TEXT NOT NULL DEFAULT '',
    description     TEXT NOT NULL DEFAULT '',
    reporter        TEXT NOT NULL DEFAULT '',
    location        TEXT NOT NULL DEFAULT '',
    meta_updated_by TEXT NOT NULL DEFAULT '',
    meta_updated_at TEXT NOT NULL DEFAULT '',
    version         INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS locks (
    clip_id   TEXT NOT NULL,
    editor    TEXT NOT NULL,
    path      TEXT NOT NULL DEFAULT '',
    since     REAL NOT NULL,
    expires   REAL NOT NULL,
    PRIMARY KEY (clip_id, editor)
);
CREATE TABLE IF NOT EXISTS audit_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    actor   TEXT NOT NULL,
    action  TEXT NOT NULL,
    detail  TEXT NOT NULL DEFAULT ''
);
"""

META_FIELDS = ("notes", "tags_custom", "description", "reporter", "location")


def _conn() -> sqlite3.Connection:
    c = getattr(_local, "c", None)
    if c is None:
        c = sqlite3.connect(str(DB_PATH), timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.executescript(_SCHEMA)
        _local.c = c
    return c


def init():
    _conn()


# ── clip metadata ────────────────────────────────────────────────────────────

def get_many(clip_ids) -> dict:
    """{clip_id: row-dict} for the ids that have any stored user data."""
    ids = list(clip_ids)
    out = {}
    c = _conn()
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = ",".join("?" * len(chunk))
        for r in c.execute(f"SELECT * FROM clip_meta WHERE clip_id IN ({q})", chunk):
            out[r["clip_id"]] = dict(r)
    return out


def all_user_rows() -> dict:
    """Every row (used by the indexer: one query instead of one per clip)."""
    return {r["clip_id"]: dict(r) for r in _conn().execute("SELECT * FROM clip_meta")}


def _ensure(c, clip_id: str, path: str = ""):
    c.execute("INSERT OR IGNORE INTO clip_meta (clip_id, path) VALUES (?, ?)", (clip_id, path))
    if path:
        c.execute("UPDATE clip_meta SET path=? WHERE clip_id=? AND path=''", (path, clip_id))


def set_notes(clip_id: str, notes: str, path: str = "", expected_version=None):
    """Returns the new version, or None if expected_version did not match."""
    c = _conn()
    with c:
        _ensure(c, clip_id, path)
        if expected_version is not None:
            cur = c.execute("SELECT version FROM clip_meta WHERE clip_id=?", (clip_id,)).fetchone()
            if cur and cur["version"] != expected_version:
                return None
        c.execute("UPDATE clip_meta SET notes=?, version=version+1 WHERE clip_id=?", (notes, clip_id))
        return c.execute("SELECT version FROM clip_meta WHERE clip_id=?", (clip_id,)).fetchone()["version"]


def set_notes_bulk(clip_ids, notes: str):
    c = _conn()
    with c:
        for cid in clip_ids:
            _ensure(c, cid)
            c.execute("UPDATE clip_meta SET notes=?, version=version+1 WHERE clip_id=?", (notes, cid))


def set_meta(clip_id: str, values: dict, actor: str, path: str = ""):
    c = _conn()
    stamp = time.strftime("%Y-%m-%d %H:%M")
    with c:
        _ensure(c, clip_id, path)
        for k in ("tags_custom", "description", "reporter", "location"):
            if k in values:
                c.execute(f"UPDATE clip_meta SET {k}=? WHERE clip_id=?", (values[k], clip_id))
        c.execute("UPDATE clip_meta SET meta_updated_by=?, meta_updated_at=?, version=version+1 "
                  "WHERE clip_id=?", (actor, stamp, clip_id))


def bump_use_count(clip_id: str, path: str = "") -> int:
    c = _conn()
    with c:
        _ensure(c, clip_id, path)
        c.execute("UPDATE clip_meta SET use_count=use_count+1 WHERE clip_id=?", (clip_id,))
        return c.execute("SELECT use_count FROM clip_meta WHERE clip_id=?", (clip_id,)).fetchone()[0]


def set_hidden(clip_id: str, hidden: bool, path: str = ""):
    c = _conn()
    with c:
        _ensure(c, clip_id, path)
        c.execute("UPDATE clip_meta SET hidden=? WHERE clip_id=?", (1 if hidden else 0, clip_id))


def import_legacy(rows):
    """One-time migration: rows of (clip_id, path, notes, use_count). Never overwrites
    data that already exists in SQLite."""
    c = _conn()
    n = 0
    with c:
        for cid, path, notes, use_count in rows:
            if not (notes or use_count):
                continue
            c.execute("INSERT OR IGNORE INTO clip_meta (clip_id, path, notes, use_count) "
                      "VALUES (?, ?, ?, ?)", (cid, path or "", notes or "", int(use_count or 0)))
            n += 1
    return n


# ── locks (auto-expiring "in use" markers) ───────────────────────────────────

def _purge_locks(c):
    c.execute("DELETE FROM locks WHERE expires < ?", (time.time(),))


def acquire_lock(clip_id: str, editor: str, path: str = ""):
    """Returns the OTHER editor currently holding the clip (dict) or None."""
    c = _conn()
    now = time.time()
    with c:
        _purge_locks(c)
        other = c.execute("SELECT editor, since FROM locks WHERE clip_id=? AND editor<>? "
                          "ORDER BY since LIMIT 1", (clip_id, editor)).fetchone()
        c.execute("INSERT INTO locks (clip_id, editor, path, since, expires) VALUES (?,?,?,?,?) "
                  "ON CONFLICT(clip_id, editor) DO UPDATE SET expires=excluded.expires",
                  (clip_id, editor, path, now, now + LOCK_TTL_SECONDS))
    return dict(other) if other else None


def release_lock(clip_id: str, editor: str):
    c = _conn()
    with c:
        c.execute("DELETE FROM locks WHERE clip_id=? AND editor=?", (clip_id, editor))


def conflicts(clip_ids, editor: str):
    ids = list(clip_ids)
    if not ids:
        return []
    c = _conn()
    with c:
        _purge_locks(c)
    q = ",".join("?" * len(ids))
    rows = c.execute(f"SELECT clip_id, editor, path FROM locks WHERE editor<>? AND clip_id IN ({q})",
                     [editor, *ids]).fetchall()
    return [dict(r) for r in rows]


# ── audit ────────────────────────────────────────────────────────────────────

def audit(actor: str, action: str, detail: str = ""):
    try:
        c = _conn()
        with c:
            c.execute("INSERT INTO audit_log (ts, actor, action, detail) VALUES (?,?,?,?)",
                      (time.time(), actor or "?", action, detail[:1000]))
    except Exception:
        pass    # auditing must never break the request
