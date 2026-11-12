"""
ClipStage — FastAPI backend (v5.0)

v5.0     GET /auth/me (who is signed in + role); /auth/logout also ends the Supabase session;
         newest copy of indexer_status.js is served; header shows email + Log out.
What changed vs S11 (v3) and v3.2 -> v4.0
  v4.0     shared clipstage_config.py (no more `from indexer import ...`) - UI file served from
           static/ OR next to api.py, whichever is newer - empty (0-byte) thumbnails are treated
           as missing - indexer prune guard / backups / restore (see indexer.py).

Original v3 notes
  SEARCH   /search now asks Typesense directly (word-prefix, any order, ranked, filtered,
           sorted, paged). The in-memory copy of the whole archive and the second Python
           matching pass are gone. Requires the clips_v2 collection (migrate_to_v2.py).
  DATA     Notes / use_count / hidden / custom metadata live in SQLite (store.py) and are
           mirrored to Typesense so they stay searchable. A reindex can no longer lose them.
  SECURITY /admin/refresh-cache removed (was unauthenticated) · per-user authorization
           (delete / restore / audit are admin-only, staging is limited to your own editor
           name when your profile has one) · CSRF header on every state-changing call ·
           CORS closed by default · /stage only accepts clips that are in the index ·
           login rate-limit · /health no longer leaks volumes · security headers ·
           API docs off by default · audit log.
  FEATURES /lock, /check-conflicts and /meta (the UI already called them; they did not exist) ·
           soft delete + restore · same-name staging no longer overwrites another clip ·
           preview limit (CLIPSTAGE_MAX_STREAMS) · non-ASCII filenames no longer crash /stream.
"""

import hashlib
import html as _html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

import clipstage_dotenv  # noqa: F401  (loads .env first - must stay above the imports below)
import store
from search_backend import HL_END as _HL_END, HL_START as _HL_START, backend_name, get_backend
from clipstage_config import (BASE_DIR, COLLECTION, FFMPEG, INDEX_STOP_FILE, IS_WINDOWS,
                              LINK_MODE as _CFG_LINK_MODE, SCAN_VOLUMES, STAGING_PATH, STATIC_DIR,
                              THUMB_DIR, VOLUMES_ROOT, clip_uid, detached_popen_kwargs, norm_path,
                              pid_alive, process_cmdline, terminate_process_tree)

try:
    VERSION = (BASE_DIR / "VERSION").read_text().strip()
except OSError:
    VERSION = "dev"

# ─── CONFIG ───────────────────────────────────────────────────────────────────

EDITORS_FILE = BASE_DIR / "editors.json"

CLIPSTAGE_SMB_HOST = os.environ.get("CLIPSTAGE_SMB_HOST", "").strip()
STAGING_SHARE_NAME = os.environ.get("CLIPSTAGE_STAGING_SHARE", "staging").strip() or "staging"
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")

ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("CLIPSTAGE_ALLOWED_ORIGINS", "").split(",") if o.strip()]
FORCE_SECURE_COOKIE = os.environ.get("CLIPSTAGE_COOKIE_SECURE", "") == "1"
# How long a browser may stay signed in without typing the password again (silent token refresh).
# Still capped by the account's valid_through date and by Supabase's own refresh-token rules.
REFRESH_COOKIE = "clipstage_refresh_token"
try:
    REFRESH_COOKIE_DAYS = max(1, int(os.environ.get("CLIPSTAGE_REFRESH_DAYS", "7")))
except ValueError:
    REFRESH_COOKIE_DAYS = 7
ENABLE_DOCS = os.environ.get("CLIPSTAGE_DOCS", "") == "1"
MAX_STREAMS = int(os.environ.get("CLIPSTAGE_MAX_STREAMS", "3"))
MAX_RESULTS = int(os.environ.get("CLIPSTAGE_MAX_RESULTS", "2000"))
NUM_TYPOS = int(os.environ.get("CLIPSTAGE_NUM_TYPOS", "0"))

LINK_MODE = _CFG_LINK_MODE      # symlink | hardlink | copy   (CLIPSTAGE_LINK_MODE)
CSRF_HEADER = "x-clipstage-csrf"

INDEXER_SCRIPT = BASE_DIR / "indexer.py"
INDEXER_LOG = BASE_DIR / "indexer.log"
INDEX_STATUS = BASE_DIR / "index_status.json"
INDEXER_ARGS = ["--prune"]

# ─── SUPABASE AUTH ────────────────────────────────────────────────────────────
# Supabase Auth owns credentials; public.clipstage_profiles gives role, expiry and
# (optionally) the editor name a personal account is bound to.

ACCOUNT_ROLES = {"admin", "editor"}
AUTH_TIMEOUT = 10
IDENTITY_TTL = 60          # seconds an identity is trusted before Supabase is asked again


@dataclass(frozen=True)
class Who:
    email: str
    role: str
    valid_through: date
    editor_name: str = ""      # "" = shared/legacy account, may act as any listed editor

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _supabase_headers(token: str = "") -> dict:
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise HTTPException(status_code=503, detail="Supabase authentication is not configured")
    headers = {"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _supabase_request(method: str, path: str, token: str = "", **kwargs):
    try:
        return requests.request(method, f"{SUPABASE_URL}{path}", headers=_supabase_headers(token),
                                timeout=AUTH_TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        raise HTTPException(status_code=503, detail="Supabase authentication is unavailable") from exc


def _read_supabase_user(token: str):
    response = _supabase_request("GET", "/auth/v1/user", token)
    if response.status_code in (401, 403):
        return None
    if response.status_code != 200:
        raise HTTPException(status_code=503, detail="Could not verify Supabase session")
    try:
        user = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="Invalid response from Supabase") from exc
    return user if isinstance(user, dict) and user.get("id") else None


_PROFILE_SELECT = {"v": "role,valid_through,editor_name"}   # falls back if the column is missing


def _read_profile(user_id: str, token: str):
    """Returns (role, valid_through, editor_name) or None if missing/expired/invalid."""
    response = _supabase_request("GET", "/rest/v1/clipstage_profiles", token,
                                 params={"select": _PROFILE_SELECT["v"], "user_id": f"eq.{user_id}"})
    if response.status_code == 400 and "editor_name" in _PROFILE_SELECT["v"]:
        # profiles table has no editor_name column yet (SQL not re-run) — still works, unbound
        _PROFILE_SELECT["v"] = "role,valid_through"
        response = _supabase_request("GET", "/rest/v1/clipstage_profiles", token,
                                     params={"select": _PROFILE_SELECT["v"], "user_id": f"eq.{user_id}"})
    if response.status_code == 401:
        return None
    if response.status_code != 200:
        raise HTTPException(status_code=503, detail="Could not read Supabase account profile")
    try:
        profiles = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="Invalid profile response from Supabase") from exc
    if not isinstance(profiles, list) or len(profiles) != 1 or not isinstance(profiles[0], dict):
        return None
    profile = profiles[0]
    try:
        valid_through = date.fromisoformat(profile["valid_through"])
    except (KeyError, TypeError, ValueError):
        return None
    role = profile.get("role")
    if role not in ACCOUNT_ROLES or datetime.now(timezone.utc).date() > valid_through:
        return None
    return role, valid_through, str(profile.get("editor_name") or "").strip()


