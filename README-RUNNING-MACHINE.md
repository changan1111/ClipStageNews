# ClipStage v5.0 - Running Machine Guide

For the Mac that **already runs ClipStage**. Covers: upgrading to v5.0, checking it works, daily
operation, backups, recovery and rollback. (Installing on a new Mac? Use
[`README-FRESH-INSTALL.md`](README-FRESH-INSTALL.md). What the tool is: [`README.md`](README.md).)

**Which upgrade path?**

| You run now | Path | Typical downtime |
| --- | --- | --- |
| **v3.2** (`clips_v2` collection, `clipstage.db`) | Section 2 - drop-in, no data migration | ~5 minutes |
| **S11 or S12** (`clips` collection, no `clipstage.db`) | Section 2, **plus** Section 3 (one-time migration) | ~15-30 minutes |

v5.0 does **not** change the search schema or the database layout, so rolling back from v5.0 to
v3.2 is safe (Section 8).

---

## 1. Before you touch anything

Do all of these first. Five minutes now saves a bad day later.

```sh
cd ~/path/to/your/current/ClipStage          # the folder you run today
STAMP=$(date +%Y%m%d_%H%M)
mkdir -p ~/clipstage-backup-$STAMP
cp .env clipstage.db editors.json ~/clipstage-backup-$STAMP/ 2>/dev/null
cp -R backups ~/clipstage-backup-$STAMP/ 2>/dev/null
ls ~/clipstage-backup-$STAMP
```

- `clipstage.db` holds every note, detail, use count, hidden flag and the audit log. **It cannot be
  rebuilt.** (S11/S12 installs have no `clipstage.db`; their notes live inside Typesense - see 3.)
- Also copy the Typesense data folder (default `~/typesense-data`) if the index is large - it saves
  a long re-index if something goes wrong:
  `cp -R ~/typesense-data ~/clipstage-backup-$STAMP/typesense-data` (stop Typesense first for a
  consistent copy: `pkill typesense-server`).
- Keep the **old folder** as it is. You will install v5.0 into a *new* folder next to it.
- Tell editors there will be a short outage. Do the upgrade outside broadcast hours.

---

## 2. Upgrade (v3.2 or v4.0 -> v5.0)

### 2.1 Install v5.0 beside the old version

```sh
cd ~/path/to
unzip clipstage-v5.0.zip                      # creates ClipStage-v5.0/
cd ClipStage-v5.0
cp ../OLD_FOLDER/.env .env                    # your existing settings
cp ../OLD_FOLDER/editors.json editors.json    # your existing editor names
cp ../OLD_FOLDER/clipstage.db clipstage.db    # your notes / usage / audit log
cp -R ../OLD_FOLDER/static/thumbs static/     # keep existing thumbnails (can be large; or `mv`)
cp ../OLD_FOLDER/index_dircache.json .        # optional: keeps the next scan quick
cp ../OLD_FOLDER/mount_volumes.sh . 2>/dev/null   # if you have one (the package does not ship it)
```

Replace `OLD_FOLDER` with your current folder name. (`mount_volumes.sh` is specific to your NAS and
is not part of the package; the scripts run without it if the volumes are already mounted.)

### 2.2 Update `.env`

Open `.env` and compare with the new `.env.sample`:

```sh
diff <(sed 's/=.*//' .env | sort) <(sed 's/=.*//' .env.sample | sort)
```

Lines shown with `>` are settings that are new in v5.0 - all have sensible defaults, so adding them
is optional. The ones worth setting:

| Setting | Why |
| --- | --- |
| `TYPESENSE_KEY` | **Rotate it now** (see 2.3). The key shipped in earlier sample files should be treated as public. |
| `CLIPSTAGE_PRUNE_MAX_PERCENT=5` | Prune safety threshold (default is fine). |
| `CLIPSTAGE_THUMBS=1`, `CLIPSTAGE_THUMBS_LIMIT=5000` | Nightly thumbnails. Set `CLIPSTAGE_THUMBS=0` to turn them off. |
| `CLIPSTAGE_ALERT_WEBHOOK=` | Optional Slack/Teams/Discord webhook so failures reach you. |
| `CLIPSTAGE_BIND_HOST` | **Check it.** `127.0.0.1` means *only this Mac* can open ClipStage. If editors currently use `http://<this-mac>:8000` directly, you must either put the HTTPS proxy in front (Section 7) or set this to the Mac's LAN address / `0.0.0.0`. |

