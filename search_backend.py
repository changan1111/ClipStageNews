"""
ClipStage - search backends.

ONE code base, two interchangeable search engines.  Pick with SEARCH_BACKEND in .env:

    SEARCH_BACKEND=typesense   (default on Mac / Linux)   needs the Typesense server
    SEARCH_BACKEND=sqlite      (default on Windows)       nothing to install, no WSL, no service

Both expose exactly the same methods and return exactly the same shapes, so api.py,
indexer.py, generate_thumbs.py and the web UI (search results, facets, highlighting,
sorting, notes, drag-and-drop) behave identically on every machine.

SQLite backend: a separate file (clipstage_search.db, override CLIPSTAGE_SEARCH_DB) holding a
plain `docs` table + an FTS5 index.  Like Typesense it is only a SEARCH index: it can be deleted
and rebuilt by the indexer at any time.  Notes / use counts / hidden flags stay in store.py.
"""
import html as _html
import json
import math
import os
import re
import sqlite3
import threading
import unicodedata
from pathlib import Path

import clipstage_dotenv  # noqa: F401  (loads .env first)
from clipstage_config import BASE_DIR, COLLECTION, IS_WINDOWS

# ── shared constants ──────────────────────────────────────────────────────────

TOKEN_SEPARATORS = ["_", "-", ".", "(", ")", "[", "]", "+"]

SCHEMA = {
    "name": COLLECTION,
    "token_separators": TOKEN_SEPARATORS,
    "fields": [
        {"name": "id",          "type": "string"},
        {"name": "filename",    "type": "string"},
        {"name": "path",        "type": "string", "index": False},
        {"name": "size_mb",     "type": "float"},
        {"name": "date",        "type": "string", "sort": True},
        {"name": "category",    "type": "string", "facet": True},
        {"name": "volume",      "type": "string", "facet": True, "optional": True},
        {"name": "folder",      "type": "string", "facet": True, "optional": True},
        {"name": "ext",         "type": "string", "facet": True},
        {"name": "duration",    "type": "string", "optional": True},
        {"name": "probed",      "type": "bool",   "optional": True},
        # user data: SOURCE OF TRUTH is SQLite (store.py); mirrored here so it is searchable
        {"name": "notes",       "type": "string", "optional": True},
        {"name": "tags_custom", "type": "string", "optional": True},
        {"name": "description", "type": "string", "optional": True},
        {"name": "reporter",    "type": "string", "optional": True},
        {"name": "location",    "type": "string", "optional": True},
        {"name": "use_count",   "type": "int32",  "optional": True},
        {"name": "hidden",      "type": "bool",   "optional": True},
    ],
    "default_sorting_field": "size_mb",
}

USER_FIELDS = ("notes", "tags_custom", "description", "reporter", "location")

# searched fields, in order, with their weights (same numbers the Typesense setup always used)
SEARCH_FIELDS = ("filename", "folder", "volume", "notes", "tags_custom", "description", "reporter", "location")
SEARCH_WEIGHTS = (10, 4, 4, 4, 4, 4, 3, 3)
FACET_FIELDS = ("volume", "folder", "ext")
FACET_MAX_VALUES = 50

HL_START, HL_END = "\x01", "\x02"

# "best match": relevance first, but scores are grouped into RANK_BUCKETS equal-size bands so
# clips that match about equally are then ordered by POPULARITY (use_count) and then newest.
RANK_BUCKETS = int(os.environ.get("CLIPSTAGE_RANK_BUCKETS", "5"))

SORT_KEYS = ("", "best", "relevance", "size_asc", "size_desc", "newest", "oldest", "most_used")


class SearchUnavailable(Exception):
    """The search engine could not be reached / opened."""


# ══════════════════════════════════════════════════════════════════════════════
#  TYPESENSE  (Mac / Linux default) - behaviour unchanged from earlier versions
# ══════════════════════════════════════════════════════════════════════════════

