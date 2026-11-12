"""
API tests with a FAKE Typesense and a FAKE Supabase (no network, no NAS needed).

They prove ClipStage's own logic: authorization, CSRF, staging safety, SQLite user data,
soft delete, locks, rate limit, HTML escaping. They do NOT prove Typesense's ranking —
test that once against your real server (see README "Verify search").

    pip install fastapi uvicorn typesense requests httpx pytest
    python -m pytest tests -q
"""
import os
import re
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
os.environ.update(
    SEARCH_BACKEND="typesense", TYPESENSE_KEY="test-key", SUPABASE_URL="https://x.supabase.co", SUPABASE_ANON_KEY="anon",
    CLIPSTAGE_DB=str(TMP / "t.db"), CLIPSTAGE_STAGING_PATH=str(TMP / "staging"),
    CLIPSTAGE_VOLUMES_ROOT=str(TMP / "vol"), CLIPSTAGE_MAX_STREAMS="1",
)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient
from typesense.exceptions import ObjectNotFound

import api
import store

H = {"x-clipstage-csrf": "1"}
FUTURE = (date.today() + timedelta(days=30)).isoformat()
PAST = (date.today() - timedelta(days=1)).isoformat()

ACCOUNTS = {   # token -> (email, role, valid_through, editor_name)
    "admintok": ("admin@x.com", "admin", FUTURE, ""),
    "edtok": ("shared@x.com", "editor", FUTURE, ""),
    "boundtok": ("gokul@x.com", "editor", FUTURE, "GOKUL"),
    "exptok": ("old@x.com", "editor", PAST, ""),
}
BAD_LOGINS = {"bad@x.com"}
LOGOUT_CALLS = []


class Resp:
    def __init__(self, code, body):
        self.status_code, self._b = code, body

    def json(self):
        return self._b


def fake_request(method, url, headers=None, timeout=None, **kw):
    tok = (headers or {}).get("Authorization", "").replace("Bearer ", "")
    if url.endswith("/auth/v1/user"):
        a = ACCOUNTS.get(tok)
        return Resp(200, {"id": tok, "email": a[0]}) if a else Resp(401, {})
    if "/rest/v1/clipstage_profiles" in url:
        a = ACCOUNTS[tok]
        row = {"role": a[1], "valid_through": a[2]}
        if "editor_name" in kw["params"]["select"]:
            row["editor_name"] = a[3]
        return Resp(200, [row])
    if url.endswith("/auth/v1/logout"):
        LOGOUT_CALLS.append(tok)
        return Resp(204, {})
    if "grant_type=refresh_token" in url:
        if kw["json"]["refresh_token"] == "badref":
            return Resp(400, {})
        return Resp(200, {"access_token": "admintok", "refresh_token": "newref", "expires_in": 3600,
                          "user": {"id": "a", "email": "admin@x.com"}})
    if "grant_type=password" in url:
        if kw["json"]["email"] in BAD_LOGINS:
            return Resp(400, {})
        return Resp(200, {"access_token": "admintok", "expires_in": 3600, "user": {"id": "a", "email": "admin@x.com"}})
    raise AssertionError(url)


SEP = re.compile(r"[\s_\-.()\[\]+]+")


def toks(s):
    return [t for t in SEP.split(str(s).lower()) if t]


class FakeDocs:
    def __init__(self, store_):
        self.d = store_

    def __getitem__(self, uid):
        d = self.d

        class One:
            def retrieve(_):
                if uid not in d:
                    raise ObjectNotFound("nope")
                return dict(d[uid])

            def update(_, fields):
                if uid not in d:
                    raise ObjectNotFound("nope")
                d[uid].update(fields)
        return One()

    def import_(self, patches, opts):
        for p in patches:
            if p["id"] in self.d:
                self.d[p["id"]].update(p)
        return [{"success": True}] * len(patches)

    def search(self, p):
        docs = list(self.d.values())
        for cond in (p.get("filter_by") or "").split(" && "):
            if not cond:
                continue
            f, _, v = cond.partition(":")
            if v.startswith("["):
                ids = v.strip("[]").split(",")
                docs = [x for x in docs if x["id"] in ids]
            else:
                v = v.lstrip("=").strip("`")
                docs = [x for x in docs if str(x.get(f, False)).lower() == v.lower()]
        q = p["q"]
        if q != "*":
            qt = toks(q)
            fields = p["query_by"].split(",")

            def ok(doc):
                for f in fields:
                    ft = toks(doc.get(f, ""))
                    if all(any(t == w or (i == len(qt) - 1 and p.get("prefix") and t.startswith(w)) for t in ft)
                           for i, w in enumerate(qt)):
                        return True
                return False
            docs = [x for x in docs if ok(x)]
        page, per = p.get("page", 1), p.get("per_page", 10)
        return {"found": len(docs), "hits": [{"document": dict(x)} for x in docs[(page - 1) * per: page * per]]}