`.env` rules: one `KEY=value` per line; spaces inside values are fine (`SHARE FOLDER`); do not
use `export`, `$(...)` or other shell syntax - the file is parsed, never executed.

### 2.3 Rotate the Typesense key

```sh
openssl rand -hex 24                          # copy the output
```

Put it in `.env` as `TYPESENSE_KEY=...`, then restart Typesense so it uses the new key:

```sh
pkill typesense-server                        # then step 2.5 starts it again with the new key
```

If Typesense is started by something other than ClipStage (a launch daemon, Homebrew service), change
the key there too. The index data is **not** affected by the key.

### 2.4 Install dependencies

```sh
python3 -m venv .venv                         # skip if you already use a venv; or reuse the old one
.venv/bin/pip install -r requirements.txt
```

Check ffmpeg: `ffmpeg -version | head -1` and `ffprobe -version | head -1`.

### 2.5 Run the tests (optional but recommended - 10 seconds)

```sh
.venv/bin/pip install -r requirements-dev.txt
TYPESENSE_KEY=x SUPABASE_URL=https://x.supabase.co SUPABASE_ANON_KEY=x .venv/bin/python -m pytest -q
```

Expected: all tests pass (the one that needs `ffmpeg` is skipped if it is missing). These tests use
fakes; they do not touch your NAS or live index.

### 2.6 Switch over

```sh
pkill -f "uvicorn api:app"                    # stop the OLD version (from any folder)
bash start_clipstage.sh                       # in the NEW folder; leave this window open
```

You should see "Mounting NAS volumes", "Starting Typesense", then uvicorn listening. Warnings about
"API listening on ... over plain HTTP" mean the bind address is not `127.0.0.1` (see 2.2).

### 2.7 Verify (all five, once)

1. **Health:** `curl -s http://127.0.0.1:8000/health` -> `{"status":"ok"}`.
2. **Login:** open the app in a browser and sign in as an admin. Wrong password five times in a row
   must lock the login for a few minutes.
3. **Search:** search `launch first`. Clips named `First_Time_This_Movie_Launch`,
   `Launch_First_Movie` and `Launch_The_Day_First` must all appear; `first laun` must too.
4. **Your data survived:** open a clip you know had a note - the note must be there.
5. **Stage + bulk note:** select two clips, add a bulk note, stage them. Open the staging folder
   from a real editor's computer and confirm the clips open (symlinks only work if the editor's Mac
   has the same NAS volumes mounted under the same names).

Admin extras: `GET /admin/health` shows `"version": "5.0.0"`, your collection and document count,
and `token_separators` containing `_`.

### 2.8 Re-install the schedule

```sh
bash install_nightly.sh
launchctl kickstart -k gui/$(id -u)/com.clipstage.indexer      # run it now instead of waiting for 17:00
tail -f indexer.log
```

Look for `=== Scheduled index ...`, a scan summary, `Pruned N stale entries`, the thumbnail summary
and `=== Finished (exit 0)`. A LaunchAgent only runs while this Mac user is logged in.

**First thumbnail run:** v5.0 reads the clip list from the index and skips clips that already have a
thumbnail; empty (0-byte) files left by old timeouts are deleted and regenerated. If the backlog is
large it will take several nights at 5,000 per night - or run it once by hand:
`./clipstage_run.sh generate_thumbs.py --limit 20000 --workers 4`.

---

## 3. Extra steps only for S11 / S12 installs

Your current search collection is called `clips` and notes live inside it. v5.0 uses `clips_v2` and
keeps notes in `clipstage.db`. The migration copies everything and **leaves `clips` untouched**:

Run these commands from the NEW project folder. Keep `clipstage_env.sh` and `.env` there. The
migration must run with the environment set up by `clipstage_env.sh`; the command below sources it
before running the selected Python interpreter. Typesense must be running with the key in `.env`;
`start_clipstage.sh` starts Typesense as well as the API.

```sh
# In the NEW folder, with clipstage_env.sh and .env in place and Typesense running:
bash -c '. ./clipstage_env.sh && "$PYTHON" migrate_to_v2.py'  # builds clips_v2 and copies notes/use counts into clipstage.db
./clipstage_run.sh indexer.py --prune           # picks up anything that changed meanwhile
```

Also re-run `supabase_profiles_setup.sql` in the Supabase SQL editor (adds the optional
`editor_name` column and fixes a policy). Until you do, logins still work; accounts just are not
bound to one editor.

