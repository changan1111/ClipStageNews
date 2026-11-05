"""
ClipStage v5.0 — Multi-Volume Archive Indexer (fast + live status + safety guards)

Scans configured NAS volumes and records, per video file:
    volume → the NAS share name        e.g. EDIT2
    folder → top-level folder in volume e.g. KARTHIK_DND

What's new vs the old version
    * Parallel directory scan (SCAN_THREADS workers across ALL volumes) —
      SMB is latency-bound, so this is the biggest speed-up.
    * ffprobe runs in a thread pool (PROBE_THREADS) and only for files that
      are new/changed. Files ffprobe can't read are remembered (probed=True)
      so they are NOT re-probed on every run.
    * Unchanged files are skipped entirely (no re-import).
    * --prune compares against what the scan just saw — no per-file exists()
      call over the network. Volumes whose scan had errors are never pruned.
    * Writes index_status.json (state / phase / progress / last run) so the
      web UI can show live progress, running time and last-run info — with
      full-archive runs and single-volume (--only) runs tracked separately
      (last_full_* vs last_partial[VOLUME]) so neither overwrites the other.
    * --force rebuilds the search collection. Notes / use_count / hidden live in SQLite
      (store.py), so nothing user-typed is ever lost by a rebuild.
    * Search engine: SEARCH_BACKEND=typesense (Mac default, key in TYPESENSE_KEY) or sqlite (Windows default).

Usage:
    python indexer.py            index / delta update
    python indexer.py --dry      count files only, nothing written
    python indexer.py --force    wipe index and rebuild (keeps notes/use_count)
    python indexer.py --prune    also delete index entries whose file is gone
    python indexer.py --full     ignore the folder-change cache, rescan everything
    python indexer.py --only EDIT2   scan just one volume (handy for benchmarking)
    python indexer.py --only EDIT2,PLAYOUT   scan a few volumes (comma list, or repeat --only)
    python indexer.py --reprobe  retry ffprobe on clips that came back blank before
    python indexer.py --restore backups/clips_YYYYmmdd_HHMMSS.jsonl   put a snapshot back
    python indexer.py --prune --allow-mass-prune   override the prune safety limit

Safety built in (v4.0)
    * --force first writes backups/clips_<time>.jsonl (newest five kept). If the backup
      cannot be written the rebuild is ABORTED before anything is deleted.
    * --prune is decided PER VOLUME. If more than CLIPSTAGE_PRUNE_MAX_PERCENT of a volume's
      clips (and more than 50) look missing, or the scan saw nothing at all, that volume is
      NOT pruned and a warning is recorded (shown in the UI, alerted by the nightly job).

Quick mode: after a full scan the indexer remembers each folder's modified-time.
On the next run, folders whose time hasn't changed are NOT re-listed (no per-file
stat over SMB), which is where nearly all the time goes. A full rescan is forced
every FULL_RESCAN_DAYS days, and whenever you pass --full / --force.
"""

import os
import sys
import time
import json
import queue
import shutil
import signal
import threading
import unicodedata
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import clipstage_dotenv  # noqa: F401  (loads .env first - must stay above the imports below)
import store
from search_backend import SCHEMA, TOKEN_SEPARATORS, USER_FIELDS, get_backend  # noqa: F401
from clipstage_config import (COLLECTION, EXCLUDE_FOLDERS as _BASE_EXCLUDES, FFPROBE as _CFG_FFPROBE,
                              INDEX_STOP_FILE, SCAN_VOLUMES, VIDEO_EXTENSIONS, VOLUMES_ROOT, clip_uid,
                              norm_path, pid_alive)

# ─── CONFIG ───────────────────────────────────────────────────────────────────

# Volumes, extensions, folder excludes, collection name and the clip-ID hash come from
# clipstage_config.py (shared with api.py and generate_thumbs.py).
EXCLUDE_FOLDERS = set(_BASE_EXCLUDES)

# Final Cut Pro libraries hold thousands of render/proxy files that clutter a clip
# archive and slow the scan. Set True to skip them (run once with --full --prune
# afterwards to also remove them from the index).
SKIP_FCP_RENDER_FOLDERS = False
if SKIP_FCP_RENDER_FOLDERS:
    EXCLUDE_FOLDERS |= {"render files", "transcoded media", "proxy media",
                        "optimized media", "analysis files", ".fcpcache"}

FULL_RESCAN_DAYS = 7
DIRCACHE_FILE = Path(__file__).parent / "index_dircache.json"