class FakeCollection:
    def __init__(self, docs):
        self.documents = FakeDocs(docs)

    def retrieve(self):
        return {"token_separators": ["_", "-"], "num_documents": len(self.documents.d)}


class FakeClient:
    def __init__(self):
        self.docs = {}
        self.collections = {api.COLLECTION: FakeCollection(self.docs)}


def mkdoc(path, **kw):
    d = {"id": api.clip_id_for(path), "filename": path.rsplit("/", 1)[-1], "path": path, "size_mb": 10.0,
         "date": "2026-01-01", "category": "NEWS", "volume": "EDIT", "folder": "NEWS", "ext": "MXF", "hidden": False}
    d.update(kw)
    return d


@pytest.fixture()
def env(monkeypatch):
    fc = FakeClient()
    monkeypatch.setattr(api.backend, "client", fc)
    monkeypatch.setattr(api.requests, "request", fake_request)
    api._ID_CACHE.clear()
    api._LOGIN_FAILS.clear()
    (TMP / "vol" / "EDIT" / "A").mkdir(parents=True, exist_ok=True)
    (TMP / "vol" / "EDIT2" / "B").mkdir(parents=True, exist_ok=True)
    real = {}
    for name, p in {
        "a": str(TMP / "vol/EDIT/A/First_Time_This_Movie_Launch.mxf"),
        "b": str(TMP / "vol/EDIT/A/Launch_First_Movie.mxf"),
        "c": str(TMP / "vol/EDIT/A/Launch_The_Day_First.mxf"),
        "d": str(TMP / "vol/EDIT/A/Weather_Report.mxf"),
        "e1": str(TMP / "vol/EDIT/A/same.mxf"),
        "e2": str(TMP / "vol/EDIT2/B/same.mxf"),
        "x": str(TMP / "vol/EDIT/A/<img src=x onerror=alert(1)>_launch.mxf"),
    }.items():
        Path(p).write_bytes(b"x")
        d = mkdoc(p)
        fc.docs[d["id"]] = d
        real[name] = d
    monkeypatch.setattr(api, "_editor_names", lambda: ["GOKUL", "PRIYA"])
    with TestClient(api.app) as c:
        yield c, fc, real


def A(tok):
    return {**H, "Authorization": f"Bearer {tok}"}


# ── auth / csrf ──────────────────────────────────────────────────────────────

def test_requires_login(env):
    c, *_ = env
    assert c.get("/search?q=launch").status_code == 401
    assert c.get("/health").json() == {"status": "ok"}          # nothing leaked


def test_expired_account_rejected(env):
    c, *_ = env
    assert c.get("/search?q=launch", headers=A("exptok")).status_code == 401


def test_csrf_header_required_for_writes(env):
    c, _, r = env
    res = c.post("/clips/bulk-notes", json={"ids": [], "notes": ""}, headers={"Authorization": "Bearer edtok"})
    assert res.status_code == 403


def test_refresh_cache_endpoint_is_gone(env):
    c, *_ = env
    assert c.post("/admin/refresh-cache", headers=A("admintok")).status_code in (404, 405)


def test_auth_me_reports_account_and_role(env):
    c, *_ = env
    assert c.get("/auth/me").status_code == 401                      # not signed in
    me = c.get("/auth/me", headers=A("admintok")).json()
    assert me["username"] == "admin@x.com" and me["role"] == "admin" and me["valid_through"] == FUTURE
    ed = c.get("/auth/me", headers=A("boundtok")).json()
    assert ed["role"] == "editor" and ed["editor_name"] == "GOKUL"