S12 settings that no longer exist: `CLIPSTAGE_INTERNAL_TOKEN` (v5.0 has no in-memory cache to
refresh), `CLIPSTAGE_AUTH_CACHE_SECONDS` (the login cache is a fixed 60 s), `CLIPSTAGE_CORS_ORIGINS`
(now `CLIPSTAGE_ALLOWED_ORIGINS`). Leaving them in `.env` is harmless.

---

## 4. Daily operation

| Task | Command / place |
| --- | --- |
| Start | `bash start_clipstage.sh` |
| Stop | `pkill -f "uvicorn api:app"` |
| Is it up? | `curl -s http://127.0.0.1:8000/health` |
| Index everything now | `./manual_index.sh` or the **Sync** button (admin) |
| Index one or a few volumes | `./manual_index.sh PLAYOUT` / `./manual_index.sh EDIT2 PLAYOUT`, or tick the volumes in the picker next to **Sync** |
| Watch the nightly run | `tail -f indexer.log` |
| Who did what | `GET /admin/audit` (admin) |
| Restore a hidden clip | `POST /admin/restore/<id>` (admin; header `X-ClipStage-CSRF: 1`) |
| Run only the thumbnails | `./clipstage_run.sh generate_thumbs.py --limit 5000` |
| Retry thumbnails that failed | `./clipstage_run.sh generate_thumbs.py --retry-failed` |

The nightly job (17:00, edit `Hour` in the plist then re-run `install_nightly.sh` to change it)
mounts volumes, starts Typesense if needed, indexes with prune, raises an alert if anything failed or
if a prune was refused, then makes up to 5,000 thumbnails.

### When an alert says "Prune REFUSED for <volume>"

That is the guard working. More than 5 % of that volume's indexed clips looked missing, or the scan
saw nothing. Almost always the volume is **not fully mounted** or the NAS is unreachable.

1. `ls /Volumes/<volume> | head` - is it empty or errors? Remount / fix the NAS.
2. Re-run `./manual_index.sh <volume>` once it is healthy; the prune then proceeds normally.
3. Only if the clips really were deleted on purpose (a big clean-up):
   `./clipstage_run.sh indexer.py --prune --only <volume> --allow-mass-prune`.

Nothing is lost while a prune is refused - the clips simply stay in search.

### When search looks wrong or the index is damaged

1. Try `./clipstage_run.sh indexer.py --full` (re-lists every folder).
2. Still wrong: `./clipstage_run.sh indexer.py --force`. It first writes a snapshot to `backups/`, then
   rebuilds the collection. Notes and details are re-applied from `clipstage.db`, so nothing typed
   by editors is lost. If the snapshot cannot be written the rebuild stops before deleting anything.
3. To go back to a snapshot: `./clipstage_run.sh indexer.py --restore backups/clips_YYYYmmdd_HHMMSS.jsonl`.
   The live notes in `clipstage.db` are re-applied afterwards.

---

## 5. Backups - what to copy and when

| Item | How often | How |
| --- | --- | --- |
| **`clipstage.db`** | **Nightly** | `sqlite3 clipstage.db ".backup '/backup/clipstage-$(date +%F).db'"` (or `cp` while the API is idle) |
| `.env`, `editors.json` | After every change | copy |
| `backups/` (index snapshots) | Automatic before `--force` | newest five kept |
| Typesense data folder | Weekly, or before big changes | stop Typesense, then copy |
| Supabase | Per your Supabase plan | project backup; export `clipstage_profiles` |

Not worth backing up: `static/thumbs/`, `index_dircache.json`, `index_status.json`, logs - all are
regenerated.

---

## 6. Troubleshooting

### 6.1 Rules that prevent most problems

- **Start the app with `./start_clipstage.sh`.** Only the shell scripts read `.env`. Starting
  `uvicorn api:app` by hand gives the API empty Supabase/Typesense settings.
- **Run Python tools with their environment loaded.** `./clipstage_run.sh` sources the adjacent
  `clipstage_env.sh` for tools such as `indexer.py`; for the S11/S12 migration, use the explicit
  `bash -c '. ./clipstage_env.sh && "$PYTHON" migrate_to_v2.py'` command in Section 3. Keep
  `clipstage_env.sh` and `.env` beside the scripts. Running `python3 indexer.py` directly fails
  with `TYPESENSE_KEY environment variable is required`.