SCAN_THREADS = int(os.environ.get("CLIPSTAGE_SCAN_THREADS", "16"))
PROBE_THREADS = int(os.environ.get("CLIPSTAGE_PROBE_THREADS", "8"))
PROBE_TIMEOUT = 10          # seconds per ffprobe call
BATCH_SIZE = 500
# Progress is saved to the index every CHECKPOINT clips while probing, so a crash, restart or Stop
# during a long first run keeps everything finished so far (the next run skips those clips).
CHECKPOINT = max(100, int(os.environ.get("CLIPSTAGE_CHECKPOINT", "2000")))

STATUS_FILE = Path(__file__).parent / "index_status.json"

# ──────────────────────────────────────────────────────────────────────────────

# launchd / services run with a minimal PATH, so also try Homebrew locations.
FFPROBE = _CFG_FFPROBE      # CLIPSTAGE_FFPROBE override -> PATH -> Homebrew folders (clipstage_config)

# One code base, two search engines (Typesense on Mac, SQLite FTS5 on Windows): see search_backend.py.
# SCHEMA / TOKEN_SEPARATORS / USER_FIELDS live there and are re-exported above for older callers.
backend = get_backend(120)      # exports / big imports can be slow


# ─── STATUS FILE (read by the API → shown in the UI) ─────────────────────────

DRY = False
REPROBE = False
ALLOW_MASS_PRUNE = False

# Prune safety: an empty / half-mounted volume must never wipe the index. Per volume,
# pruning is refused when more than this % of its indexed clips look missing (up to
# PRUNE_MIN_ALLOWED deletions are always fine - normal housekeeping).
PRUNE_MAX_PERCENT = float(os.environ.get("CLIPSTAGE_PRUNE_MAX_PERCENT", "5"))
PRUNE_MIN_ALLOWED = 50
WARNINGS: list = []                      # surfaced in the UI via status "last_warning"
BACKUP_DIR = Path(__file__).parent / "backups"
_slock = threading.Lock()
_last_write = 0.0
try:
    _status: dict = json.loads(STATUS_FILE.read_text())
except Exception:
    _status = {}


def set_status(force: bool = False, **kw):
    """Merge fields into the status file. Throttled to ~1 write / 1.5 s."""
    global _last_write
    if DRY:
        return
    with _slock:
        _status.update(kw)
        now = time.time()
        _status["updated_at"] = now
        if force or now - _last_write >= 1.5:
            tmp = STATUS_FILE.with_name(STATUS_FILE.name + ".tmp")
            tmp.write_text(json.dumps(_status))
            os.replace(tmp, STATUS_FILE)       # atomic — readers never see half a file
            _last_write = now


def _pid_alive(pid) -> bool:
    return pid_alive(pid)       # never os.kill(pid, 0): on Windows that would TERMINATE the process


# ─── STOP REQUESTS (Stop button in the web page, `clipstage stop-index`, SIGTERM) ─────────────
# A stop is COOPERATIVE and SAFE: the run ends at the next checkpoint, nothing is pruned or deleted,
# and the folder cache is not updated - so the next run simply picks up where the index stands.

class IndexerStopped(Exception):
    """The run was stopped on request (not an error)."""


_STOP_EVENT = threading.Event()
_stop_probe = {"t": 0.0}


def stop_requested() -> bool:
    if _STOP_EVENT.is_set():
        return True
    now = time.time()
    if now - _stop_probe["t"] >= 0.5:               # one stat() every half second at most
        _stop_probe["t"] = now
        if INDEX_STOP_FILE.exists():
            _STOP_EVENT.set()
    return _STOP_EVENT.is_set()


def check_stop():
    """Call from the main thread at checkpoints: ends the run if a stop was requested."""
    if stop_requested():
        set_status(True, phase="stopping")
        raise IndexerStopped()


def _clear_stale_stop_flag():
    """A leftover flag from an earlier, crashed run must not stop a new run. A request made
    within the last minute is real (the Stop button may be pressed while this process starts)."""
    try:
        if INDEX_STOP_FILE.exists() and time.time() - INDEX_STOP_FILE.stat().st_mtime > 60:
            INDEX_STOP_FILE.unlink()
    except OSError:
        pass


# ─── SEARCH INDEX HELPERS ────────────────────────────────────────────────────

def setup_collection(force=False):
    backend.setup(force)


