#!/usr/bin/env python3
"""
ClipStage - does a lighter ffprobe call make the duration scan faster?  (read-only, changes nothing)

    cd ~/ClipStage
    ./clipstage_run.sh probe_benchmark.py /Volumes/EDIT2 200

Arguments: a folder on the NAS (default /Volumes/EDIT2) and how many files to sample (default 200).
It runs the CURRENT ffprobe call and a LIGHTER call on the same files, with the same number of
threads as the indexer, and prints the time for each plus how many durations differ.
Use files the indexer has not cached: this reads the NAS, it does not touch the index.
"""
import json, os, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor

FFPROBE = os.environ.get("FFPROBE_BIN", "ffprobe")
THREADS = int(os.environ.get("CLIPSTAGE_PROBE_THREADS", "8"))
EXTS = (".mxf", ".mov", ".mp4")

root = sys.argv[1] if len(sys.argv) > 1 else "/Volumes/EDIT2"
count = int(sys.argv[2]) if len(sys.argv) > 2 else 200

CURRENT = [FFPROBE, "-v", "error", "-show_entries", "format=duration:stream=duration", "-of", "json"]
LIGHT = [FFPROBE, "-v", "error", "-probesize", "1000000", "-analyzeduration", "500000",
         "-show_entries", "format=duration:stream=duration", "-of", "json"]


def pick(root, n):
    files = []
    for d, _, names in os.walk(root):
        for name in sorted(names):
            if name.lower().endswith(EXTS) and not name.startswith("."):
                files.append(os.path.join(d, name))
                if len(files) >= n:
                    return files
    return files


def dur(cmd, path):
    try:
        r = subprocess.run(cmd + [path], capture_output=True, text=True, timeout=30)
        data = json.loads(r.stdout or "{}")
        vals = [(data.get("format") or {}).get("duration")] + [s.get("duration") for s in data.get("streams", [])]
        for v in vals:
            try:
                return round(float(v), 2)
            except (TypeError, ValueError):
                pass
    except Exception:
        pass
    return None


def run(label, cmd, files):
    t = time.time()
    with ThreadPoolExecutor(max_workers=THREADS) as ex:
        out = list(ex.map(lambda p: dur(cmd, p), files))
    secs = time.time() - t
    ok = sum(1 for x in out if x is not None)
    print(f"{label:8s} {secs:7.1f}s   {len(files)/secs*60:6.0f} files/min   {ok}/{len(files)} durations read")
    return out


files = pick(root, count)
if not files:
    sys.exit(f"No media files found under {root}")
print(f"Sampling {len(files)} files from {root} with {THREADS} threads\n")
a = run("current", CURRENT, files)
# the second pass would hit the OS/SMB cache for the same files, so list order is reversed to blunt that
b = run("light", LIGHT, files[::-1])[::-1]
diff = sum(1 for x, y in zip(a, b) if x is not None and y is not None and abs(x - y) > 0.1)
miss = sum(1 for x, y in zip(a, b) if x is not None and y is None)
print(f"\nDurations that differ by more than 0.1 s: {diff}")
print(f"Files the light call failed on but the current call read: {miss}")
print("Only switch if 'light' is clearly faster AND both numbers above are 0.")