def test_logout_clears_cookie_and_revokes_supabase_session(env):
    c, *_ = env
    LOGOUT_CALLS.clear()
    c.cookies.set("clipstage_access_token", "admintok")
    assert c.get("/auth/me").status_code == 200
    res = c.post("/auth/logout")
    assert res.status_code == 200 and res.json() == {"ok": True}
    assert LOGOUT_CALLS == ["admintok"]                               # session ended at Supabase too
    assert "clipstage_access_token" in res.headers.get("set-cookie", "")   # cookie cleared
    assert api._ID_CACHE == {}                                             # not trusted from cache any more


def test_refresh_issues_new_cookies_and_rotates_refresh_token(env):
    c, *_ = env
    c.cookies.set("clipstage_refresh_token", "oldref", path="/auth")
    res = c.post("/auth/refresh", headers=H)
    assert res.status_code == 200
    body = res.json()
    assert body["role"] == "admin" and body["expires_in"] > 0
    cookies = res.headers.get("set-cookie", "")
    assert "clipstage_access_token=admintok" in cookies
    assert "clipstage_refresh_token=newref" in cookies and "Path=/auth" in cookies


def test_refresh_needs_csrf_header_and_cookie(env):
    c, *_ = env
    c.cookies.set("clipstage_refresh_token", "oldref", path="/auth")
    assert c.post("/auth/refresh").status_code == 403                 # no CSRF header
    c.cookies.clear()
    assert c.post("/auth/refresh", headers=H).status_code == 401      # no refresh cookie


def test_refresh_rejected_token_clears_cookies(env):
    c, *_ = env
    c.cookies.set("clipstage_refresh_token", "badref", path="/auth")
    res = c.post("/auth/refresh", headers=H)
    assert res.status_code == 401
    assert "clipstage_refresh_token" in res.headers.get("set-cookie", "")   # cleared


def test_logout_also_clears_refresh_cookie(env):
    c, *_ = env
    res = c.post("/auth/logout")
    assert "clipstage_refresh_token" in res.headers.get("set-cookie", "")


def test_login_rate_limit(env):
    c, *_ = env
    for _ in range(5):
        assert c.post("/auth/login", json={"email": "bad@x.com", "password": "p"}).status_code == 401
    assert c.post("/auth/login", json={"email": "bad@x.com", "password": "p"}).status_code == 429


# ── search ───────────────────────────────────────────────────────────────────

def test_any_order_search(env):
    c, *_ = env
    names = {h["filename"] for h in c.get("/search?q=launch first", headers=A("edtok")).json()["hits"]}
    assert {"First_Time_This_Movie_Launch.mxf", "Launch_First_Movie.mxf", "Launch_The_Day_First.mxf"} <= names
    assert "Weather_Report.mxf" not in names


def test_prefix_on_last_word(env):
    c, *_ = env
    assert c.get("/search?q=first laun", headers=A("edtok")).json()["total"] >= 3


def test_filename_html_is_escaped(env):
    c, *_ = env
    hits = c.get("/search?q=launch", headers=A("edtok")).json()["hits"]
    evil = next(h for h in hits if "onerror" in h["filename"])
    assert "<img" not in evil["filename_hl"]


# ── authorization ────────────────────────────────────────────────────────────

def test_only_admin_can_hide_and_restore(env):
    c, _, r = env
    uid = r["d"]["id"]
    assert c.delete(f"/clip/{uid}", headers=A("edtok")).status_code == 403
    assert c.delete(f"/clip/{uid}", headers=A("admintok")).status_code == 200
    assert c.get("/search?q=weather", headers=A("edtok")).json()["total"] == 0
    assert c.post(f"/admin/restore/{uid}", headers=A("edtok")).status_code == 403
    assert c.post(f"/admin/restore/{uid}", headers=A("admintok")).status_code == 200
    assert c.get("/search?q=weather", headers=A("edtok")).json()["total"] == 1