def backup_existing(existing: dict, keep: int = 5):
    """Write the current index to backups/ before a destructive --force rebuild.
    Raises (and so aborts the rebuild) if the snapshot cannot be written."""
    if not existing:
        return None
    BACKUP_DIR.mkdir(exist_ok=True)
    f = BACKUP_DIR / f"clips_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
    with open(f, "w", encoding="utf-8") as fh:
        for d in existing.values():
            fh.write(json.dumps(d, ensure_ascii=False) + "\n")
    for old in sorted(BACKUP_DIR.glob("clips_*.jsonl"))[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass
    print(f"Backup written: {f} ({len(existing):,} clips)")
    return f


def restore_backup(path: Path) -> int:
    """python indexer.py --restore backups/clips_YYYYmmdd_HHMMSS.jsonl
    Upserts every document in the snapshot (same IDs are overwritten), then re-applies the
    live notes / usage / hidden flags from SQLite so a restore can never roll user data back."""
    setup_collection(force=False)
    total, batch = 0, []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            batch.append(json.loads(line))
            if len(batch) >= BATCH_SIZE:
                backend.upsert(batch, "upsert")
                total += len(batch)
                batch = []
    if batch:
        backend.upsert(batch, "upsert")
        total += len(batch)
    print(f"Restored {total:,} clips from {path}")
    resync_user_data(_load_existing_docs())
    return total


def _load_existing_docs() -> dict:
    """Export existing docs → {id: doc}."""
    try:
        return backend.export_all()
    except Exception:
        return {}


# ─── ffprobe ─────────────────────────────────────────────────────────────────

def get_duration(fpath: str):
    """
    Returns (duration_str, definitive).
    definitive=False → tool problem (ffprobe couldn't be launched): try again next run.
    definitive=True  → we got an answer, or it timed out / is unreadable (R3D/BRAW):
                       remember it and don't re-probe unless the file changes
                       (use --reprobe to retry).
    """
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "error",
             "-show_entries", "format=duration:stream=duration",
             "-of", "json", fpath],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return "", True          # too slow to read — remember it, don't retry every run
    except Exception:
        return "", False         # tool problem (ffprobe missing etc.) — retry next run

    if r.returncode != 0:
        return "", True
    try:
        data = json.loads(r.stdout)
    except Exception:
        return "", True

    candidates = [(data.get("format") or {}).get("duration")]
    candidates += [s.get("duration") for s in data.get("streams", [])]
    for raw in candidates:
        try:
            secs = float(raw)
        except (TypeError, ValueError):
            continue
        if secs > 0:
            h, m, s = int(secs // 3600), int(secs % 3600 // 60), int(secs % 60)
            return f"{h:02d}:{m:02d}:{s:02d}", True
    return "", True


# ─── PHASE 1: PARALLEL SCAN ──────────────────────────────────────────────────

def _load_dircache() -> dict:
    try:
        return json.loads(DIRCACHE_FILE.read_text())
    except Exception:
        return {}


def _save_dircache(dirs: dict, full_at: float):
    tmp = DIRCACHE_FILE.with_name(DIRCACHE_FILE.name + ".tmp")
    tmp.write_text(json.dumps({"full_at": full_at, "dirs": dirs}))
    os.replace(tmp, DIRCACHE_FILE)


class ScanContext:
    def __init__(self, roots, existing, force, dircache, full, full_vols=frozenset()):
        self.existing = existing
        self.user = store.all_user_rows()      # notes / use_count / hidden from SQLite
        self.force = force
        self.full = full                       # True → ignore the folder cache
        self.full_vols = set(full_vols)        # volumes due for their periodic full rescan
        self.dircache = dircache               # {dir: [mtime_ns, [subdir names]]}
        self.newcache = {}                     # rebuilt during this scan
        self.lock = threading.Lock()
        self.vols = {r.name: {"docs": [], "seen": set(), "ok": True} for r in roots}
        self.probe = []                        # docs that still need a duration
        self.dir_ids = {}                      # dir → [doc ids] (for skipped folders)
        if not full:
            for uid, d in existing.items():
                self.dir_ids.setdefault(d.get("path", "").rsplit("/", 1)[0], []).append(uid)
        self.stats = {"scanned": 0, "unchanged": 0, "errors": 0, "reused": 0,
                      "dirs_skipped": 0, "dirs_listed": 0, "broken_links": 0,
                      "volumes": {r.name: 0 for r in roots}}

    def push(self, force=False):
        with self.lock:
            snap = dict(scanned=self.stats["scanned"],
                        unchanged=self.stats["unchanged"],
                        errors=self.stats["errors"],
                        volumes=dict(self.stats["volumes"]))
        set_status(force, **snap)


def _stat_file(entry: os.DirEntry):
    """
    Returns (stat_result, path) or None for a broken symlink.
    Some SMB shares list names in a different Unicode form (NFC/NFD) than they
    accept back — that made Hindi/Bengali filenames fail with 'No such file'.
    We retry with the other forms and keep whichever one works.
    """
    try:
        return entry.stat(), entry.path
    except FileNotFoundError:
        if entry.is_symlink():
            return None                        # dangling link (e.g. FCP 'Original Media')
        for form in ("NFC", "NFD"):
            alt = unicodedata.normalize(form, entry.path)
            if alt != entry.path:
                try:
                    return os.stat(alt), alt
                except OSError:
                    pass
        raise


def _handle_file(ctx: ScanContext, vol: str, top: str, entry: os.DirEntry, name: str, ext: str):
    found = _stat_file(entry)
    if found is None:
        with ctx.lock:
            ctx.stats["broken_links"] += 1
        return
    st, path = found
    path = norm_path(path)         # one spelling everywhere (forward slashes on Windows) - the clip ID hashes it
    size_mb = round(st.st_size / (1024 * 1024), 2)
    mod_date = time.strftime("%Y-%m-%d", time.localtime(st.st_mtime))
    uid = clip_uid(path)
    v = ctx.vols[vol]
    v["seen"].add(uid)

    prev = ctx.existing.get(uid)
    same = bool(prev) and prev.get("size_mb") == size_mb and prev.get("date") == mod_date
    complete = same and prev.get("volume") and prev.get("folder") \
        and prev.get("hidden") is not None \
        and (prev.get("duration") or (prev.get("probed") and not REPROBE))

    with ctx.lock:
        ctx.stats["scanned"] += 1
        ctx.stats["volumes"][vol] += 1
        n = ctx.stats["scanned"]

    if complete and not ctx.force:
        with ctx.lock:
            ctx.stats["unchanged"] += 1        # nothing to write for this one
    else:
        doc = {
            "id": uid,
            "filename": name,
            "path": path,
            "size_mb": size_mb,
            "date": mod_date,
            "category": top,                   # backward compat
            "volume": vol,
            "folder": top,
            "ext": ext.lstrip(".").upper(),
            "duration": "",
        }
        # User data comes from SQLite (survives any rebuild). Fall back to what the old
        # document held, so a rewrite of a changed file can never wipe a note.
        u = ctx.user.get(uid) or {}
        for k in USER_FIELDS:
            val = u.get(k) or (prev or {}).get(k) or ""
            if val:
                doc[k] = val
        uc = u.get("use_count") or (prev or {}).get("use_count") or 0
        if uc:
            doc["use_count"] = int(uc)
        doc["hidden"] = bool(u.get("hidden"))
        if same and prev.get("duration"):
            doc["duration"] = prev["duration"]  # reuse — skip ffprobe
            doc["probed"] = True
            with ctx.lock:
                ctx.stats["reused"] += 1
        elif same and prev.get("probed") and not REPROBE:
            doc["probed"] = True               # unreadable last time, file unchanged
        else:
            ctx.probe.append(doc)              # new / changed → needs ffprobe

        v["docs"].append(doc)

    if n % 200 == 0:
        ctx.push()


def _process_dir(ctx: ScanContext, q: "queue.Queue", vol: str, top, d: str):
    d = norm_path(d)               # folder-cache keys and indexed paths must use the same spelling
    v = ctx.vols[vol]
    try:
        dmt = os.stat(d).st_mtime_ns           # read BEFORE listing: if it changes
    except OSError as e:                       # mid-scan we'll simply rescan next time
        v["ok"] = False
        with ctx.lock:
            ctx.stats["errors"] += 1
        print(f"  Cannot read folder {d}: {e}")
        return

    # ── quick mode: folder unchanged since last full listing → skip it ──
    cached = None if (ctx.full or vol in ctx.full_vols) else ctx.dircache.get(d)
    if cached and cached[0] == dmt:
        ids = ctx.dir_ids.get(d, ())
        v["seen"].update(ids)
        with ctx.lock:
            ctx.stats["dirs_skipped"] += 1
            ctx.stats["scanned"] += len(ids)
            ctx.stats["unchanged"] += len(ids)
            ctx.stats["volumes"][vol] += len(ids)
        ctx.newcache[d] = cached
        for sub in cached[1]:
            q.put((vol, top if top is not None else sub, os.path.join(d, sub)))
        return

    with ctx.lock:
        ctx.stats["dirs_listed"] += 1
    subdirs, clean = [], True
    try:
        with os.scandir(d) as it:
            for entry in it:
                try:
                    name = entry.name
                    if entry.is_dir(follow_symlinks=False):
                        if not name.startswith(".") and name.lower() not in EXCLUDE_FOLDERS:
                            subdirs.append(name)
                            q.put((vol, top if top is not None else name, entry.path))
                        continue
                    if name.startswith("._"):  # macOS AppleDouble junk on SMB
                        continue
                    ext = os.path.splitext(name)[1].lower()
                    if ext not in VIDEO_EXTENSIONS:
                        continue
                    _handle_file(ctx, vol, top if top is not None else "(root)",
                                 entry, name, ext)
                except Exception as e:
                    clean = False
                    with ctx.lock:
                        ctx.stats["errors"] += 1
                    print(f"  SKIP {entry.name}: {e}")
    except OSError as e:
        clean = False
        v["ok"] = False                        # incomplete → never prune this volume
        with ctx.lock:
            ctx.stats["errors"] += 1
        print(f"  Cannot read folder {d}: {e}")

    if clean:                                  # only trust folders we listed without errors
        ctx.newcache[d] = [dmt, subdirs]


def scan_all(roots, existing, force, dircache, full, full_vols=frozenset()) -> ScanContext:
    ctx = ScanContext(roots, existing, force, dircache, full, full_vols)
    q: "queue.Queue" = queue.Queue()
    for r in roots:
        q.put((r.name, None, str(r)))

    def worker():
        while True:
            item = q.get()
            if item is None:
                q.task_done()
                return
            try:
                if not stop_requested():             # after a stop request the queue just drains quickly
                    _process_dir(ctx, q, *item)
            except Exception as e:
                with ctx.lock:
                    ctx.stats["errors"] += 1
                print(f"  worker error: {e}")
            finally:
                q.task_done()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(SCAN_THREADS)]
    for t in threads:
        t.start()
    q.join()                                   # all dirs (incl. discovered ones) done
    for _ in threads:
        q.put(None)
    for t in threads:
        t.join()
    ctx.push(True)
    return ctx