class TypesenseBackend:
    name = "typesense"

    def __init__(self, timeout: int = 30):
        import typesense                                   # only needed for this backend
        from typesense.exceptions import ObjectNotFound
        self._ObjectNotFound = ObjectNotFound
        key = os.environ.get("TYPESENSE_KEY")
        if not key:
            raise RuntimeError("TYPESENSE_KEY environment variable is required "
                               "(or set SEARCH_BACKEND=sqlite)")
        self.client = typesense.Client({
            "nodes": [{"host": os.environ.get("TYPESENSE_HOST", "localhost"),
                       "port": os.environ.get("TYPESENSE_PORT", "8108"), "protocol": "http"}],
            "api_key": key,
            "connection_timeout_seconds": timeout,
        })
        tm = f"_text_match(buckets: {RANK_BUCKETS})" if RANK_BUCKETS > 0 else "_text_match"
        self._sorts = {
            "": f"{tm}:desc,use_count:desc,date:desc",
            "best": f"{tm}:desc,use_count:desc,date:desc",
            "relevance": "_text_match:desc,date:desc",
            "size_asc": "size_mb:asc", "size_desc": "size_mb:desc",
            "newest": "date:desc", "oldest": "date:asc",
            "most_used": "use_count:desc,date:desc",
        }

    def _docs(self):
        return self.client.collections[COLLECTION].documents

    # -- collection ---------------------------------------------------------
    def setup(self, force: bool = False):
        try:
            self.client.collections[COLLECTION].retrieve()
            exists = True
        except Exception:
            exists = False
        if not exists:
            self.client.collections.create(SCHEMA)
            print("Collection created.")
            return
        if force:
            self.client.collections[COLLECTION].delete()
            self.client.collections.create(SCHEMA)
            print("Collection wiped and recreated (notes / use_count / durations carried over).")
        else:
            print("Collection exists — updating new/changed files only (notes + use_count preserved).")
            self._check_separators()
            self._ensure_new_fields()

    def _check_separators(self):
        try:
            have = self.client.collections[COLLECTION].retrieve().get("token_separators") or []
            if set(have) != set(TOKEN_SEPARATORS):
                print("WARNING: collection '%s' was created without the expected token_separators.\n"
                      "         Word-by-word search (LAUNCH inside AMMA_LAUNCH.mxf) will not work.\n"
                      "         Run:  python3 migrate_to_v2.py   (or indexer.py --force) to rebuild." % COLLECTION)
        except Exception as e:
            print(f"Separator check skipped: {e}")

    def _ensure_new_fields(self):
        try:
            schema = self.client.collections[COLLECTION].retrieve()
            existing = {f["name"] for f in schema.get("fields", [])}
            missing = [f for f in SCHEMA["fields"] if f["name"] not in existing and f.get("optional")]
            if missing:
                self.client.collections[COLLECTION].update({"fields": missing})
                print("Schema updated — added: " + ", ".join(f["name"] for f in missing))
        except Exception as e:
            print(f"Schema check skipped: {e}")

    def startup_check(self):
        try:
            schema = self.client.collections[COLLECTION].retrieve()
        except Exception:
            print(f"[startup] WARNING: collection '{COLLECTION}' not found. Run migrate_to_v2.py "
                  f"(existing install) or indexer.py (fresh install).", flush=True)
            return
        if "_" not in set(schema.get("token_separators") or []):
            print(f"[startup] WARNING: '{COLLECTION}' has no token_separators — searching for LAUNCH inside "
                  f"AMMA_LAUNCH.mxf will fail. Rebuild with migrate_to_v2.py.", flush=True)

    def info(self) -> dict:
        i = self.client.collections[COLLECTION].retrieve()
        return {"documents": i.get("num_documents", 0), "token_separators": i.get("token_separators", []),
                "engine": "typesense"}

    # -- documents ----------------------------------------------------------
    def get(self, uid: str):
        try:
            return self.client.collections[COLLECTION].documents[uid].retrieve()
        except self._ObjectNotFound:
            return None

    def get_many(self, uids) -> dict:
        out, uids = {}, list(uids)
        for i in range(0, len(uids), 200):
            chunk = uids[i:i + 200]
            res = self._docs().search({"q": "*", "query_by": "filename",
                                       "filter_by": "id:[" + ",".join(chunk) + "]", "per_page": 250})
            for h in res.get("hits", []):
                out[h["document"]["id"]] = h["document"]
        return out

    def update(self, uid: str, fields: dict):
        self.client.collections[COLLECTION].documents[uid].update(fields)

    def update_many(self, patches: list):
        for i in range(0, len(patches), 200):
            self._docs().import_(patches[i:i + 200], {"action": "update"})

    def upsert(self, docs: list, action: str = "emplace") -> int:
        """action: upsert (replace) | emplace (create or merge) | update (merge existing only).
        Returns how many documents failed."""
        res = self._docs().import_(docs, {"action": action})
        return sum(1 for r in res if not r.get("success"))

    def delete_ids(self, ids: list):
        ids = list(ids)
        try:
            self._docs().delete({"filter_by": "id:[" + ",".join(ids) + "]"})
        except Exception:
            for uid in ids:
                try:
                    self.client.collections[COLLECTION].documents[uid].delete()
                except Exception:
                    pass

    def export_all(self) -> dict:
        raw = self._docs().export()
        out = {}
        for line in raw.splitlines():
            line = line.strip()
            if line:
                d = json.loads(line)
                out[d["id"]] = d
        return out

    def list_hidden(self, limit: int = 250):
        res = self._docs().search({"q": "*", "query_by": "filename", "filter_by": "hidden:=true",
                                   "per_page": limit})
        return ([{"id": h["document"]["id"], "filename": h["document"].get("filename", ""),
                  "path": h["document"].get("path", "")} for h in res.get("hits", [])],
                res.get("found", 0))

    # -- search -------------------------------------------------------------
    @staticmethod
    def _bt(value: str) -> str:
        if "`" in value or len(value) > 200:
            raise ValueError("Invalid filter value")
        return f"`{value}`"

    def _search(self, params: dict) -> dict:
        """Typesense search; if an older server rejects the bucketed sort, retry without buckets."""
        try:
            return self._docs().search(params)
        except Exception:
            sb = params.get("sort_by", "")
            if "(buckets:" not in sb:
                raise
            plain = re.sub(r"\(buckets:\s*\d+\)", "", sb)
            return self._docs().search({**params, "sort_by": plain})

    @staticmethod
    def _facets(res: dict) -> dict:
        out = {}
        for fc in res.get("facet_counts") or []:
            name = fc.get("field_name")
            if name:
                out[name] = [{"value": c.get("value", ""), "count": int(c.get("count", 0))}
                             for c in fc.get("counts", []) if c.get("value")]
        return out

    @staticmethod
    def _hl(hit: dict):
        val = None
        h = hit.get("highlight")
        if isinstance(h, dict) and isinstance(h.get("filename"), dict):
            val = h["filename"].get("value") or h["filename"].get("snippet")
        if val is None:
            for x in hit.get("highlights") or []:
                if x.get("field") == "filename":
                    val = x.get("value") or x.get("snippet")
        return val if (val and HL_START in val) else None

    def search(self, q, *, volume="", folder="", ext="", sort="", page=1, per_page=30,
               want_all=True, max_results=2000, num_typos=0) -> dict:
        filters = ["hidden:=false"]
        if volume:
            filters.append(f"volume:={self._bt(volume)}")
        if folder:
            filters.append(f"folder:={self._bt(folder)}")
        if ext:
            filters.append(f"ext:={self._bt(ext.upper())}")
        base = {
            "q": q, "query_by": ",".join(SEARCH_FIELDS), "query_by_weights": ",".join(map(str, SEARCH_WEIGHTS)),
            "prefix": True, "num_typos": num_typos, "drop_tokens_threshold": 0,
            "filter_by": " && ".join(filters), "sort_by": self._sorts.get(sort, self._sorts[""]),
            "highlight_fields": "filename", "highlight_full_fields": "filename",
            "highlight_start_tag": HL_START, "highlight_end_tag": HL_END,
        }
        facet_by = ",".join(FACET_FIELDS)
        raw, total, truncated, facets = [], 0, False, {}
        if want_all:
            p = 1
            while True:
                extra = {"facet_by": facet_by, "max_facet_values": FACET_MAX_VALUES} if p == 1 else {}
                res = self._search({**base, **extra, "per_page": 250, "page": p})
                if p == 1:
                    facets = self._facets(res)
                total = res.get("found", 0)
                raw += res.get("hits", [])
                if len(raw) >= total or not res.get("hits"):
                    break
                if len(raw) >= max_results:
                    raw, truncated = raw[:max_results], True
                    break
                p += 1
            out_page = 1
        else:
            per_page = max(1, min(per_page, 250))
            res = self._search({**base, "facet_by": facet_by, "max_facet_values": FACET_MAX_VALUES,
                                "per_page": per_page, "page": max(1, page)})
            facets, total, raw, out_page = self._facets(res), res.get("found", 0), res.get("hits", []), max(1, page)
        return {"hits": [{"document": h["document"], "hl": self._hl(h)} for h in raw],
                "total": total, "page": out_page, "facets": facets, "truncated": truncated}