- **Use one address in the browser** (`http://127.0.0.1:8000` *or* `localhost`, not both): each has its
  own saved session.
- **Never paste `.env` contents, passwords or tokens into chats or screenshots.** If you did,
  rotate them: new Typesense key (`openssl rand -hex 24` in `.env`, then restart Typesense **and** the
  API together) and a new Supabase password. The Supabase anon key is public by design.
- **To see the real reason for a 503/401**, open browser DevTools -> Network, click the failing
  request -> Response, and read `detail`.

### 6.2 Login problems

`POST /auth/login` returns one of these. 401s on `/admin/indexable-volumes` and `favicon.ico` before
you sign in are normal.

| `detail` / symptom | Cause | Fix |
| --- | --- | --- |
| `Supabase authentication is not configured` | `SUPABASE_URL` / `SUPABASE_ANON_KEY` empty in the running process | Start with `./start_clipstage.sh`; check the two lines in `.env` have no quotes or trailing spaces. |
| `Supabase authentication is unavailable` | This Mac cannot reach Supabase (DNS, VPN, firewall) or the URL is wrong | `curl -i "$SUPABASE_URL/auth/v1/health" -H "apikey: <anon key>"` must return 200. |
| `Supabase login failed` | Supabase answered something other than 200/400/401, e.g. the free-tier project is **paused** | Open the Supabase dashboard and restore the project. |
| `Could not read Supabase account profile` | The password is fine, but the `clipstage_profiles` read fails. Usually a **recursive row-level-security policy** (`42P17 infinite recursion detected in policy`), or the table/grants are missing | Re-run `supabase_profiles_setup.sql` in the Supabase SQL editor: it drops **every** policy on the table and recreates the single read policy. Then re-test (below). |
| 403 `Account role or valid-through date is missing or expired` | No row for that user in `clipstage_profiles`, or `valid_through` is in the past | Add/extend the row (examples at the bottom of `supabase_profiles_setup.sql`). |
| 401 `Incorrect email or password` / 429 `Too many failed attempts` | Wrong credentials; 5 failures lock that email for 5 minutes | Check the password in Supabase (Authentication -> Users); wait, then retry. |
| "login required" after about an hour | Supabase token expiry | Sign in again, or lengthen the token lifetime in Supabase Auth settings. |

To test the profile read outside the app (fill in your anon key, email and password):

```sh
ANON="<anon key>"; URL="https://<project>.supabase.co"
TOKEN=$(curl -s -X POST "$URL/auth/v1/token?grant_type=password" -H "apikey: $ANON" \
  -H "Content-Type: application/json" -d '{"email":"you@example.com","password":"..."}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
curl -s -i "$URL/rest/v1/clipstage_profiles?select=role,valid_through" \
  -H "apikey: $ANON" -H "Authorization: Bearer $TOKEN"
```

Expect `HTTP/2 200` and one row such as `[{"role":"admin","valid_through":"2026-12-31"}]`. `[]`, 401, 403 or
`42P17` means the policies/grants are wrong: re-run `supabase_profiles_setup.sql`.

### 6.3 Search problems

| Symptom | Cause | Fix |
| --- | --- | --- |
| `/search` returns 503 (`Search unavailable: ...`) | Typesense down, key mismatch, or the `clips_v2` collection is missing | Run the checks below. |
| `/collections` lists only `clips`, not `clips_v2` | Upgraded from an older install: v5.0 searches `clips_v2` | From the project folder, with `clipstage_env.sh` and `.env` present and Typesense running, run `bash -c '. ./clipstage_env.sh && "$PYTHON" migrate_to_v2.py'` (migration leaves `clips` untouched), then `./manual_index.sh`. Make sure `.env` has no `CLIPSTAGE_COLLECTION=clips`; it must be `clips_v2` or absent. |
| `collection 'clips_v2' not found` in the log | Fresh index not built yet | `./manual_index.sh` (volumes must be mounted). |
| `/health` ok but search finds nothing | Typesense down or key mismatch | See checks below. |
| Notes saved but not searchable for a while | Typesense was briefly down when saving | The next index run re-syncs. |

Typesense checks, from the project folder:

```sh
curl -s localhost:8108/health                       # {"ok":true}
KEY=$(grep '^TYPESENSE_KEY=' .env | cut -d= -f2-)
curl -s "localhost:8108/collections" -H "X-TYPESENSE-API-KEY: $KEY"    # must list clips_v2
```