# ─── PHASE 2: PARALLEL ffprobe ───────────────────────────────────────────────

_noprobe_warned: list = []


def probe_all(todo: list, base: int = 0, total: int = 0):
    """Read durations. base/total keep the progress bar cumulative when called once per checkpoint."""
    if not todo:
        return
    if not FFPROBE:
        if not _noprobe_warned:
            _noprobe_warned.append(True)                 # once per run, not once per checkpoint
            print("WARNING: ffprobe not found — durations skipped (install ffmpeg).")
            set_status(True, message="ffprobe not found — durations skipped")
        return

    set_status(True, phase="probing", to_probe=total or len(todo), probed=base)
    done = [0]
    lock = threading.Lock()

    def job(doc):
        if stop_requested():
            return
        dur, definitive = get_duration(doc["path"])
        doc["duration"] = dur
        if definitive:
            doc["probed"] = True
        with lock:
            done[0] += 1
            n = done[0]
        if n % 10 == 0:
            set_status(probed=base + n)

    with ThreadPoolExecutor(max_workers=PROBE_THREADS) as ex:
        list(ex.map(job, todo))
    set_status(True, probed=base + done[0])
    check_stop()


# ─── PHASE 3: SAVE + PRUNE ───────────────────────────────────────────────────

def _import_batches(docs: list) -> int:
    """Upsert docs into the search index in batches; returns how many failed."""
    failed = 0
    for i in range(0, len(docs), BATCH_SIZE):
        check_stop()
        failed += backend.upsert(docs[i:i + BATCH_SIZE], "emplace")
    return failed


