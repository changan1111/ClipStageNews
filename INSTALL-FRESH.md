# ClipStage v5.0 - Fresh Installation (new Mac, one file)

Use this on a Mac that has never run ClipStage. Upgrading an existing install? Use `INSTALL-UPGRADE.md`.
Run the steps in order. Each step ends with a check.

**You need:** a Mac that stays on and logged in, admin rights, NAS volumes mounting under `/Volumes`,
a Supabase project (URL + **anon/publishable** key; never the service-role key), and this zip.

---

## 1. Install tools
```sh
brew install python@3.12 ffmpeg
python3 --version            # 3.10 or newer
ffmpeg -version | head -1
which typesense-server       # install from typesense.org/docs (macOS) if missing
```

## 2. Supabase logins
1. Authentication -> Providers: Email on. **Disable public sign-ups.**
2. Authentication -> Users -> Add user: the admin and every editor ("auto confirm").
3. SQL editor: run the contents of `supabase_profiles_setup.sql`.
4. Give each user a role and expiry (change e-mails/dates):
```sql
insert into public.clipstage_profiles (user_id, role, valid_through, editor_name)
select id, 'admin', '2027-12-31'::date, null from auth.users where email = 'you@example.com'
on conflict (user_id) do update set role=excluded.role, valid_through=excluded.valid_through, editor_name=excluded.editor_name;

insert into public.clipstage_profiles (user_id, role, valid_through, editor_name)
select id, 'editor', '2027-12-31'::date, 'Meena R' from auth.users where email = 'meena@example.com'
on conflict (user_id) do update set role=excluded.role, valid_through=excluded.valid_through, editor_name=excluded.editor_name;
```
5. Project Settings -> API: copy Project URL and anon key.

Check: `select * from public.clipstage_profiles;` shows your rows.

## 3. Install ClipStage
```sh
cd ~/Documents
unzip clipstage-v5_0-final.zip
cd ClipStage-v5.0
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -c "import fastapi, typesense, requests; print('ok')"
```

## 4. Configure
```sh
cp .env.sample .env
cp editors.sample.json editors.json
openssl rand -hex 24         # use the output as TYPESENSE_KEY
open -e .env
```
Set in `.env`:

| Setting | Value |
| --- | --- |
| `TYPESENSE_KEY` | the new random value |
| `SUPABASE_URL`, `SUPABASE_ANON_KEY` | from step 2 |
| `CLIPSTAGE_SCAN_VOLUMES` | volume names exactly as in `ls /Volumes`, comma separated |
| `CLIPSTAGE_SMB_HOST` | this Mac's IP as editors see it (e.g. `10.1.10.203`) |
| `CLIPSTAGE_BIND_HOST` | `0.0.0.0` so other machines can connect (already the default in `.env.sample`). Use `127.0.0.1` only behind the HTTPS proxy (step 9). |
| `CLIPSTAGE_COLLECTION` | `clips_v2` (or leave it out; never `clips`) |

`editors.json` must look exactly like `{ "editors": ["GOKUL", "PRIYA"] }`. Names must match the
`editor_name` values from step 2.

## 5. Mount NAS and prepare staging
```sh
ls /Volumes                              # names must match CLIPSTAGE_SCAN_VOLUMES
mkdir -p /Users/Shared/staging
```
Add `/Users/Shared/staging` in System Settings -> General -> Sharing -> File Sharing. Editors' Macs
need the NAS volumes mounted under the **same names** (staged clips are symlinks).

If the Mac firewall is on, allow incoming connections for Python.

## 6. Start the app
Terminal window 1 (leave it open):
```sh
cd ~/Documents/ClipStage-v5.0
bash start_clipstage.sh
```
It mounts volumes, starts Typesense, then starts uvicorn. The line "Starting ClipStage API on 0.0.0.0:8000"
confirms the bind. A "collection clips_v2 not found" message is normal on a fresh install.

