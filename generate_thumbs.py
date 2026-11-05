"""
ClipStage v5.0 — Thumbnail Generator

What changed vs v3.2
  * Reads the clip list from the search index (same IDs the UI asks for) instead of
    walking the whole NAS again.  --walk restores the old filesystem walk.
  * Writes each thumbnail atomically (temp file → rename): a crash or timeout can no
    longer leave an empty/half JPEG that is then treated as "done".
  * Failures are remembered in <id>.fail and retried after RETRY_DAYS (or now with
    --retry-failed) instead of being retried every night or never.
  * A volume that is not mounted is reported as "missing" — never marked as a failure.
  * Fast input seeking (-ss before -i); falls back to frame 0 for very short clips.
  * --limit N caps the number of NEW thumbnails per run (good for the nightly job).
  * Volumes / extensions / paths come from clipstage_config.py (no hard-coded user path).

Usage
  python generate_thumbs.py                  all clips in the index that have no thumbnail yet
  python generate_thumbs.py --limit 5000     at most 5,000 new thumbnails this run
  python generate_thumbs.py --retry-failed   also retry clips that failed before
  python generate_thumbs.py --walk           scan the volumes instead of reading the index
  python generate_thumbs.py --workers 8      parallel ffmpeg processes (default 4)
"""

import argparse
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import clipstage_dotenv  # noqa: F401  (loads .env first)
from clipstage_config import (COLLECTION, EXCLUDE_FOLDERS, FFMPEG, SCAN_VOLUMES, THUMB_DIR,
                              VIDEO_EXTENSIONS, VOLUMES_ROOT, clip_uid, env_int, norm_path)

RETRY_DAYS = env_int("CLIPSTAGE_THUMB_RETRY_DAYS", 7)
FFMPEG_TIMEOUT = env_int("CLIPSTAGE_THUMB_TIMEOUT", 30)
THUMB_DIR = str(THUMB_DIR)


# ── clip sources ──────────────────────────────────────────────────────────────

def clips_from_index():
    """[(uid, path)] from the search index (Typesense on Mac, SQLite on Windows)."""
    from search_backend import get_backend
    docs = get_backend(10).export_all()
    return [(uid, d["path"]) for uid, d in docs.items() if uid and d.get("path")]


def clips_from_walk():
    """[(uid, path)] by walking the configured volumes (legacy behaviour)."""
    out = []
    for vol_name in SCAN_VOLUMES:
        vol_path = os.path.join(str(VOLUMES_ROOT), vol_name)
        if not os.path.exists(vol_path):
            print(f"WARNING: {vol_path} not mounted — skipping")
            continue
        print(f"Scanning {vol_path} ...")
        for root, dirs, filenames in os.walk(vol_path):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() not in EXCLUDE_FOLDERS]
            for f in filenames:
                if f.startswith("._"):
                    continue
                if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS:
                    p = norm_path(os.path.join(root, f))
                    out.append((clip_uid(p), p))
    return out


# ── one thumbnail ─────────────────────────────────────────────────────────────

def _ffmpeg_frame(src: str, dst_tmp: str, seek: str) -> bool:
    cmd = [FFMPEG or "ffmpeg", "-nostdin", "-v", "error", "-ss", seek, "-i", src,
           "-frames:v", "1", "-vf", "scale=320:180", "-q:v", "5", "-y", "-f", "image2", dst_tmp]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False
    return r.returncode == 0 and os.path.exists(dst_tmp) and os.path.getsize(dst_tmp) > 0