def save_docs(docs: list) -> int:
    set_status(True, phase="saving", to_save=len(docs), saved=0)
    failed = 0
    for i in range(0, len(docs), BATCH_SIZE):
        check_stop()
        batch = docs[i:i + BATCH_SIZE]
        failed += backend.upsert(batch, "emplace")
        set_status(saved=min(i + BATCH_SIZE, len(docs)))
    return failed


def probe_and_save(docs: list, to_probe: list):
    """Phase 2+3 in CHECKPOINTS: read durations for a chunk of clips, save that chunk, repeat.
    Everything already saved survives a crash / restart / Stop; the next run sees those clips as
    complete and does not read them again. Returns (failed_imports, probe_seconds, save_seconds)."""
    need = {d["id"] for d in to_probe}
    total_probe = len(need)
    probe_s = save_s = 0.0
    failed = 0
    probed = 0
    set_status(True, phase="probing" if total_probe else "saving",
               to_probe=total_probe, probed=0, to_save=len(docs), saved=0)
    for i in range(0, len(docs), CHECKPOINT):
        check_stop()
        chunk = docs[i:i + CHECKPOINT]
        todo = [d for d in chunk if d["id"] in need]
        t = time.time()
        probe_all(todo, base=probed, total=total_probe)
        probe_s += time.time() - t
        probed += len(todo)
        check_stop()
        t = time.time()
        failed += _import_batches(chunk)
        save_s += time.time() - t
        set_status(saved=min(i + CHECKPOINT, len(docs)))
    return failed, probe_s, save_s