Connection refused = Typesense is not running (`./start_clipstage.sh`). 401/Forbidden = the key in `.env`
differs from the key Typesense was started with (restart both after changing it).

### 6.4 UI problems

| Symptom | Cause | Fix |
| --- | --- | --- |
| EDITOR dropdown empty | Not signed in; or `editors.json` missing; or its key is wrong. The file must be exactly `{ "editors": ["GOKUL", "PRIYA"] }` - a typo such as `"editors.jason"` gives an empty list | Fix the file (no restart needed) and refresh. Check: `python3 -c "import json;print(len(json.load(open('editors.json'))['editors']))"`. Names may not contain `/` or `\` or leading/trailing spaces. A login whose profile has `editor_name` sees only that name. |
| No email / **Log out** in the header, or no **Sync Index** / volume picker | An old `static/index.html` or `static/indexer_status.js` is being served, or the signed-in account is not `admin` | Use the v5.0 files (the header asks `/auth/me` who you are and shows your email, role and **Log out**). Move any old `static/indexer_status.js` / `static/index.html` aside, restart, hard-refresh (Cmd+Shift+R). If still wrong: browser console `localStorage.clear(); location.reload()` and sign in again. Only an account with `role = admin` in `clipstage_profiles` sees Sync Index. |
| Sync Index runs everything although volumes are ticked | Old `index.html` (v4.0) ignored the picker | Use the v5.0 `index.html` and `indexer_status.js` together. |
| Editors cannot open the page | `CLIPSTAGE_BIND_HOST=127.0.0.1` with no proxy | Set it to the server's LAN address (e.g. `10.1.10.203`) or `0.0.0.0` behind the HTTPS proxy (Section 7). |
| Preview says "too many previews" | All preview slots busy (default 3) | Raise `CLIPSTAGE_MAX_STREAMS` if the Mac can cope. |
| Thumbnails missing | `generate_thumbs.py` writes `<id>.fail` for unreadable clips and retries after 7 days; unmounted volumes are "missing", not failed | `./clipstage_run.sh generate_thumbs.py --limit 5000`. |

`CLIPSTAGE_SMB_HOST` (for example `10.1.10.200`) is only used to build the `smb://` links editors click
to open a file. It does not affect search or login.

### 6.5 Indexing, startup and environment

| Symptom | Cause | Fix |
| --- | --- | --- |
| `TYPESENSE_KEY environment variable is required` (running a Python tool, or the API will not start) | `.env` not loaded or not next to `api.py` | Run tools with `./clipstage_run.sh <script>.py`; start the API with `./start_clipstage.sh`. |
| `Indexer already running (pid N)` | A run is in progress. If not: it crashed | The UI shows "error"; the next run clears it. |
| Nothing indexed / "volume not mounted" | Volume names differ | `ls /Volumes`; they must match `CLIPSTAGE_SCAN_VOLUMES` exactly. |
| Nightly job did not run | The Mac user must be logged in | `tail indexer.log launchd.log`; re-run `install_nightly.sh`. |
| `.env` value with spaces not applied | Wrapped in `export` or sourced | Plain `KEY=value` lines only; spaces inside a value are fine (`SHARE FOLDER`). |

---

## 7. HTTPS for LAN users (recommended)

Passwords and the login cookie cross the LAN in clear text over plain HTTP. To fix it:

1. `brew install caddy`
2. Copy `Caddyfile.sample`, set a name your editors' computers can resolve.
3. On the server: `caddy trust`. On **each editor computer**: install Caddy's root certificate once
   and mark it "Always Trust" (path is inside the sample file).
4. `.env`: `CLIPSTAGE_BIND_HOST=127.0.0.1` and `CLIPSTAGE_COOKIE_SECURE=1`; restart ClipStage.
5. From another computer, both of these must now **fail**: `curl -m 3 http://<mac-ip>:8000` and
   `curl -m 3 http://<mac-ip>:8108/health`.

---

## 8. Rolling back

v5.0 does not change the index schema or `clipstage.db`, so the old version runs on the same data:

```sh
pkill -f "uvicorn api:app"
cd ~/path/to/OLD_FOLDER
bash start_clipstage.sh
```

Notes added while v5.0 was running are in `clipstage.db`; copy the **newer** file back into the old
folder first (`cp ../ClipStage-v5.0/clipstage.db .`) so they are not lost. If you rotated the
Typesense key, put the new key in the old folder's `.env` too.