Terminal window 2:
```sh
cd ~/Documents/ClipStage-v5.0
curl -s http://127.0.0.1:8000/health            # {"status":"ok"}
lsof -iTCP:8000 -sTCP:LISTEN                    # must show *:8000
```
From **another machine**: open `http://<this-mac-ip>:8000`.

## 7. Build the first index (fresh install: NO migration needed)
```sh
./clipstage_run.sh indexer.py --dry             # counts only, writes nothing
./manual_index.sh PLAYOUT                       # quick trial on one volume
./manual_index.sh                               # everything (leave running)
./clipstage_run.sh generate_thumbs.py --limit 2000    # optional; nightly job continues it
```
Always use `./clipstage_run.sh` or `./manual_index.sh`, never `python3 indexer.py` directly (no `.env` loaded).

## 8. Verify
1. Sign in as admin at `http://<this-mac-ip>:8000` (also from another machine).
2. Search a filename word; `first laun` finds `Launch_First...`.
3. Preview plays; add a note and find the clip by the note.
4. Stage two clips; links appear in `/Users/Shared/staging/<NAME>/` and play from an editor's Mac.
5. `GET /admin/health` (as admin) shows version `5.0.0`, documents > 0.

## 9. Nightly schedule (no extra env setup needed)
```sh
bash install_nightly.sh
launchctl kickstart -k gui/$(id -u)/com.clipstage.indexer      # test now
tail -f indexer.log                                           # ends with "Finished (exit 0)"
```
The job loads `.env` itself and does not use the bind address. Default time is 17:00 (edit `Hour`
in `com.clipstage.indexer.plist`, then re-run `install_nightly.sh`). It runs only while this Mac user is
logged in and the Mac is awake. If it sees empty volumes, allow `/bin/bash` access to network volumes in
Privacy & Security.

## 10. HTTPS (recommended)
Plain HTTP exposes passwords on the network. To fix: `brew install caddy`, set the name in
`Caddyfile.sample`, `caddy trust`, install Caddy's root certificate on editors' Macs, then in `.env` set
`CLIPSTAGE_BIND_HOST=127.0.0.1` and `CLIPSTAGE_COOKIE_SECURE=1` and restart `bash start_clipstage.sh`.
Editors then use the `https://` name.

## 11. Hand-over checklist
- [ ] Admin and editors can log in from their own machines; expiry dates set.
- [ ] Search, preview, notes, staging work from an editor's Mac.
- [ ] Nightly job tested; log ends in `exit 0`.
- [ ] `clipstage.db` backup scheduled: `sqlite3 clipstage.db ".backup '/backup/clipstage-$(date +%F).db'"`; also keep `.env` and `editors.json`.
- [ ] HTTPS done, or a date agreed.
- [ ] Typesense key generated fresh and stored safely.

## Troubleshooting
| Symptom | Fix |
| --- | --- |
| Other machine: "site can't be reached" / `ERR_CONNECTION_REFUSED` | `CLIPSTAGE_BIND_HOST` is `127.0.0.1` or missing. Set `0.0.0.0` in `.env`, rerun `bash start_clipstage.sh`, check `lsof -iTCP:8000`, allow Python in the firewall. |
| `TYPESENSE_KEY ... required` | `.env` missing, or run via `./clipstage_run.sh`. |
| typesense-server not found | Set `TYPESENSE_BIN=/full/path` in `.env`. |
| Nothing indexed | Volume names differ from `CLIPSTAGE_SCAN_VOLUMES`. |
| Search 503 | Typesense down, key mismatch, or `clips_v2` not built yet (step 7). |
| Login 503 | App started without `.env`; use `bash start_clipstage.sh`. Or re-run `supabase_profiles_setup.sql`. |
| "account expired" | No row in `clipstage_profiles` or `valid_through` in the past. |
| Editor dropdown empty | Not signed in, or `editors.json` key is not exactly `"editors"`. |