def prune_missing(existing: dict, ctx: ScanContext, dry: bool):
    """Delete index entries not seen in this scan - only for volumes that were scanned
    completely (no unreadable folders) AND only when the number of missing clips is
    plausible (see PRUNE_MAX_PERCENT)."""
    set_status(True, phase="pruning")
    considered: dict = {}
    stale_by_vol: dict = {}
    for uid, doc in existing.items():
        vol = doc.get("volume") or ""
        if not vol:
            try:
                vol = Path(doc.get("path", "")).relative_to(VOLUMES_ROOT).parts[0]
            except (ValueError, IndexError):
                vol = ""
        v = ctx.vols.get(vol)
        if v is None:
            continue                               # volume not scanned this run
        if not v["ok"]:
            continue                               # incomplete scan -> don't risk deleting
        considered[vol] = considered.get(vol, 0) + 1
        if uid not in v["seen"]:
            stale_by_vol.setdefault(vol, []).append(uid)

    skipped = [n for n, v in ctx.vols.items() if not v["ok"]]
    if skipped:
        print(f"Prune skipped for volumes with scan errors: {', '.join(skipped)}")

    stale = []
    for vol, ids in stale_by_vol.items():
        total_v = considered.get(vol, 0)
        limit = max(PRUNE_MIN_ALLOWED, int(total_v * PRUNE_MAX_PERCENT / 100))
        saw_nothing = not ctx.vols[vol]["seen"]
        if not ALLOW_MASS_PRUNE and (len(ids) > limit or saw_nothing):
            msg = (f"Prune REFUSED for {vol}: {len(ids):,} of {total_v:,} indexed clips look "
                   f"missing (limit {limit:,}). Is the volume empty or half-mounted? "
                   f"Check the NAS, then re-run with --allow-mass-prune if this is intended.")
            print(msg)
            WARNINGS.append(msg)
            continue
        stale.extend(ids)

    if not dry:
        for i in range(0, len(stale), 100):
            backend.delete_ids(stale[i:i + 100])
    print(f"Pruned {len(stale):,} stale entr{'y' if len(stale) == 1 else 'ies'}"
          + (" (dry run - nothing deleted)" if dry else ""))


# ─── MAIN FLOW ───────────────────────────────────────────────────────────────

def parse_only(argv) -> list:
    """Collect volume names from every `--only` in argv. Each value may be a comma list:
        --only EDIT2            --only EDIT2,PLAYOUT            --only EDIT2 --only PLAYOUT
    Returns the names in SCAN_VOLUMES order (canonical spelling, no duplicates). Names that
    are not configured raise ValueError so a typo never silently becomes a full run."""
    asked = []
    for i, a in enumerate(argv):
        if a == "--only" and i + 1 < len(argv):
            asked += [x.strip() for x in argv[i + 1].split(",") if x.strip()]
    if not asked:
        return []
    by_fold = {v.casefold(): v for v in SCAN_VOLUMES}
    unknown = [a for a in asked if a.casefold() not in by_fold]
    if unknown:
        raise ValueError(f"Unknown volume(s): {', '.join(unknown)}. "
                         f"Choose from: {', '.join(SCAN_VOLUMES)}")
    want = {a.casefold() for a in asked}
    return [v for v in SCAN_VOLUMES if v.casefold() in want]