def test_audit_admin_only(env):
    c, *_ = env
    assert c.get("/admin/audit", headers=A("edtok")).status_code == 403
    assert c.get("/admin/audit", headers=A("admintok")).status_code == 200


def test_bound_account_limited_to_own_editor(env):
    c, _, r = env
    body = {"editor": "PRIYA", "paths": [r["a"]["path"]]}
    assert c.post("/stage", json=body, headers=A("boundtok")).status_code == 403
    assert c.delete("/stage/PRIYA", headers=A("boundtok")).status_code == 403
    assert c.post("/stage", json={**body, "editor": "GOKUL"}, headers=A("boundtok")).status_code == 200
    assert c.get("/editors", headers=A("boundtok")).json()["editors"] == ["GOKUL"]


def test_shared_account_can_use_any_listed_editor(env):
    c, _, r = env
    assert c.post("/stage", json={"editor": "PRIYA", "paths": [r["a"]["path"]]}, headers=A("edtok")).json()["count"] == 1
    assert c.post("/stage", json={"editor": "NOBODY", "paths": [r["a"]["path"]]}, headers=A("edtok")).status_code == 403


# ── staging safety ───────────────────────────────────────────────────────────

def test_cannot_stage_files_outside_index(env):
    c, *_ = env
    res = c.post("/stage", json={"editor": "GOKUL", "paths": ["/etc/passwd", "/Volumes/../etc/hosts"]},
                 headers=A("edtok")).json()
    assert res["count"] == 0 and len(res["errors"]) == 2


def test_same_filename_does_not_overwrite(env):
    c, _, r = env
    res = c.post("/stage", json={"editor": "GOKUL", "paths": [r["e1"]["path"], r["e2"]["path"]]}, headers=A("edtok")).json()
    assert res["count"] == 2 and len(set(res["staged"])) == 2
    again = c.post("/stage", json={"editor": "GOKUL", "paths": [r["e1"]["path"]]}, headers=A("edtok")).json()
    assert again["staged"][0] in res["staged"]                    # re-staging reuses the link


def test_use_count_lives_in_sqlite(env):
    c, _, r = env
    c.post("/stage", json={"editor": "GOKUL", "paths": [r["b"]["path"]]}, headers=A("edtok"))
    hit = next(h for h in c.get("/search?q=launch", headers=A("edtok")).json()["hits"] if h["id"] == r["b"]["id"])
    assert hit["use_count"] == 1
    assert store.get_many([r["b"]["id"]])[r["b"]["id"]]["use_count"] == 1


def test_staging_list_maps_uid(env):
    c, _, r = env
    c.post("/stage", json={"editor": "GOKUL", "paths": [r["b"]["path"]]}, headers=A("edtok"))
    clips = c.get("/stage/GOKUL", headers=A("edtok")).json()["clips"]
    assert clips[0]["uid"] == r["b"]["id"]


# ── notes / meta / locks ─────────────────────────────────────────────────────

def test_notes_saved_in_sqlite_and_versioned(env):
    c, fc, r = env
    uid = r["a"]["id"]
    v1 = c.patch(f"/clip/{uid}/notes", json={"notes": "cm speech"}, headers=A("edtok")).json()["version"]
    assert store.get_many([uid])[uid]["notes"] == "cm speech"
    assert fc.docs[uid]["notes"] == "cm speech"                   # mirrored for search
    assert c.patch(f"/clip/{uid}/notes", json={"notes": "x", "version": v1 - 1}, headers=A("edtok")).status_code == 409
    assert c.patch(f"/clip/{uid}/notes", json={"notes": "y", "version": v1}, headers=A("edtok")).status_code == 200
    assert c.patch(f"/clip/{uid}/notes", json={"notes": "z" * 5001}, headers=A("edtok")).status_code == 400


def test_bulk_notes_limit(env):
    c, _, r = env
    assert c.post("/clips/bulk-notes", json={"ids": ["a" * 5] * 201, "notes": "n"}, headers=A("edtok")).status_code == 400
    ok = c.post("/clips/bulk-notes", json={"ids": [r["a"]["id"], "nope"], "notes": "n"}, headers=A("edtok")).json()
    assert ok["ok"] == [r["a"]["id"]] and ok["failed"] == ["nope"]