# ══════════════════════════════════════════════════════════════════════════════
#  SQLITE FTS5  (Windows default) - same results, no server
# ══════════════════════════════════════════════════════════════════════════════

_DOC_COLS = ("id", "filename", "path", "size_mb", "date", "category", "volume", "folder", "ext",
             "duration", "probed", "notes", "tags_custom", "description", "reporter", "location",
             "use_count", "hidden")
_FTS_COLS = SEARCH_FIELDS
_TEXT_DEFAULT = {"filename": "", "path": "", "date": "", "category": "", "volume": "", "folder": "",
                 "ext": "", "duration": "", "notes": "", "tags_custom": "", "description": "",
                 "reporter": "", "location": ""}

_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS docs (
    rid         INTEGER PRIMARY KEY,
    id          TEXT NOT NULL UNIQUE,
    filename    TEXT NOT NULL DEFAULT '',
    path        TEXT NOT NULL DEFAULT '',
    size_mb     REAL NOT NULL DEFAULT 0,
    date        TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL DEFAULT '',
    volume      TEXT NOT NULL DEFAULT '',
    folder      TEXT NOT NULL DEFAULT '',
    ext         TEXT NOT NULL DEFAULT '',
    duration    TEXT NOT NULL DEFAULT '',
    probed      INTEGER NOT NULL DEFAULT 0,
    notes       TEXT NOT NULL DEFAULT '',
    tags_custom TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    reporter    TEXT NOT NULL DEFAULT '',
    location    TEXT NOT NULL DEFAULT '',
    use_count   INTEGER NOT NULL DEFAULT 0,
    hidden      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS docs_volume ON docs(volume);
CREATE INDEX IF NOT EXISTS docs_folder ON docs(folder);
CREATE INDEX IF NOT EXISTS docs_ext    ON docs(ext);
CREATE INDEX IF NOT EXISTS docs_hidden ON docs(hidden);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5({", ".join(_FTS_COLS)}, tokenize = 'unicode61');
"""


def _is_tok(ch: str) -> bool:
    """Same notion of 'word character' as FTS5's unicode61 tokenizer: letters, digits and
    combining marks (so Tamil vowel signs stay inside the word). Everything else separates."""
    return unicodedata.category(ch)[0] in "LNM"


def tokenize(text: str) -> list:
    """Split text into lowercase words. '_' '-' '.' '(' ')' '[' ']' '+' and spaces all separate."""
    out, cur = [], []
    for ch in text or "":
        if _is_tok(ch):
            cur.append(ch)
        elif cur:
            out.append("".join(cur).casefold())
            cur = []
    if cur:
        out.append("".join(cur).casefold())
    return out


def _fts_match(toks: list) -> str:
    """Every word must match inside ONE field (as Typesense does); only the LAST word may be a
    prefix.  ->  {filename} : ("amma" "laun"*) OR {folder} : ("amma" "laun"*) OR ..."""
    phrase = " ".join('"%s"%s' % (t.replace('"', '""'), "*" if i == len(toks) - 1 else "")
                      for i, t in enumerate(toks))
    # the parentheses matter: without them FTS5 applies the column filter to the FIRST word only
    return " OR ".join("{%s} : (%s)" % (col, phrase) for col in _FTS_COLS)


def _mark(text: str, toks: list) -> str:
    """Wrap every filename word that matched a search word in HL_START / HL_END."""
    last = len(toks) - 1
    out, i, n = [], 0, len(text)
    while i < n:
        if _is_tok(text[i]):
            j = i
            while j < n and _is_tok(text[j]):
                j += 1
            word = text[i:j]
            fold = word.casefold()
            if any(fold == t or (k == last and fold.startswith(t)) for k, t in enumerate(toks)):
                out.append(HL_START + word + HL_END)
            else:
                out.append(word)
            i = j
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _row_to_doc(r) -> dict:
    d = {k: r[k] for k in _DOC_COLS}
    d["probed"] = bool(d["probed"])
    d["hidden"] = bool(d["hidden"])
    d["use_count"] = int(d["use_count"] or 0)
    d["size_mb"] = float(d["size_mb"] or 0)
    return d


class SqliteBackend:
    name = "sqlite"

    def __init__(self, path=None):
        self.path = Path(path or os.environ.get("CLIPSTAGE_SEARCH_DB") or (BASE_DIR / "clipstage_search.db"))
        self._local = threading.local()
        self._c()                                  # fail early (e.g. FTS5 missing) with a clear message

    # -- connection ---------------------------------------------------------
    def _c(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            try:
                c = sqlite3.connect(str(self.path), timeout=30)
                c.row_factory = sqlite3.Row
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("PRAGMA synchronous=NORMAL")
                c.executescript(_SCHEMA_SQL)
            except sqlite3.OperationalError as exc:
                if "fts5" in str(exc).lower():
                    raise RuntimeError("This Python's SQLite has no FTS5 support. Install the official "
                                       "python.org build of Python 3.10+ (it includes FTS5).") from exc
                raise SearchUnavailable(f"Cannot open search database {self.path}: {exc}") from exc
            self._local.c = c
        return c

    # -- collection ---------------------------------------------------------
    def setup(self, force: bool = False):
        c = self._c()
        n = c.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        if force and n:
            with c:
                c.execute("DELETE FROM docs")
                c.execute("DELETE FROM fts")
            print("Search index wiped and recreated (notes / use_count / durations carried over).")
        elif n:
            print("Search index exists — updating new/changed files only (notes + use_count preserved).")
        else:
            print("Search index created.")

    def startup_check(self):
        n = self._c().execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        if n == 0:
            print(f"[startup] Search index {self.path.name} is empty — run the indexer "
                  f"(indexer.py / the Index button) to fill it.", flush=True)

    def info(self) -> dict:
        n = self._c().execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        return {"documents": n, "token_separators": TOKEN_SEPARATORS, "engine": "sqlite-fts5"}

    # -- documents ----------------------------------------------------------
    def get(self, uid: str):
        r = self._c().execute("SELECT * FROM docs WHERE id=?", (uid,)).fetchone()
        return _row_to_doc(r) if r else None

    def get_many(self, uids) -> dict:
        out, uids = {}, list(uids)
        c = self._c()
        for i in range(0, len(uids), 500):
            chunk = uids[i:i + 500]
            q = ",".join("?" * len(chunk))
            for r in c.execute(f"SELECT * FROM docs WHERE id IN ({q})", chunk):
                out[r["id"]] = _row_to_doc(r)
        return out

    @staticmethod
    def _norm(doc: dict) -> dict:
        v = {}
        for k, val in doc.items():
            if k not in _DOC_COLS:
                continue
            if k in ("probed", "hidden"):
                v[k] = 1 if val else 0
            elif k == "use_count":
                v[k] = int(val or 0)
            elif k == "size_mb":
                v[k] = float(val or 0)
            else:
                v[k] = "" if val is None else str(val)
        return v

    def _write_one(self, c, doc: dict, mode: str) -> bool:
        uid = doc.get("id")
        if not uid:
            return False
        cur = c.execute("SELECT * FROM docs WHERE id=?", (uid,)).fetchone()
        if cur is None and mode == "update":
            return False
        patch = self._norm(doc)
        if cur is None or mode == "upsert":
            base = {"size_mb": 0.0, "probed": 0, "use_count": 0, "hidden": 0, **_TEXT_DEFAULT}
        else:
            base = {k: cur[k] for k in _DOC_COLS}
        base.update(patch)
        base["id"] = uid
        fts_changed = cur is None or mode == "upsert" or any(k in patch for k in _FTS_COLS)
        if cur is None:
            cols = list(_DOC_COLS)
            rid = c.execute(f"INSERT INTO docs ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                            [base[k] for k in cols]).lastrowid
        else:
            rid = cur["rid"]
            cols = [k for k in _DOC_COLS if k != "id"]
            c.execute(f"UPDATE docs SET {','.join(k + '=?' for k in cols)} WHERE rid=?",
                      [base[k] for k in cols] + [rid])
        if fts_changed:
            c.execute("DELETE FROM fts WHERE rowid=?", (rid,))
            c.execute(f"INSERT INTO fts (rowid, {','.join(_FTS_COLS)}) VALUES (?, {','.join('?' * len(_FTS_COLS))})",
                      [rid] + [base[k] for k in _FTS_COLS])
        return True

    def _write_many(self, docs: list, mode: str) -> int:
        c, failed = self._c(), 0
        with c:
            for d in docs:
                try:
                    if not self._write_one(c, d, mode):
                        failed += 1
                except sqlite3.Error:
                    failed += 1
        return failed

    def update(self, uid: str, fields: dict):
        if self._write_many([{**fields, "id": uid}], "update"):
            raise KeyError(uid)

    def update_many(self, patches: list):
        self._write_many(patches, "update")

    def upsert(self, docs: list, action: str = "emplace") -> int:
        return self._write_many(docs, "upsert" if action == "upsert" else
                                "update" if action == "update" else "emplace")

    def delete_ids(self, ids: list):
        ids, c = list(ids), self._c()
        with c:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                q = ",".join("?" * len(chunk))
                rids = [r[0] for r in c.execute(f"SELECT rid FROM docs WHERE id IN ({q})", chunk)]
                for rid in rids:
                    c.execute("DELETE FROM fts WHERE rowid=?", (rid,))
                c.execute(f"DELETE FROM docs WHERE id IN ({q})", chunk)

    def export_all(self) -> dict:
        return {r["id"]: _row_to_doc(r) for r in self._c().execute("SELECT * FROM docs")}

    def list_hidden(self, limit: int = 250):
        c = self._c()
        total = c.execute("SELECT COUNT(*) FROM docs WHERE hidden=1").fetchone()[0]
        rows = c.execute("SELECT id, filename, path FROM docs WHERE hidden=1 LIMIT ?", (limit,))
        return ([{"id": r["id"], "filename": r["filename"], "path": r["path"]} for r in rows], total)

    # -- search -------------------------------------------------------------
    _ORDER = {
        "relevance": "s ASC, d.date DESC",
        "size_asc": "d.size_mb ASC", "size_desc": "d.size_mb DESC",
        "newest": "d.date DESC", "oldest": "d.date ASC",
        "most_used": "d.use_count DESC, d.date DESC",
    }

    def search(self, q, *, volume="", folder="", ext="", sort="", page=1, per_page=30,
               want_all=True, max_results=2000, num_typos=0) -> dict:
        toks = tokenize(q)
        empty = {"hits": [], "total": 0, "page": 1, "facets": {}, "truncated": False}
        if not toks:
            return empty
        where, params = ["fts MATCH ?", "d.hidden = 0"], [_fts_match(toks)]
        for col, val in (("volume", volume), ("folder", folder), ("ext", ext.upper() if ext else "")):
            if val:
                if len(val) > 200:
                    raise ValueError("Invalid filter value")
                where.append(f"d.{col} = ?")
                params.append(val)
        weights = ",".join(str(w) for w in SEARCH_WEIGHTS)
        order = self._ORDER.get(sort, "s ASC")                 # "" / "best" -> relevance, bucketed below
        sql = (f"SELECT d.rid, bm25(fts, {weights}) AS s, d.use_count AS u, d.date AS dt, "
               f"d.volume AS v, d.folder AS f, d.ext AS e "
               f"FROM fts JOIN docs d ON d.rid = fts.rowid WHERE {' AND '.join(where)} ORDER BY {order}")
        try:
            rows = [tuple(r) for r in self._c().execute(sql, params)]
        except sqlite3.OperationalError as exc:
            raise SearchUnavailable(f"Search failed: {exc}") from exc
        total = len(rows)
        # rows: 0 rid, 1 score (lower = better), 2 use_count, 3 date, 4 volume, 5 folder, 6 ext

        # facet counts over everything that matched (before paging), like Typesense
        facets = {}
        for name, idx in (("volume", 4), ("folder", 5), ("ext", 6)):
            counts = {}
            for r in rows:
                if r[idx]:
                    counts[r[idx]] = counts.get(r[idx], 0) + 1
            top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:FACET_MAX_VALUES]
            if top:
                facets[name] = [{"value": k, "count": n} for k, n in top]

        if sort in ("", "best"):
            if RANK_BUCKETS > 0 and total:
                size = math.ceil(total / RANK_BUCKETS)
                ordered, i = [], 0
                while i < total:
                    j = min(i + size, total)
                    while j < total and rows[j][1] == rows[j - 1][1]:        # never split an exact tie
                        j += 1
                    band = rows[i:j]
                    band.sort(key=lambda r: r[3] or "", reverse=True)        # newest
                    band.sort(key=lambda r: r[2] or 0, reverse=True)         # then most used (stable)
                    ordered += band
                    i = j
                rows = ordered
            else:
                rows.sort(key=lambda r: r[3] or "", reverse=True)
                rows.sort(key=lambda r: r[2] or 0, reverse=True)
                rows.sort(key=lambda r: r[1])                                # exact-score ties only

        truncated = False
        if want_all:
            if len(rows) > max_results:
                rows, truncated = rows[:max_results], True
            out_page = 1
        else:
            per_page = max(1, min(per_page, 250))
            out_page = max(1, page)
            rows = rows[(out_page - 1) * per_page: out_page * per_page]

        docs = self.get_by_rids([r[0] for r in rows])
        hits = []
        for r in rows:
            d = docs.get(r[0])
            if d:
                hits.append({"document": d, "hl": _mark(d["filename"], toks)})
        return {"hits": hits, "total": total, "page": out_page, "facets": facets, "truncated": truncated}

    def get_by_rids(self, rids: list) -> dict:
        out, c = {}, self._c()
        for i in range(0, len(rids), 500):
            chunk = rids[i:i + 500]
            q = ",".join("?" * len(chunk))
            for r in c.execute(f"SELECT * FROM docs WHERE rid IN ({q})", chunk):
                out[r["rid"]] = _row_to_doc(r)
        return out


# ══════════════════════════════════════════════════════════════════════════════

def backend_name() -> str:
    name = (os.environ.get("SEARCH_BACKEND") or "").strip().lower()
    if not name:
        name = "sqlite" if IS_WINDOWS else "typesense"
    if name not in ("typesense", "sqlite"):
        raise RuntimeError(f"SEARCH_BACKEND must be 'typesense' or 'sqlite' (got '{name}')")
    return name


_BACKEND = None
_LOCK = threading.Lock()


def get_backend(timeout: int = 30):
    """One shared backend per process (api.py, indexer.py, generate_thumbs.py all call this)."""
    global _BACKEND
    with _LOCK:
        if _BACKEND is None:
            _BACKEND = SqliteBackend() if backend_name() == "sqlite" else TypesenseBackend(timeout)
        return _BACKEND