def index_archive(existing: dict, dry=False, prune=False, force=False,
                  full=False, only=None):
    only_set = {o.casefold() for o in (only or [])}
    roots, skipped = [], []
    for name in SCAN_VOLUMES:
        if only_set and name.casefold() not in only_set:
            continue
        p = next(
            (x for x in VOLUMES_ROOT.iterdir()
             if x.is_dir() and x.name.casefold() == name.casefold()),
            VOLUMES_ROOT / name
        )
        (roots if p.exists() else skipped).append(p)
    for p in skipped:
        print(f"WARNING: volume not mounted, skipping: {p}")
    if not roots:
        raise RuntimeError(f"No matching volume is mounted. Check {VOLUMES_ROOT} / --only.")

    if existing:
        print(f"Loaded {len(existing):,} existing docs.")

    # ── decide quick vs full scan ────────────────────────────────────────────
    raw = _load_dircache()
    old_dirs = raw.get("dirs", {})
    full_at = raw.get("full_at", {})
    if not isinstance(full_at, dict):
        full_at = {}
    now = time.time()
    force_full = full or force or dry or not existing or not old_dirs
    full_vols = {r.name for r in roots
                 if now - full_at.get(r.name, 0) > FULL_RESCAN_DAYS * 86400}
    mode = "FULL" if force_full else ("quick" if len(full_vols) < len(roots) else "FULL")
    if not force_full and full_vols:
        print(f"Periodic full rescan due for: {', '.join(sorted(full_vols))}")

    t0 = time.time()
    print(f"Scanning {len(roots)} volume(s), {mode} mode, {SCAN_THREADS} threads…")
    set_status(True, phase="scanning", mode=mode)
    ctx = scan_all(roots, existing, force, old_dirs, force_full, full_vols)
    scan_s = time.time() - t0
    check_stop()            # a half-finished scan must never reach save or prune

    docs = [d for v in ctx.vols.values() for d in v["docs"]]
    s = ctx.stats
    for name, n in s["volumes"].items():
        print(f"  {name}: {n:,} clips")
    print(f"Scan done in {scan_s:.0f}s — {s['scanned']:,} clips "
          f"({s['scanned'] / max(scan_s, 1):.0f}/s), {s['unchanged']:,} unchanged, "
          f"{len(docs):,} to write, {len(ctx.probe):,} need ffprobe")
    print(f"  folders: {s['dirs_listed']:,} listed, {s['dirs_skipped']:,} skipped (unchanged) · "
          f"{s['broken_links']} broken links ignored · {s['errors']} errors")

    if dry:
        print("Dry run — nothing written.")
        if prune:
            prune_missing(existing, ctx, dry=True)
        return s["scanned"]

    failed, probe_s, save_s = probe_and_save(docs, ctx.probe) if docs else (0, 0.0, 0.0)
    if failed:
        print(f"WARNING: {failed} documents failed to import — folder cache NOT updated.")
    check_stop()            # never prune after a stop request
    t2 = time.time()
    if prune:
        prune_missing(existing, ctx, dry=False)
    print(f"Timings: scan {scan_s:.0f}s · ffprobe {probe_s:.0f}s · save {save_s:.0f}s · prune {time.time() - t2:.0f}s")

    # Save the folder cache ONLY after everything above succeeded, so a crashed
    # run can never make the next run skip folders whose files weren't saved.
    if not failed:
        scanned_prefixes = tuple(norm_path(r) for r in roots)
        merged = {k: v for k, v in old_dirs.items()
                  if not any(k == pfx or k.startswith(pfx + "/") for pfx in scanned_prefixes)}
        merged.update(ctx.newcache)
        for r in roots:
            if force_full or r.name in full_vols:
                if ctx.vols[r.name]["ok"]:
                    full_at[r.name] = now
        _save_dircache(merged, full_at)
    return s["scanned"]


def resync_user_data(existing: dict):
    """Push SQLite user data to any search-index document that is out of date (e.g. the API
    saved a note while Typesense was briefly down). Only touches documents that differ."""
    rows = store.all_user_rows()
    fixes = []
    for uid, r in rows.items():
        d = existing.get(uid)
        if not d:
            continue
        patch = {"id": uid}
        for k in USER_FIELDS:
            if (d.get(k) or "") != (r.get(k) or ""):
                patch[k] = r.get(k) or ""
        if int(d.get("use_count") or 0) != int(r.get("use_count") or 0):
            patch["use_count"] = int(r.get("use_count") or 0)
        if bool(d.get("hidden")) != bool(r.get("hidden")):
            patch["hidden"] = bool(r.get("hidden"))
        if len(patch) > 1:
            fixes.append(patch)
    for i in range(0, len(fixes), BATCH_SIZE):
        backend.upsert(fixes[i:i + BATCH_SIZE], "update")
    if fixes:
        print(f"Re-synced user data on {len(fixes):,} document(s).")


