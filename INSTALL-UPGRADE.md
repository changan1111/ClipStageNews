# ClipStage v5.0 - Upgrade of an Existing Install (one file)

For a Mac already running ClipStage. New Mac? Use `INSTALL-FRESH.md`.

| You run now | Path |
| --- | --- |
| **v3.x / v4.x** (collection `clips_v2`, has `clipstage.db`) | Steps 1-8. **No migration.** |
| **S11 / S12** (collection `clips`, no `clipstage.db`) | Steps 1-8 **plus Step 7 migration** |

v5.0 does not change the search schema or database layout, so rolling back to v3.2 is safe (keep the old folder).

---

## 1. Back up first
```sh
cd ~/path/to/OLD_FOLDER
STAMP=$(date +%Y%m%d_%H%M); mkdir -p ~/clipstage-backup-$STAMP
cp .env clipstage.db editors.json ~/clipstage-backup-$STAMP/ 2>/dev/null
```
`clipstage.db` holds all notes and usage and cannot be rebuilt. Optionally copy `~/typesense-data` too
(stop Typesense first). Tell editors about a short outage.

## 2. Unpack v5.0 beside the old version
```sh
cd ~/path/to
unzip clipstage-v5_0-final.zip && cd ClipStage-v5.0
cp ../OLD_FOLDER/.env .env
cp ../OLD_FOLDER/editors.json editors.json
cp ../OLD_FOLDER/clipstage.db clipstage.db 2>/dev/null
cp -R ../OLD_FOLDER/static/thumbs static/ 2>/dev/null
cp ../OLD_FOLDER/index_dircache.json . 2>/dev/null
cp ../OLD_FOLDER/mount_volumes.sh . 2>/dev/null
```

## 3. Update `.env`  (this is where the network fix goes)
```sh
open -e .env
```
Make sure these lines exist and are correct:
```
CLIPSTAGE_BIND_HOST=0.0.0.0      # REQUIRED for other machines to connect. Missing = 127.0.0.1 = "site can't be reached"
CLIPSTAGE_COOKIE_SECURE=0        # 1 only when users come through https
CLIPSTAGE_COLLECTION=clips_v2    # or remove the line; never "clips"
TYPESENSE_KEY=<new random key>   # rotate: openssl rand -hex 24
CLIPSTAGE_PRUNE_MAX_PERCENT=5
CLIPSTAGE_THUMBS=1
CLIPSTAGE_THUMBS_LIMIT=5000
```
See what is new compared with the sample:
`diff <(sed 's/=.*//' .env | sort) <(sed 's/=.*//' .env.sample | sort)` (lines marked `>` are optional).
Use `127.0.0.1` for the bind only if you put the HTTPS proxy in front. Rotating the key requires
`pkill typesense-server`; the next start uses the new key. The index data is unaffected.

## 4. Install dependencies
```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
ffmpeg -version | head -1
```

## 5. Optional tests (10 seconds)
```sh
.venv/bin/pip install -r requirements-dev.txt
TYPESENSE_KEY=x SUPABASE_URL=https://x.supabase.co SUPABASE_ANON_KEY=x .venv/bin/python -m pytest -q
```

## 6. Switch over
```sh
pkill -f "uvicorn api:app"        # stop the old version
bash start_clipstage.sh           # new folder; leave this window open
```
Look for "Starting ClipStage API on 0.0.0.0:8000". In a second window:
```sh
curl -s http://127.0.0.1:8000/health      # {"status":"ok"}
lsof -iTCP:8000 -sTCP:LISTEN              # must show *:8000
```
Then open `http://<this-mac-ip>:8000` from another machine.

## 7. S11 / S12 ONLY: one-time migration
Skip this entire step if you already had `clips_v2`. With the app started (Typesense running):
```sh
./clipstage_run.sh migrate_to_v2.py        # builds clips_v2, copies notes/use counts into clipstage.db
./clipstage_run.sh indexer.py --prune      # picks up changes since
```
The old `clips` collection is left untouched. Redo with `--replace` if needed. Also re-run
`supabase_profiles_setup.sql` in Supabase.

## 8. Verify and re-install the schedule
1. Sign in from another machine; search, preview, notes, stage all work.
2. `GET /admin/health` shows `5.0.0`, documents > 0, `token_separators` containing `_`.
3. Nightly job:
```sh
bash install_nightly.sh
launchctl kickstart -k gui/$(id -u)/com.clipstage.indexer
tail -f indexer.log        # ends with "Finished (exit 0)"
```
The nightly job loads `.env` itself and is unaffected by the bind address. It runs at 17:00 by default,
only while this Mac user is logged in and awake.
If the thumbnail backlog is large, run once: `./clipstage_run.sh generate_thumbs.py --limit 20000 --workers 4`.

## Rollback
```sh
pkill -f "uvicorn api:app"
cd ~/path/to/OLD_FOLDER && bash start_clipstage.sh
```
Notes added since the upgrade live in the new `clipstage.db`; copy it back if you want them.

## Troubleshooting
| Symptom | Fix |
| --- | --- |
| Other machine: `ERR_CONNECTION_REFUSED` | Bind is `127.0.0.1`/missing. Set `CLIPSTAGE_BIND_HOST=0.0.0.0`, restart, check `lsof`, allow Python in the firewall. |
| `/collections` lists only `clips` | S11/S12 install: do Step 7, then `./manual_index.sh`. |
| `TYPESENSE_KEY required` when running a tool | Use `./clipstage_run.sh <script>.py`. |
| Search 503 | Typesense down, or key in `.env` differs from the one Typesense started with (`pkill typesense-server`, restart). |
| Nightly did not run | `launchctl list | grep clipstage`; read `indexer.log` and `launchd.log`. |