def test_meta_roundtrip(env):
    c, _, r = env
    uid = r["a"]["id"]
    assert c.post(f"/meta/{uid}", json={"tags": "cm, chennai", "reporter": "Priya"}, headers=A("edtok")).status_code == 200
    hit = next(h for h in c.get("/search?q=launch", headers=A("edtok")).json()["hits"] if h["id"] == uid)
    assert hit["tags_custom"] == "cm, chennai" and hit["meta_updated_by"] == "shared@x.com"


def test_lock_conflict(env):
    c, _, r = env
    uid = r["a"]["id"]
    assert c.post(f"/lock/{uid}", json={"editor": "GOKUL"}, headers=A("edtok")).json()["conflict"] is False
    other = c.post(f"/lock/{uid}", json={"editor": "PRIYA"}, headers=A("edtok")).json()
    assert other["conflict"] is True and other["locked_by"] == "GOKUL"
    chk = c.post("/check-conflicts", json={"editor": "PRIYA", "paths": [r["a"]["path"]]}, headers=A("edtok")).json()
    assert chk["has_conflicts"] and chk["conflicts"][0]["locked_by"] == "GOKUL"
    assert c.request("DELETE", f"/lock/{uid}", json={"editor": "GOKUL"}, headers=A("edtok")).status_code == 200
    assert c.post("/check-conflicts", json={"editor": "PRIYA", "paths": [r["a"]["path"]]}, headers=A("edtok")).json()["has_conflicts"] is False


# ── stream ───────────────────────────────────────────────────────────────────

def test_stream_limit_and_unicode_filename(env, monkeypatch):
    c, fc, r = env
    monkeypatch.setattr(api, "_FFMPEG_BIN", "/bin/true")
    tamil = str(TMP / "vol/EDIT/A/செய்தி_launch.mxf")
    Path(tamil).write_bytes(b"x")
    d = mkdoc(tamil)
    fc.docs[d["id"]] = d
    res = c.get(f"/stream/{d['id']}", headers=A("edtok"))
    assert res.status_code == 200 and "filename*=UTF-8''" in res.headers["content-disposition"]
    assert api._STREAM_SLOTS.acquire(blocking=False)              # take the only slot
    try:
        assert c.get(f"/stream/{d['id']}", headers=A("edtok")).status_code == 429
    finally:
        api._STREAM_SLOTS.release()


# ── injection attempts ───────────────────────────────────────────────────────

def test_filter_injection_rejected(env):
    c, *_ = env
    for param in ("volume", "folder", "ext"):
        assert c.get(f"/search?q=launch&{param}=EDIT%60%20%26%26%20hidden%3A%3Dtrue", headers=A("edtok")).status_code == 400


def test_odd_ids_rejected_and_sql_stays_literal(env):
    c, _, r = env
    assert c.patch("/clip/1'%20OR%20'1/notes", json={"notes": "x"}, headers=A("edtok")).status_code == 400
    assert c.patch("/clip/..%2F..%2Fetc/notes", json={"notes": "x"}, headers=A("edtok")).status_code in (400, 404)
    uid = r["a"]["id"]
    evil = "'); DROP TABLE clip_meta;--"
    assert c.patch(f"/clip/{uid}/notes", json={"notes": evil}, headers=A("edtok")).status_code == 200
    assert store.get_many([uid])[uid]["notes"] == evil          # stored as plain text, table intact


def test_editor_name_traversal_rejected(env):
    c, *_ = env
    for bad in ("..", "../x", "GOKUL/../PRIYA"):
        assert c.post("/stage", json={"editor": bad, "paths": ["x"]}, headers=A("admintok")).status_code == 403
    assert c.delete("/stage/..", headers=A("admintok")).status_code in (403, 404, 405)   # client normalises ".." away; nothing is deleted


def test_unknown_sort_falls_back(env):
    c, *_ = env
    assert c.get("/search?q=launch&sort=name);drop", headers=A("edtok")).status_code == 200