_ID_CACHE: dict = {}
_ID_LOCK = threading.Lock()


def _current_identity(token: str):
    """Who is this token? Cached for IDENTITY_TTL seconds so a page load does not cost
    two Supabase round-trips per request. A revoked/expired account stops working within
    that window."""
    if not token:
        return None
    key = hashlib.sha256(token.encode()).hexdigest()
    now = time.time()
    with _ID_LOCK:
        hit = _ID_CACHE.get(key)
    if hit and now - hit[0] < IDENTITY_TTL:
        who = hit[1]
        return who if datetime.now(timezone.utc).date() <= who.valid_through else None
    user = _read_supabase_user(token)
    if not user:
        return None
    profile = _read_profile(str(user["id"]), token)
    if not profile:
        return None
    who = Who(user.get("email", ""), profile[0], profile[1], profile[2])
    with _ID_LOCK:
        if len(_ID_CACHE) > 500:
            for k in sorted(_ID_CACHE, key=lambda k: _ID_CACHE[k][0])[:100]:
                _ID_CACHE.pop(k, None)
        _ID_CACHE[key] = (now, who)
    return who


def _bearer_token(authorization: str) -> str:
    scheme, separator, token = (authorization or "").partition(" ")
    return token.strip() if separator and scheme.lower() == "bearer" else ""


def get_who(request: Request) -> Who:
    who = getattr(request.state, "who", None)
    if not who:
        raise HTTPException(status_code=401, detail="Login required or account expired")
    return who


def require_admin(who: Who = Depends(get_who)) -> Who:
    if not who.is_admin:
        raise HTTPException(status_code=403, detail="Admin login required")
    return who


# ─── LOGIN RATE LIMIT ─────────────────────────────────────────────────────────

_LOGIN_FAILS: dict = {}
LOGIN_MAX_FAILS = 5
LOGIN_WINDOW = 300


def _login_blocked(key) -> bool:
    now = time.time()
    fails = [t for t in _LOGIN_FAILS.get(key, []) if now - t < LOGIN_WINDOW]
    _LOGIN_FAILS[key] = fails
    return len(fails) >= LOGIN_MAX_FAILS


def _login_failed(key):
    _LOGIN_FAILS.setdefault(key, []).append(time.time())


# ─── SEARCH BACKEND ───────────────────────────────────────────────────────────
# One code base, two engines: Typesense (Mac default) or SQLite FTS5 (Windows default).
# Choose with SEARCH_BACKEND in .env. Everything below talks to `backend` only, so the web UI
# behaves identically on both (see search_backend.py).

backend = get_backend(30)


def clip_id_for(path: str) -> str:
    """Same id the indexer gives a file (md5 of its path)."""
    return clip_uid(path)


def _valid_uid(uid) -> bool:
    return isinstance(uid, str) and uid.isalnum() and 0 < len(uid) <= 32


def _get_doc(uid: str):
    try:
        return backend.get(uid)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Search index unavailable: {exc}") from exc


def _get_docs(uids) -> dict:
    """{id: doc} for many ids (one backend call per 200 ids)."""
    return backend.get_many([u for u in uids if _valid_uid(u)])


def _mirror(uid: str, fields: dict):
    """Best-effort copy of user data into the search index. SQLite (store.py) is the source of
    truth, and the indexer re-syncs any difference on its next run."""
    try:
        backend.update(uid, fields)
    except Exception as exc:
        print(f"[mirror] {uid} not updated in the search index ({exc}) — will re-sync on next index run", flush=True)


def _mirror_many(patches: list):
    try:
        backend.update_many(patches)
    except Exception as exc:
        print(f"[mirror] bulk update failed ({exc}) — will re-sync on next index run", flush=True)


def _startup_check():
    backend.startup_check()


# ─── APP ──────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    store.init()
    _startup_check()
    yield


app = FastAPI(
    title="ClipStage", version=VERSION, lifespan=lifespan,
    docs_url="/docs" if ENABLE_DOCS else None,
    redoc_url=None,
    openapi_url="/openapi.json" if ENABLE_DOCS else None,
)