def make_thumb(uid: str, path: str) -> str:
    """Returns: 'generated' | 'failed' | 'missing'."""
    thumb = os.path.join(THUMB_DIR, uid + ".jpg")
    fail = os.path.join(THUMB_DIR, uid + ".fail")
    tmp = os.path.join(THUMB_DIR, f"{uid}.{os.getpid()}.{threading.get_ident()}.tmp")

    if not os.path.exists(path):
        return "missing"                   # unmounted volume / deleted file — not a failure

    ok = False
    try:
        for seek in ("3", "0"):            # short clips have no frame at 3 s
            if _ffmpeg_frame(path, tmp, seek):
                ok = True
                break
        if ok:
            os.replace(tmp, thumb)         # atomic: readers never see a partial JPEG
            if os.path.exists(fail):
                os.unlink(fail)
            return "generated"
        with open(fail, "w") as f:
            f.write(str(int(time.time())))
        return "failed"
    except Exception:
        return "failed"
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# ── main ──────────────────────────────────────────────────────────────────────

def pending(clips, retry_failed: bool, limit: int):
    """Clips that need a thumbnail: no .jpg (or an empty legacy one) and no recent .fail."""
    have = {}
    for e in os.scandir(THUMB_DIR):
        if e.name.endswith((".jpg", ".fail")):
            try:
                have[e.name] = e.stat().st_size if e.name.endswith(".jpg") else e.stat().st_mtime
            except OSError:
                pass
    now = time.time()
    todo, cached, cooling = [], 0, 0
    for uid, path in clips:
        size = have.get(uid + ".jpg")
        if size:                                     # exists and non-empty
            cached += 1
            continue
        if size == 0:                                # legacy 0-byte file from an old timeout
            try:
                os.unlink(os.path.join(THUMB_DIR, uid + ".jpg"))
            except OSError:
                pass
        fail_mtime = have.get(uid + ".fail")
        if fail_mtime and not retry_failed and now - fail_mtime < RETRY_DAYS * 86400:
            cooling += 1
            continue
        todo.append((uid, path))
    if limit and len(todo) > limit:
        todo = todo[:limit]
    return todo, cached, cooling


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ClipStage thumbnail generator")
    ap.add_argument("--limit", type=int, default=0, help="max NEW thumbnails this run (0 = all)")
    ap.add_argument("--retry-failed", action="store_true", help="retry clips that failed before")
    ap.add_argument("--walk", action="store_true", help="scan volumes instead of reading the index")
    ap.add_argument("--workers", type=int, default=env_int("CLIPSTAGE_THUMB_WORKERS", 4),
                    help="parallel ffmpeg processes (NAS-bound; lower if the NAS feels slow)")
    args = ap.parse_args(argv)

    os.makedirs(THUMB_DIR, exist_ok=True)
    try:
        clips = clips_from_walk() if args.walk else clips_from_index()
    except Exception as e:
        print(f"ERROR: could not list clips: {e}")
        return 2

    todo, cached, cooling = pending(clips, args.retry_failed, args.limit)
    total = len(todo)
    print(f"{len(clips):,} clips · {cached:,} already have a thumbnail · "
          f"{cooling:,} failed recently (skipped) · {total:,} to do")
    print(f"Thumbnails → {THUMB_DIR} · workers {args.workers}\n")

    counts = {"generated": 0, "failed": 0, "missing": 0}
    lock = threading.Lock()
    t0 = time.time()
    completed = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(make_thumb, uid, path) for uid, path in todo]
        for fut in as_completed(futures):
            result = fut.result()
            with lock:
                completed += 1
                counts[result] += 1
                if completed % 100 == 0 or completed == total:
                    el = time.time() - t0
                    rate = completed / el if el > 0 else 1
                    eta = int((total - completed) / rate) if rate > 0 else 0
                    print(f"[{completed / total * 100:5.1f}%] {completed:,}/{total:,} | "
                          f"new:{counts['generated']} failed:{counts['failed']} "
                          f"missing:{counts['missing']} ETA:{eta // 60}m{eta % 60}s")

    el = int(time.time() - t0)
    print(f"\nDone in {el // 60}m {el % 60}s — generated {counts['generated']:,} · "
          f"failed {counts['failed']:,} · file missing/unmounted {counts['missing']:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