def _fake_indexer(monkeypatch, tmp_path):
    launched = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, **kw):
        launched["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(api.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(api, "INDEX_STATUS", tmp_path / "status.json")
    monkeypatch.setattr(api, "INDEXER_LOG", tmp_path / "indexer.log")
    monkeypatch.setattr(api, "SCAN_VOLUMES", ["EDIT", "EDIT2", "PLAYOUT"])
    return launched


def test_run_indexer_accepts_several_volumes(env, monkeypatch, tmp_path):
    c, *_ = env
    launched = _fake_indexer(monkeypatch, tmp_path)
    r = c.post("/admin/run-indexer?volume=playout,edit", headers=A("admintok"))
    assert r.status_code == 200 and r.json()["scope"] == "EDIT, PLAYOUT"
    assert launched["cmd"][-2:] == ["--only", "EDIT,PLAYOUT"]


def test_run_indexer_all_volumes_ticked_is_a_normal_full_run(env, monkeypatch, tmp_path):
    c, *_ = env
    launched = _fake_indexer(monkeypatch, tmp_path)
    r = c.post("/admin/run-indexer?volume=EDIT,EDIT2,PLAYOUT", headers=A("admintok"))
    assert r.status_code == 200 and r.json()["scope"] == "All volumes"
    assert "--only" not in launched["cmd"]


def test_run_indexer_rejects_unknown_volume_in_list(env, monkeypatch, tmp_path):
    c, *_ = env
    _fake_indexer(monkeypatch, tmp_path)
    r = c.post("/admin/run-indexer?volume=EDIT,NOPE", headers=A("admintok"))
    assert r.status_code == 400 and "NOPE" in r.json()["detail"]


def test_search_returns_match_counts_per_column(env):
    c, fc, _ = env
    orig = fc.collections[api.COLLECTION].documents.search

    def with_facets(p):
        res = orig(p)
        assert p.get("facet_by") == "volume,folder,ext" if p.get("page", 1) == 1 else True
        res["facet_counts"] = [{"field_name": "volume", "counts": [{"value": "EDIT", "count": 1234}, {"value": "EDIT2", "count": 7}]}]
        return res

    fc.collections[api.COLLECTION].documents.search = with_facets
    body = c.get("/search?q=launch", headers=A("edtok")).json()
    assert body["facets"]["volume"] == [{"value": "EDIT", "count": 1234}, {"value": "EDIT2", "count": 7}]


def test_search_works_when_no_facets_come_back(env):
    c, *_ = env
    assert c.get("/search?q=launch", headers=A("edtok")).json()["facets"] == {}


def test_default_ranking_is_match_then_popularity_then_newest(env):
    c, fc, _ = env
    seen = []
    orig = fc.collections[api.COLLECTION].documents.search
    fc.collections[api.COLLECTION].documents.search = lambda p: (seen.append(p["sort_by"]), orig(p))[1]
    c.get("/search?q=launch", headers=A("edtok"))
    c.get("/search?q=launch&sort=newest", headers=A("edtok"))
    assert seen[0].startswith("_text_match(buckets:") and "use_count:desc" in seen[0] and seen[0].endswith("date:desc")
    assert seen[1] == "date:desc"


def test_search_retries_without_buckets_on_older_typesense(env):
    c, fc, _ = env
    orig = fc.collections[api.COLLECTION].documents.search
    calls = []

    def picky(p):
        calls.append(p["sort_by"])
        if "(buckets:" in p["sort_by"]:
            raise RuntimeError("bad sort")
        return orig(p)

    fc.collections[api.COLLECTION].documents.search = picky
    r = c.get("/search?q=launch", headers=A("edtok"))
    assert r.status_code == 200 and r.json()["total"] > 0
    assert calls[-1] == "_text_match:desc,use_count:desc,date:desc"


# ── Stop button: /admin/stop-indexer ───────────────────────────────────────────────────────────

def _running_status(monkeypatch, tmp_path, pid=424242):
    import json as _json
    import time as _time
    st = tmp_path / "status.json"
    st.write_text(_json.dumps({"state": "running", "phase": "scanning", "pid": pid,
                               "started_at": _time.time(), "updated_at": _time.time(), "scope": "All volumes"}))
    monkeypatch.setattr(api, "INDEX_STATUS", st)
    monkeypatch.setattr(api, "INDEX_STOP_FILE", tmp_path / "index_stop.flag")
    monkeypatch.setattr(api, "_pid_alive", lambda p: True)
    started = []
    monkeypatch.setattr(api.threading, "Thread", lambda target, args, daemon: type(
        "T", (), {"start": lambda self: started.append(args)})())
    return st, started


def test_stop_indexer_requires_admin(env, monkeypatch, tmp_path):
    c, *_ = env
    _running_status(monkeypatch, tmp_path)
    assert c.post("/admin/stop-indexer", headers=A("edtok")).status_code == 403
    assert c.post("/admin/stop-indexer", headers=H).status_code == 401      # CSRF ok, but not signed in
    assert not (tmp_path / "index_stop.flag").exists()


def test_stop_indexer_needs_csrf_header(env, monkeypatch, tmp_path):
    c, *_ = env
    _running_status(monkeypatch, tmp_path)
    r = c.post("/admin/stop-indexer", headers={"Authorization": "Bearer admintok"})
    assert r.status_code in (400, 401, 403)
    assert not (tmp_path / "index_stop.flag").exists()


def test_stop_indexer_writes_flag_and_starts_watchdog(env, monkeypatch, tmp_path):
    c, *_ = env
    _, started = _running_status(monkeypatch, tmp_path)
    r = c.post("/admin/stop-indexer", headers=A("admintok"))
    assert r.status_code == 200 and r.json()["stopping"] is True
    assert (tmp_path / "index_stop.flag").exists()
    assert started and started[0][0] == 424242
    rows = store._conn().execute("SELECT actor, action FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    assert rows["actor"] == "admin@x.com" and rows["action"] == "stop-indexer"


def test_stop_indexer_when_idle_is_409(env, monkeypatch, tmp_path):
    c, *_ = env
    st = tmp_path / "idle.json"
    st.write_text('{"state": "idle"}')
    monkeypatch.setattr(api, "INDEX_STATUS", st)
    monkeypatch.setattr(api, "INDEX_STOP_FILE", tmp_path / "index_stop.flag")
    r = c.post("/admin/stop-indexer", headers=A("admintok"))
    assert r.status_code == 409 and not (tmp_path / "index_stop.flag").exists()


def test_new_run_clears_an_old_stop_request(env, monkeypatch, tmp_path):
    c, *_ = env
    _fake_indexer(monkeypatch, tmp_path)
    flag = tmp_path / "index_stop.flag"
    flag.write_text("old")
    monkeypatch.setattr(api, "INDEX_STOP_FILE", flag)
    assert c.post("/admin/run-indexer", headers=A("admintok")).status_code == 200
    assert not flag.exists()


def test_force_stop_only_kills_a_confirmed_indexer(monkeypatch, tmp_path):
    killed = []
    monkeypatch.setattr(api, "_pid_alive", lambda p: not killed)          # alive until "killed"
    monkeypatch.setattr(api, "terminate_process_tree", lambda pid, force=False: killed.append((pid, force)) or True)
    monkeypatch.setattr(api, "INDEX_STATUS", tmp_path / "s.json")
    (tmp_path / "s.json").write_text('{"state": "running", "pid": 77}')
    monkeypatch.setattr(api, "INDEX_STOP_FILE", tmp_path / "f")
    monkeypatch.setattr(api.time, "sleep", lambda s: None)
    # 1) some unrelated process that reused the pid -> never touched
    monkeypatch.setattr(api, "process_cmdline", lambda pid: "/usr/bin/vim notes.txt")
    api._force_stop_watchdog(77, 0)
    assert killed == []
    # 2) really indexer.py -> ended, and the status says "stopped"
    monkeypatch.setattr(api, "process_cmdline", lambda pid: "python /x/indexer.py --prune")
    api._force_stop_watchdog(77, 0)
    assert killed and killed[0] == (77, False)
    import json as _json
    assert _json.loads((tmp_path / "s.json").read_text())["state"] == "stopped"