PUBLIC_PATHS = {"/", "/health", "/auth/login", "/auth/logout", "/auth/refresh", "/static/indexer_status.js"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

CSP = ("default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src 'self' https://fonts.gstatic.com; script-src 'self' 'unsafe-inline'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


@app.middleware("http")
async def gatekeeper(request: Request, call_next):
    path = request.url.path
    if request.method != "OPTIONS" and path not in PUBLIC_PATHS:
        # CSRF: a cross-site page cannot add a custom header without a CORS preflight,
        # which we do not grant. Login/logout are exempt (nothing to steal there).
        if request.method not in SAFE_METHODS and not request.headers.get(CSRF_HEADER):
            return JSONResponse(status_code=403, content={"detail": "Missing CSRF header"})
        token = _bearer_token(request.headers.get("authorization", "")) \
            or request.cookies.get("clipstage_access_token", "")
        if not token:
            return JSONResponse(status_code=401, content={"detail": "Login required"})
        try:
            who = await run_in_threadpool(_current_identity, token)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        if not who:
            return JSONResponse(status_code=401, content={"detail": "Login required or account expired"})
        request.state.who = who
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers.setdefault("Content-Security-Policy", CSP)
    return response


# Added AFTER the gatekeeper so CORS is the outermost layer (preflights never hit auth).
# Same-origin deployments (the default) need no CORS at all.
if ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type", CSRF_HEADER, "Authorization"],
    )


def _cookie_secure(request: Request) -> bool:
    return FORCE_SECURE_COOKIE or request.url.scheme == "https"


def _capped_expiry(session: dict, valid_through: date):
    """(seconds the access token may live, the moment the account stops being valid)."""
    valid_until = datetime.combine(valid_through + timedelta(days=1), datetime_time.min, tzinfo=timezone.utc)
    try:
        expires_in = int(session.get("expires_in", 3600))
    except (TypeError, ValueError):
        expires_in = 3600
    expires_in = max(0, min(expires_in, int((valid_until - datetime.now(timezone.utc)).total_seconds())))
    return expires_in, valid_until


def _set_session_cookies(out, request: Request, token: str, refresh_token, expires_in: int, valid_until: datetime):
    secure = _cookie_secure(request)
    out.set_cookie("clipstage_access_token", token, max_age=expires_in, httponly=True,
                   secure=secure, samesite="lax", path="/")
    if refresh_token:
        remaining = max(0, int((valid_until - datetime.now(timezone.utc)).total_seconds()))
        # path="/auth" -> the browser sends this cookie only to the auth endpoints, never with
        # clip/stream/search requests.
        out.set_cookie(REFRESH_COOKIE, str(refresh_token), max_age=min(REFRESH_COOKIE_DAYS * 86400, remaining),
                       httponly=True, secure=secure, samesite="lax", path="/auth")


def _clear_session_cookies(out, request: Request):
    secure = _cookie_secure(request)
    out.delete_cookie("clipstage_access_token", path="/", httponly=True, secure=secure, samesite="lax")
    out.delete_cookie(REFRESH_COOKIE, path="/auth", httponly=True, secure=secure, samesite="lax")


@app.post("/auth/refresh")
def refresh_session(request: Request):
    """Silent re-login: swap the refresh cookie for a fresh access token (Supabase rotates the
    refresh token on every use, so the new one is stored too). Public path because the access
    cookie is usually already expired when this is called - so the CSRF header is checked here."""
    if not request.headers.get(CSRF_HEADER):
        raise HTTPException(status_code=403, detail="Missing CSRF header")
    old_refresh = request.cookies.get(REFRESH_COOKIE, "")
    if not old_refresh:
        raise HTTPException(status_code=401, detail="No refresh token - please sign in")
    response = _supabase_request("POST", "/auth/v1/token?grant_type=refresh_token",
                                 json={"refresh_token": old_refresh})
    if response.status_code in (400, 401, 403):       # revoked / reused / expired refresh token
        out = JSONResponse(status_code=401, content={"detail": "Session ended - please sign in again"})
        _clear_session_cookies(out, request)
        return out
    if response.status_code != 200:                    # Supabase hiccup: keep the cookie, try later
        raise HTTPException(status_code=503, detail="Supabase refresh failed")
    try:
        session = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="Invalid response from Supabase") from exc
    user = session.get("user") if isinstance(session, dict) else None
    token = session.get("access_token") if isinstance(session, dict) else None
    if not token or not isinstance(user, dict) or not user.get("id"):
        raise HTTPException(status_code=503, detail="Supabase did not return a session")
    profile = _read_profile(str(user["id"]), token)
    if not profile:                                    # role removed or valid_through passed
        out = JSONResponse(status_code=403, content={"detail": "Account role or valid-through date is missing or expired"})
        _clear_session_cookies(out, request)
        return out
    role, valid_through, editor_name = profile
    expires_in, valid_until = _capped_expiry(session, valid_through)
    result = {"username": user.get("email", ""), "role": role, "expires_in": expires_in,
              "valid_through": valid_through.isoformat(), "editor_name": editor_name}
    out = JSONResponse(content=result)
    _set_session_cookies(out, request, token, session.get("refresh_token") or old_refresh, expires_in, valid_until)
    return out


@app.post("/auth/login")
def login(request: Request, payload: dict = Body(...)):
    email = str(payload.get("email", "")).strip().lower()
    password = str(payload.get("password", ""))
    if not email or not password:
        raise HTTPException(status_code=401, detail="Email and password are required")
    rl_key = (request.client.host if request.client else "?", email)
    if _login_blocked(rl_key):
        raise HTTPException(status_code=429, detail="Too many failed attempts — wait a few minutes")
    response = _supabase_request("POST", "/auth/v1/token?grant_type=password",
                                 json={"email": email, "password": password})
    if response.status_code in (400, 401):
        _login_failed(rl_key)
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    if response.status_code != 200:
        raise HTTPException(status_code=503, detail="Supabase login failed")
    try:
        session = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="Invalid response from Supabase") from exc
    user = session.get("user") if isinstance(session, dict) else None
    token = session.get("access_token") if isinstance(session, dict) else None
    if not token or not isinstance(user, dict) or not user.get("id"):
        raise HTTPException(status_code=401, detail="Supabase did not return an active account")
    profile = _read_profile(str(user["id"]), token)
    if not profile:
        raise HTTPException(status_code=403, detail="Account role or valid-through date is missing or expired")
    role, valid_through, editor_name = profile
    expires_in, valid_until = _capped_expiry(session, valid_through)
    _LOGIN_FAILS.pop(rl_key, None)
    store.audit(user.get("email", email), "login", role)
    result = {"username": user.get("email", email), "role": role, "expires_in": expires_in,
              "valid_through": valid_through.isoformat(), "editor_name": editor_name}
    out = JSONResponse(content=result)
    _set_session_cookies(out, request, token, session.get("refresh_token"), expires_in, valid_until)
    return out


@app.post("/auth/logout")
def logout(request: Request):
    token = request.cookies.get("clipstage_access_token", "")
    if token:
        with _ID_LOCK:
            _ID_CACHE.pop(hashlib.sha256(token.encode()).hexdigest(), None)
        # Also end the session at Supabase so a copied token stops working. Best effort:
        # signing out must never fail just because Supabase is unreachable.
        try:
            _supabase_request("POST", "/auth/v1/logout", token, json={})
        except Exception:
            pass
    out = JSONResponse(content={"ok": True})
    _clear_session_cookies(out, request)
    return out


@app.get("/config")
def get_config(who: Who = Depends(get_who)):
    """Deployment settings only logged-in users need (kept off the public login page)."""
    return {"smb_host": CLIPSTAGE_SMB_HOST, "platform": "windows" if IS_WINDOWS else "mac",
            "share_name": STAGING_SHARE_NAME}


@app.get("/auth/me")
def auth_me(who: Who = Depends(get_who)):
    """Who is signed in right now (from the session cookie). The UI uses this so the header
    always shows the real account and role - an admin sees the Sync Index controls without
    having to sign in again."""
    return {"username": who.email, "role": who.role, "valid_through": who.valid_through.isoformat(),
            "editor_name": who.editor_name}