if __name__ == "__main__":
    DRY = "--dry" in sys.argv
    force = "--force" in sys.argv
    prune = "--prune" in sys.argv
    full = "--full" in sys.argv
    REPROBE = "--reprobe" in sys.argv
    ALLOW_MASS_PRUNE = "--allow-mass-prune" in sys.argv
    try:
        only = parse_only(sys.argv)
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(2)
    if only and {o.casefold() for o in only} == {v.casefold() for v in SCAN_VOLUMES}:
        only = []                       # every volume picked == the normal full run

    if "--restore" in sys.argv:
        i = sys.argv.index("--restore")
        if i + 1 >= len(sys.argv) or not Path(sys.argv[i + 1]).is_file():
            print("Usage: python indexer.py --restore backups/clips_YYYYmmdd_HHMMSS.jsonl")
            sys.exit(2)
        store.init()
        restore_backup(Path(sys.argv[i + 1]))
        sys.exit(0)

    # single-instance guard
    if not DRY and _status.get("state") == "running":
        other = _status.get("pid")
        if other and int(other) != os.getpid() and _pid_alive(other):
            print(f"Indexer already running (pid {other}). Exiting.")
            sys.exit(1)

    _clear_stale_stop_flag()
    if hasattr(signal, "SIGTERM") and threading.current_thread() is threading.main_thread():
        try:
            signal.signal(signal.SIGTERM, lambda *_: _STOP_EVENT.set())     # graceful stop (macOS/Linux)
        except (ValueError, OSError):
            pass
    started = time.time()
    scope = ", ".join(only) if only else "All volumes"
    set_status(
        True, state="running", phase="starting", pid=os.getpid(), started_at=started,
        finished_at=None, scanned=0, unchanged=0, errors=0, volumes={},
        to_probe=0, probed=0, to_save=0, saved=0, message="", scope=scope,
    )

    try:
        existing = {} if DRY else _load_existing_docs()   # BEFORE any --force wipe
        if not DRY:
            store.init()
            if force:
                backup_existing(existing)                 # aborts the rebuild if it fails
            setup_collection(force=force)
        total = index_archive(existing, dry=DRY, prune=prune, force=force, full=full, only=only)

        elapsed = time.time() - started
        print(f"\nAll done in {elapsed:.0f}s.")

        # Keep separate history for the nightly/full run vs a scoped ("this folder
        # only") run, so the UI can show both "Last full run" and "Last run for
        # <volume>" instead of one overwriting the other.
        finish_kw = dict(
            state="idle", phase="done", finished_at=time.time(),
            last_finished_at=time.time(), last_duration_s=elapsed,
            last_total=total, last_scope=scope, message="",
            last_warning=" | ".join(WARNINGS)[:600],
            last_volume_totals=dict(_status.get("volumes") or {}),
        )
        if only:
            # One entry per volume that was scanned, so the UI can show "Last run for
            # <volume>" whether it was indexed alone or as part of a group.
            last_partial = dict(_status.get("last_partial") or {})
            per_vol = {k.casefold(): v for k, v in (_status.get("volumes") or {}).items()}
            for vname in only:
                last_partial[vname] = {
                    "finished_at": time.time(),
                    "duration_s": elapsed,
                    "total": per_vol.get(vname.casefold(), total if len(only) == 1 else 0),
                    "with": [o for o in only if o != vname],
                }
            finish_kw["last_partial"] = last_partial
        else:
            finish_kw["last_full_finished_at"] = time.time()
            finish_kw["last_full_duration_s"] = elapsed
            finish_kw["last_full_total"] = total
        set_status(True, **finish_kw)
        if not DRY and not force:
            resync_user_data(existing)
    except IndexerStopped:
        print("\nStopped on request - nothing was pruned; the next run continues from the current index.")
        set_status(True, state="stopped", phase="stopped", finished_at=time.time(),
                   message="Stopped by user")
        sys.exit(0)
    except KeyboardInterrupt:
        set_status(True, state="error", phase="stopped", message="Stopped by user")
        raise
    except Exception as e:
        print(f"ERROR: {e}")
        set_status(True, state="error", phase="failed", message=str(e))
        sys.exit(1)
    finally:
        try:
            INDEX_STOP_FILE.unlink()          # the request has been honoured (or the run is over anyway)
        except OSError:
            pass