# ── INDEXER CONTROL + STATUS ──────────────────────────────────────────────────

_INDEXER_PROC = None


def _pid_alive(pid) -> bool:
    if _INDEXER_PROC is not None and _INDEXER_PROC.pid == pid:
        return _INDEXER_PROC.poll() is None
    return pid_alive(pid)       # never os.kill(pid, 0): on Windows that would TERMINATE the indexer


def _read_index_status() -> dict:
    try:
        s = json.loads(INDEX_STATUS.read_text())
    except Exception:
        s = {"state": "idle"}
    now = time.time()
    s["now"] = now
    if s.get("state") == "running":
        pid = s.get("pid")
        if pid and not _pid_alive(pid):
            s["state"] = "error"
            s["message"] = "Indexer process exited unexpectedly — see indexer.log"
        elif now - s.get("updated_at", now) > 600:
            s["state"] = "stalled"
    return s


@app.get("/admin/index-status")
def index_status(who: Who = Depends(get_who)):
    return _read_index_status()


def _mounted_names() -> dict:
    if not VOLUMES_ROOT.exists():
        return {}
    return {p.name.casefold(): p.name for p in VOLUMES_ROOT.iterdir() if p.is_dir()}


@app.get("/admin/indexable-volumes")
def indexable_volumes(who: Who = Depends(get_who)):
    mounted = _mounted_names()
    return {"volumes": [v for v in SCAN_VOLUMES if v.casefold() in mounted]}


@app.post("/admin/run-indexer")
def run_indexer(volume: str = "", admin: Who = Depends(require_admin)):
    global _INDEXER_PROC
    if _read_index_status().get("state") == "running":
        raise HTTPException(status_code=409, detail="Indexer is already running")
    if not INDEXER_SCRIPT.exists():
        raise HTTPException(status_code=500, detail="indexer.py not found next to api.py")
    args = list(INDEXER_ARGS)
    scope = "All volumes"
    # `volume` may be one name or a comma list ("EDIT2,PLAYOUT") for a quick run on a few volumes.
    asked = [v.strip() for v in (volume or "").split(",") if v.strip()]
    if asked:
        by_fold = {v.casefold(): v for v in SCAN_VOLUMES}
        unknown = [a for a in asked if a.casefold() not in by_fold]
        if unknown:
            raise HTTPException(status_code=400, detail=f"Unknown volume '{', '.join(unknown)}'. "
                                                        f"Choose one of: {', '.join(SCAN_VOLUMES)}")
        want = {a.casefold() for a in asked}
        matches = [v for v in SCAN_VOLUMES if v.casefold() in want]      # canonical order
        if len(matches) < len(SCAN_VOLUMES):                             # all picked == normal full run
            args += ["--only", ",".join(matches)]
            scope = ", ".join(matches)
    try:
        INDEX_STOP_FILE.unlink()              # a stop request belongs to the run it was made for
    except OSError:
        pass
    log = open(INDEXER_LOG, "ab")
    try:
        _INDEXER_PROC = subprocess.Popen([sys.executable, str(INDEXER_SCRIPT), *args], cwd=str(BASE_DIR),
                                         stdout=log, stderr=log, **detached_popen_kwargs())
    finally:
        log.close()
    try:
        prev = json.loads(INDEX_STATUS.read_text())
    except Exception:
        prev = {}
    now = time.time()
    prev.update(state="running", phase="starting", pid=_INDEXER_PROC.pid, started_at=now, updated_at=now,
                finished_at=None, scanned=0, unchanged=0, errors=0, volumes={}, to_probe=0, probed=0,
                to_save=0, saved=0, message="", scope=scope)
    tmp = INDEX_STATUS.with_name(INDEX_STATUS.name + ".tmp")
    tmp.write_text(json.dumps(prev))
    os.replace(tmp, INDEX_STATUS)
    store.audit(admin.email, "run-indexer", scope)
    return {"started": True, "pid": _INDEXER_PROC.pid, "scope": scope}


# How long a stop request may take before the API forces the indexer to end (seconds).
STOP_GRACE_SECONDS = int(os.environ.get("CLIPSTAGE_STOP_GRACE", "30"))


def _write_forced_stop_status():
    """The indexer was ended by force, so it could not write its own final status."""
    try:
        cur = json.loads(INDEX_STATUS.read_text())
    except Exception:
        cur = {}
    if cur.get("state") not in ("running", "stalled"):
        return
    now = time.time()
    cur.update(state="stopped", phase="stopped", finished_at=now, updated_at=now,
               message="Stopped by user (forced)")
    tmp = INDEX_STATUS.with_name(INDEX_STATUS.name + ".tmp")
    tmp.write_text(json.dumps(cur))
    os.replace(tmp, INDEX_STATUS)


def _force_stop_watchdog(pid: int, grace: int):
    """Give the indexer `grace` seconds to stop by itself (it checks the stop flag many times a
    second). If it is stuck - typically waiting on an unreachable NAS - end it, but only after
    confirming the process really is indexer.py, never an unrelated process that reused the id."""
    deadline = time.time() + grace
    while time.time() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(1)
    if not _pid_alive(pid):
        return
    if "indexer.py" not in process_cmdline(pid):
        print(f"stop-indexer: pid {pid} did not stop and could not be confirmed as indexer.py - left alone")
        return
    terminate_process_tree(pid)
    for _ in range(10):
        if not _pid_alive(pid):
            break
        time.sleep(1)
    if _pid_alive(pid):
        terminate_process_tree(pid, force=True)
        time.sleep(2)
    if not _pid_alive(pid):
        _write_forced_stop_status()
        try:
            INDEX_STOP_FILE.unlink()
        except OSError:
            pass


@app.post("/admin/stop-indexer")
def stop_indexer(admin: Who = Depends(require_admin)):
    """Ask the running index to stop. Safe: nothing is pruned or deleted; the next run carries on."""
    s = _read_index_status()
    if s.get("state") not in ("running", "stalled"):
        raise HTTPException(status_code=409, detail="No index is running")
    try:
        INDEX_STOP_FILE.write_text(str(time.time()))
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not write the stop request: {e}")
    store.audit(admin.email, "stop-indexer", s.get("scope") or "")
    pid = s.get("pid")
    if pid:
        threading.Thread(target=_force_stop_watchdog, args=(int(pid), STOP_GRACE_SECONDS),
                         daemon=True).start()
    return {"stopping": True, "pid": pid, "grace_seconds": STOP_GRACE_SECONDS}


@app.get("/admin/audit")
def audit_log(limit: int = 200, admin: Who = Depends(require_admin)):
    limit = max(1, min(limit, 1000))
    rows = store._conn().execute(
        "SELECT ts, actor, action, detail FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
    return {"entries": [{"ts": r["ts"], "actor": r["actor"], "action": r["action"], "detail": r["detail"]}
                        for r in rows]}


@app.get("/admin/health")
def admin_health(admin: Who = Depends(require_admin)):
    try:
        info = backend.info()
        docs, seps = info.get("documents", 0), info.get("token_separators", [])
    except Exception as exc:
        docs, seps = f"unavailable ({exc})", []
    return {"version": VERSION, "search_engine": backend.name, "collection": COLLECTION,
            "documents": docs, "token_separators": seps,
            "mounted_volumes": sorted(_mounted_names().values()), "staging": STAGING_PATH,
            "link_mode": LINK_MODE, "max_streams": MAX_STREAMS}


@app.get("/health")
def health():
    """Public liveness probe. Deliberately reveals nothing about the deployment."""
    return {"status": "ok"}


# ── EDITORS + ACCESS RULES ────────────────────────────────────────────────────

def _is_safe_editor_name(name: object) -> bool:
    return (
        isinstance(name, str) and 0 < len(name) <= 64 and name == name.strip()
        and name not in {".", ".."}
        and not any(ch in name for ch in ("/", "\\", "\x00"))
        and not any(ord(ch) < 32 or ord(ch) == 127 for ch in name)
    )


def _editor_names() -> list:
    try:
        config = json.loads(EDITORS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=503, detail="Could not read editors.json") from exc
    if not isinstance(config, dict) or not isinstance(config.get("editors"), list):
        raise HTTPException(status_code=503, detail="editors.json must contain an editors array")
    return sorted({n for n in config["editors"] if _is_safe_editor_name(n)}, key=str.casefold)


def _authorize_editor(who: Who, editor: str) -> str:
    """Editor must be listed in editors.json. Admins may act as anyone; a personal account
    (profile has editor_name) only as itself; a shared account as any listed editor."""
    if not _is_safe_editor_name(editor) or editor not in _editor_names():
        raise HTTPException(status_code=403, detail="Unknown editor")
    if not who.is_admin and who.editor_name and who.editor_name.casefold() != editor.casefold():
        raise HTTPException(status_code=403, detail="This account may only use its own staging folder")
    return editor


@app.get("/editors")
def get_editors(who: Who = Depends(get_who)):
    names = _editor_names()
    if not who.is_admin and who.editor_name:
        names = [n for n in names if n.casefold() == who.editor_name.casefold()]
    return {"editors": names}


# ── SEARCH (the search backend does the work) ─────────────────────────────────
# Both engines give: every word must match (inside one field), only the LAST word may be a
# prefix, words can be in any order, '_' '-' '.' '(' ')' '[' ']' '+' split words, ranked by
# relevance bands -> popularity (use_count) -> newest.  See search_backend.py.

_ALL_SEP_RE = re.compile(r"[\s_\-]+")


def _highlight(text: str, query: str) -> str:
    """Fallback highlighter (used when the match was in a field other than the filename)."""
    if not text:
        return text
    result = _html.escape(text)
    for qw in [w for w in _ALL_SEP_RE.split(query.strip()) if w]:
        try:
            pattern = re.compile(rf"(?<![A-Za-z0-9])({re.escape(_html.escape(qw))})", re.IGNORECASE)
            result = pattern.sub(r"<mark>\1</mark>", result, count=1)
        except re.error:
            pass
    return result


def _filename_highlight(hit: dict, doc: dict, q: str) -> str:
    val = hit.get("hl")
    if val and _HL_START in val:
        # escape FIRST, then turn our private markers into <mark> — filenames cannot inject HTML
        return _html.escape(val).replace(_HL_START, "<mark>").replace(_HL_END, "</mark>")
    return _highlight(doc.get("filename", ""), q)


def _format_hit(doc: dict, user: dict, hl: str) -> dict:
    path = doc.get("path", "")
    u = user or {}
    return {
        "id": doc["id"], "filename": doc.get("filename", ""), "filename_hl": hl,
        "path": path, "dir": path.rsplit("/", 1)[0] if "/" in path else "",
        "size_mb": doc.get("size_mb", 0), "date": doc.get("date", ""),
        "category": doc.get("category", ""), "volume": doc.get("volume", ""),
        "folder": doc.get("folder", ""), "duration": doc.get("duration", ""),
        # SQLite is the source of truth for anything a person typed or did
        "notes": u.get("notes", doc.get("notes", "")),
        "use_count": int(u.get("use_count", doc.get("use_count") or 0) or 0),
        "tags_custom": u.get("tags_custom", doc.get("tags_custom", "")),
        "description": u.get("description", doc.get("description", "")),
        "reporter": u.get("reporter", doc.get("reporter", "")),
        "location": u.get("location", doc.get("location", "")),
        "meta_updated_by": u.get("meta_updated_by", ""),
        "meta_updated_at": u.get("meta_updated_at", ""),
        "version": int(u.get("version", 0) or 0),
    }


@app.get("/search")
def search_clips(q: str = "", sort: str = "", page: int = 1, per_page: int = 30, all: bool = True,
                 volume: str = "", folder: str = "", ext: str = "", who: Who = Depends(get_who)):
    q = q.strip()
    if not q:
        return {"hits": [], "total": 0}
    if len(q) > 200:
        raise HTTPException(status_code=400, detail="Query too long")
    try:
        res = backend.search(q, volume=volume, folder=folder, ext=ext, sort=sort, page=page,
                             per_page=per_page, want_all=all, max_results=MAX_RESULTS, num_typos=NUM_TYPOS)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Search unavailable: {exc}") from exc

    raw_hits = res["hits"]
    user = store.get_many(h["document"]["id"] for h in raw_hits)
    hits = [_format_hit(h["document"], user.get(h["document"]["id"]), _filename_highlight(h, h["document"], q))
            for h in raw_hits]
    result = {"hits": hits, "total": res["total"], "page": res["page"], "facets": res["facets"]}
    if res.get("truncated"):
        result["truncated"] = True
        result["shown"] = len(hits)
    return result


@app.get("/facets/volumes")
def facet_volumes(who: Who = Depends(get_who)):
    mounted = _mounted_names()
    return {"volumes": [v for v in SCAN_VOLUMES if v.casefold() in mounted]}


@app.get("/volumes")
def list_volumes(who: Who = Depends(get_who)):
    return {"volumes": [{"name": n} for n in sorted(_mounted_names().values())]}


# ── NOTES + METADATA (SQLite first, then mirrored to the search index) ───────

def _clean_text(value, field: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"{field} must be a string")
    value = value.strip() if field != "notes" else value
    if len(value) > limit:
        raise HTTPException(status_code=400, detail=f"{field} too long (max {limit} chars)")
    return value


@app.patch("/clip/{uid}/notes")
def update_clip_notes(uid: str, payload: dict = Body(...), who: Who = Depends(get_who)):
    if not _valid_uid(uid):
        raise HTTPException(status_code=400, detail="Invalid clip id")
    notes = _clean_text(payload.get("notes", ""), "notes", 5000)
    doc = _get_doc(uid)
    if doc is None:
        raise HTTPException(status_code=404, detail="Clip not found")
    expected = payload.get("version")
    version = store.set_notes(uid, notes, doc.get("path", ""),
                              expected if isinstance(expected, int) else None)
    if version is None:
        raise HTTPException(status_code=409, detail="Someone else changed this note — reload and try again")
    _mirror(uid, {"notes": notes})
    store.audit(who.email, "notes", uid)
    return {"ok": True, "id": uid, "notes": notes, "version": version}


@app.post("/clips/bulk-notes")
def bulk_notes(payload: dict = Body(...), who: Who = Depends(get_who)):
    ids = payload.get("ids", [])
    notes = _clean_text(payload.get("notes", ""), "notes", 5000)
    if not isinstance(ids, list) or not ids:
        raise HTTPException(status_code=400, detail="ids must be a non-empty list")
    if len(ids) > 200:
        raise HTTPException(status_code=400, detail="At most 200 clips per bulk edit")
    found = _get_docs(ids)
    ok = [u for u in dict.fromkeys(ids) if u in found]
    failed = [u for u in ids if u not in found]
    if ok:
        store.set_notes_bulk(ok, notes)
        _mirror_many([{"id": u, "notes": notes} for u in ok])
        store.audit(who.email, "bulk-notes", f"{len(ok)} clips")
    return {"ok": ok, "failed": failed, "notes": notes}


@app.post("/meta/{uid}")
def update_meta(uid: str, payload: dict = Body(...), who: Who = Depends(get_who)):
    if not _valid_uid(uid):
        raise HTTPException(status_code=400, detail="Invalid clip id")
    doc = _get_doc(uid)
    if doc is None:
        raise HTTPException(status_code=404, detail="Clip not found")
    values = {
        "tags_custom": _clean_text(payload.get("tags", ""), "tags", 500),
        "description": _clean_text(payload.get("description", ""), "description", 1000),
        "reporter": _clean_text(payload.get("reporter", ""), "reporter", 200),
        "location": _clean_text(payload.get("location", ""), "location", 200),
    }
    store.set_meta(uid, values, who.email, doc.get("path", ""))
    _mirror(uid, values)
    store.audit(who.email, "meta", uid)
    return {"ok": True, "id": uid, **values}


# ── SOFT DELETE / RESTORE (admin) ─────────────────────────────────────────────

@app.delete("/clip/{uid}")
def remove_clip(uid: str, admin: Who = Depends(require_admin)):
    """Hides a clip from search. The file and its notes are untouched, and a re-index will
    NOT bring it back. Undo with POST /admin/restore/{uid}."""
    if not _valid_uid(uid):
        raise HTTPException(status_code=400, detail="Invalid clip id")
    doc = _get_doc(uid)
    if doc is None:
        raise HTTPException(status_code=404, detail="Clip not found")
    store.set_hidden(uid, True, doc.get("path", ""))
    _mirror(uid, {"hidden": True})
    store.audit(admin.email, "hide", f"{uid} {doc.get('path', '')}")
    return {"ok": True, "id": uid}


@app.post("/admin/restore/{uid}")
def restore_clip(uid: str, admin: Who = Depends(require_admin)):
    if not _valid_uid(uid):
        raise HTTPException(status_code=400, detail="Invalid clip id")
    if _get_doc(uid) is None:
        raise HTTPException(status_code=404, detail="Clip not found")
    store.set_hidden(uid, False)
    _mirror(uid, {"hidden": False})
    store.audit(admin.email, "restore", uid)
    return {"ok": True, "id": uid}


@app.get("/admin/hidden")
def list_hidden(admin: Who = Depends(require_admin)):
    clips, total = backend.list_hidden(250)
    return {"clips": clips, "total": total}


# ── LOCKS (who is editing / using a clip right now) ───────────────────────────

@app.post("/lock/{uid}")
def lock_clip(uid: str, payload: dict = Body(...), who: Who = Depends(get_who)):
    if not _valid_uid(uid):
        raise HTTPException(status_code=400, detail="Invalid clip id")
    editor = _authorize_editor(who, str(payload.get("editor", "")).strip())
    other = store.acquire_lock(uid, editor)
    if other:
        return {"conflict": True, "locked_by": other["editor"],
                "since": time.strftime("%H:%M", time.localtime(other["since"]))}
    return {"conflict": False}


@app.delete("/lock/{uid}")
def unlock_clip(uid: str, payload: dict = Body(default={}), who: Who = Depends(get_who)):
    if not _valid_uid(uid):
        raise HTTPException(status_code=400, detail="Invalid clip id")
    editor = _authorize_editor(who, str(payload.get("editor", "")).strip())
    store.release_lock(uid, editor)
    return {"ok": True}


@app.post("/check-conflicts")
def check_conflicts(payload: dict = Body(...), who: Who = Depends(get_who)):
    editor = _authorize_editor(who, str(payload.get("editor", "")).strip())
    paths = payload.get("paths", [])
    if not isinstance(paths, list) or len(paths) > 500:
        raise HTTPException(status_code=400, detail="paths must be a list (max 500)")
    by_id = {clip_id_for(p): p for p in paths if isinstance(p, str)}
    found = store.conflicts(by_id.keys(), editor)
    conflicts = [{"path": by_id[c["clip_id"]], "locked_by": c["editor"]} for c in found]
    return {"has_conflicts": bool(conflicts), "conflicts": conflicts}


# ── THUMBNAIL ─────────────────────────────────────────────────────────────────

@app.get("/thumb/{uid}")
def get_thumb(uid: str, who: Who = Depends(get_who)):
    if not _valid_uid(uid):
        raise HTTPException(status_code=400, detail="Invalid uid")
    thumb = THUMB_DIR / f"{uid}.jpg"
    try:
        ok = thumb.stat().st_size > 0
    except OSError:
        ok = False
    if not ok:
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    return FileResponse(str(thumb), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


# ── STAGE ─────────────────────────────────────────────────────────────────────

def _read_link(p) -> str:
    """The link's raw one-hop target in the SAME spelling the indexer stores (see norm_path)."""
    return norm_path(os.readlink(str(p)))


def _link_points_to(dst: Path, src: Path) -> bool:
    try:
        return dst.is_symlink() and _read_link(dst) == norm_path(src)
    except OSError:
        return False


def _volume_of(src: Path) -> str:
    try:
        return src.relative_to(VOLUMES_ROOT).parts[0]
    except (ValueError, IndexError):
        return src.parts[2] if len(src.parts) > 2 else "?"


def _create_link(src: Path, dst: Path):
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    if LINK_MODE == "hardlink":
        os.link(src, dst)
    elif LINK_MODE == "copy":
        shutil.copy2(src, dst)
    else:
        os.symlink(src, dst)


def _destination(folder: Path, src: Path) -> Path:
    """Where to put a staged clip. Re-staging the same clip reuses its link; a DIFFERENT clip
    with the same file name gets a numbered name instead of silently replacing the first one."""
    dst = folder / src.name
    n = 2
    while dst.exists() or dst.is_symlink():
        if _link_points_to(dst, src):
            return dst
        dst = folder / f"{src.stem}__{n}{src.suffix}"
        n += 1
    return dst


@app.post("/stage")
def stage_clips(payload: dict = Body(...), who: Who = Depends(get_who)):
    editor = _authorize_editor(who, str(payload.get("editor", "")).strip())
    clip_paths = payload.get("paths", [])
    if not clip_paths or not isinstance(clip_paths, list):
        raise HTTPException(status_code=400, detail="No clips selected")
    if len(clip_paths) > 200:
        raise HTTPException(status_code=400, detail="At most 200 clips per stage")

    editor_staging = Path(STAGING_PATH) / editor
    editor_staging.mkdir(parents=True, exist_ok=True)

    # Only clips that are in the index (and not hidden) can be staged — the client never
    # gets to name an arbitrary file on the server.
    wanted = {clip_id_for(p): p for p in clip_paths if isinstance(p, str) and p}
    docs = _get_docs(list(wanted))

    staged, errors = [], []
    for uid, clip_path in wanted.items():
        doc = docs.get(uid)
        if not doc or doc.get("path") != clip_path or doc.get("hidden"):
            errors.append({"path": clip_path, "error": "Clip is not in the archive index"})
            continue
        src = Path(clip_path)
        if not src.exists():
            vol_part = _volume_of(src)
            if not (VOLUMES_ROOT / vol_part).exists():
                errors.append({"path": clip_path, "error": f"Volume {VOLUMES_ROOT}/{vol_part} is not mounted"})
            else:
                errors.append({"path": clip_path, "error": "File not found in archive"})
            continue
        try:
            dst = _destination(editor_staging, src)
            if not _link_points_to(dst, src):
                _create_link(src, dst)
            staged.append(dst.name)
            count = store.bump_use_count(uid, clip_path)
            _mirror(uid, {"use_count": count})
        except Exception as exc:
            errors.append({"path": clip_path, "error": str(exc)})
    store.audit(who.email, "stage", f"{editor}: {len(staged)} ok, {len(errors)} failed")
    return {"staged": staged, "errors": errors, "staging_folder": str(editor_staging),
            "count": len(staged), "link_mode": LINK_MODE}


@app.get("/stage/{editor}")
def list_staging(editor: str, who: Who = Depends(get_who)):
    editor = _authorize_editor(who, editor)
    editor_staging = Path(STAGING_PATH) / editor
    if not editor_staging.exists():
        return {"clips": []}
    clips = []
    for i in editor_staging.iterdir():
        if not (i.is_file() or i.is_symlink()):
            continue
        try:
            mtime = i.lstat().st_mtime
        except OSError:
            mtime = 0
        uid = ""
        link_target = ""
        try:
            # raw one-hop target (NOT resolve()) — that is the exact string the indexer hashed
            target = _read_link(i) if i.is_symlink() else norm_path(i)
            uid = clip_id_for(target)
            if i.is_symlink():
                link_target = target      # the editor app links/drags this real NAS path (clipstage-fcp-drag)
        except OSError:
            pass
        clips.append({"name": i.name, "exists": i.exists(), "uid": uid, "mtime": mtime,
                      "target": link_target})
    return {"clips": clips, "editor": editor}


@app.delete("/stage/{editor}")
def clear_staging(editor: str, who: Who = Depends(get_who)):
    editor = _authorize_editor(who, editor)
    editor_staging = Path(STAGING_PATH) / editor
    if not editor_staging.exists():
        return {"cleared": 0}
    count = 0
    for item in editor_staging.iterdir():
        if item.is_file() or item.is_symlink():
            item.unlink()
            count += 1
    store.audit(who.email, "clear-staging", f"{editor}: {count}")
    return {"cleared": count}


@app.get("/staging/view/{editor}", response_class=HTMLResponse)
def browse_staging(editor: str, who: Who = Depends(get_who)):
    """HTML page listing the editor's staged clips (opens in a new tab)."""
    editor = _authorize_editor(who, editor)
    editor_staging = Path(STAGING_PATH) / editor
    editor_staging.mkdir(parents=True, exist_ok=True)

    def mtime(x: Path) -> float:
        try:
            return x.lstat().st_mtime
        except OSError:
            return 0

    items = sorted([i for i in editor_staging.iterdir() if i.is_file() or i.is_symlink()],
                   key=mtime, reverse=True)
    esc = _html.escape
    rows = ""
    for item in items:
        exists = item.exists()
        size_mb = round(item.stat().st_size / (1024 * 1024), 1) if exists else 0
        try:
            target = _read_link(item) if item.is_symlink() else "—"
        except OSError:
            target = "—"
        status = "✅ OK" if exists else "⚠️ Source missing"
        rows += (f"<tr><td>{esc(item.name)}</td><td>{size_mb} MB</td>"
                 f'<td style="color:#7ba8d4;font-size:11px">{esc(target)}</td><td>{status}</td></tr>')
    if not rows:
        rows = ('<tr><td colspan="4" style="color:#7ba8d4;text-align:center;padding:32px">'
                "No clips staged yet</td></tr>")

    if IS_WINDOWS and CLIPSTAGE_SMB_HOST:
        smb_path = f"\\\\{CLIPSTAGE_SMB_HOST}\\{STAGING_SHARE_NAME}\\{editor}"    # what an editor types in Explorer
    else:
        smb_path = f"{STAGING_PATH}/{editor}"
    e_editor = esc(editor)
    page = f"""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<title>Staging — {e_editor}</title>
<style>
  body {{ background:#001a3a; color:#c8dff0; font-family:system-ui,sans-serif; margin:0; padding:24px; }}
  h1 {{ color:#e8a020; font-size:18px; margin-bottom:4px; }}
  .sub {{ color:#7ba8d4; font-size:12px; margin-bottom:20px; }}
  .path-box {{ background:#002155; border:1px solid #0040b0; border-radius:8px; padding:12px 16px;
    margin-bottom:20px; font-size:12px; display:flex; align-items:center; gap:12px; flex-wrap:wrap; }}
  .path-box code {{ color:#a0cfff; font-family:monospace; font-size:13px; }}
  .copy-btn {{ background:#0040b0; border:none; color:#fff; padding:6px 14px; border-radius:5px;
    cursor:pointer; font-size:12px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th {{ text-align:left; color:#e8a020; padding:8px 12px; border-bottom:1px solid #0040b0;
    font-size:11px; font-family:monospace; letter-spacing:1px; }}
  td {{ padding:9px 12px; border-bottom:1px solid rgba(0,64,176,0.3); word-break:break-all; }}
  .count {{ color:#7ba8d4; font-size:12px; margin-top:12px; }}
</style>
</head><body>
<h1>📂 {e_editor} — Staging Folder</h1>
<div class="sub">{len(items)} clip(s) staged</div>
<div class="path-box">
  <span>Folder path:</span><code id="fpath">{esc(smb_path)}</code>
  <button class="copy-btn" id="copyBtn">Copy Path</button>
</div>
<table>
  <thead><tr><th>FILENAME</th><th>SIZE</th><th>SOURCE (NAS PATH)</th><th>STATUS</th></tr></thead>
  <tbody>{rows}</tbody>
</table>
<div class="count">{len(items)} file(s) in {esc(smb_path)}/</div>
<script>
document.getElementById('copyBtn').onclick = function () {{
  navigator.clipboard.writeText(document.getElementById('fpath').textContent); this.textContent = 'Copied!';
}};
setTimeout(() => location.reload(), 10000);
</script>
</body></html>"""
    return HTMLResponse(content=page)


# ── VIDEO STREAM (preview) ────────────────────────────────────────────────────

_FFMPEG_BIN = FFMPEG        # CLIPSTAGE_FFMPEG override -> PATH -> Homebrew folders (see clipstage_config)
_STREAM_SLOTS = threading.BoundedSemaphore(MAX_STREAMS)


@app.get("/stream/{uid}")
def stream_clip(uid: str, who: Who = Depends(get_who)):
    if not _valid_uid(uid):
        raise HTTPException(status_code=400, detail="Invalid uid")
    if not _FFMPEG_BIN:
        raise HTTPException(status_code=500, detail="ffmpeg not found on server PATH")
    doc = _get_doc(uid)
    if not doc or doc.get("hidden"):
        raise HTTPException(status_code=404, detail="Clip not found")
    clip_path = doc.get("path", "")
    if not clip_path or not Path(clip_path).exists():
        raise HTTPException(status_code=404, detail="File not found on disk")
    if not _STREAM_SLOTS.acquire(blocking=False):
        raise HTTPException(status_code=429, detail=f"{MAX_STREAMS} previews are already playing — try again shortly")

    def generate():
        cmd = [_FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", clip_path,
               "-t", "300", "-vf", "scale=1280:-2",
               "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
               "-c:a", "aac", "-b:a", "128k",
               "-movflags", "frag_keyframe+empty_moov+faststart", "-f", "mp4", "pipe:1"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        sent_bytes = 0
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                sent_bytes += len(chunk)
                yield chunk
        finally:
            try:
                proc.kill()
            except Exception:
                pass
            proc.wait()
            try:
                stderr_out = proc.stderr.read() if proc.stderr else b""
            except Exception:
                stderr_out = b""
            for pipe in (proc.stdout, proc.stderr):
                try:
                    pipe.close()
                except Exception:
                    pass
            _STREAM_SLOTS.release()
            if sent_bytes == 0:
                print(f"[stream] ffmpeg produced 0 bytes for uid={uid} rc={proc.returncode}\n"
                      f"{stderr_out.decode(errors='replace')[-2000:]}", flush=True)

    # RFC 5987 form: a plain filename="…" header crashes on Tamil/Hindi/Bengali names.
    filename = doc.get("filename", "clip.mp4").replace('"', "").replace("\r", "").replace("\n", "")
    disposition = f"inline; filename*=UTF-8''{quote(filename)}"
    return StreamingResponse(generate(), media_type="video/mp4", headers={"Content-Disposition": disposition})


# ── STATIC ────────────────────────────────────────────────────────────────────

STATIC_DIR.mkdir(parents=True, exist_ok=True)
THUMB_DIR.mkdir(parents=True, exist_ok=True)


@app.get("/static/indexer_status.js")
def indexer_status_js():
    """Served from static/ if present, otherwise straight from the checkout root."""
    # Newest copy wins, so an old file left in static/ can never hide an update.
    cands = [p for p in (STATIC_DIR / "indexer_status.js", BASE_DIR / "indexer_status.js") if p.exists()]
    if not cands:
        raise HTTPException(status_code=404, detail="indexer_status.js not found")
    f = max(cands, key=lambda p: p.stat().st_mtime)
    return FileResponse(str(f), media_type="application/javascript", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _ui_file() -> Path:
    """index.html from static/ or next to api.py - whichever was modified last, so a stale
    copy in static/ can never hide an update (no manual copy step needed)."""
    cands = [p for p in (STATIC_DIR / "index.html", BASE_DIR / "index.html") if p.exists()]
    if not cands:
        raise HTTPException(status_code=500, detail="index.html not found")
    return max(cands, key=lambda p: p.stat().st_mtime)


@app.get("/")
def serve_index():
    index = _ui_file()
    page = index.read_text(encoding="utf-8")
    # The SMB host is NOT baked into the public login page any more; the UI reads /config after login.
    page = page.replace("__CLIPSTAGE_SMB_HOST__", '""')
    return Response(content=page, media_type="text/html",
                    headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache",
                             "Expires": "0"})
