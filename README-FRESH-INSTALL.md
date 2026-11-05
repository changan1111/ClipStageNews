# ClipStage v5.0 - Fresh Installation Guide

For a **new Mac with nothing installed**. Follow the steps in order; each ends with a check so you
know it worked before moving on. Already running ClipStage? Use
[`README-RUNNING-MACHINE.md`](README-RUNNING-MACHINE.md) instead. What the tool does:
[`README.md`](README.md).

Time needed: about 1 hour of work, plus however long the first index of your NAS takes (minutes to
hours, depending on file count).

## What you need before you start

| Need | Notes |
| --- | --- |
| A Mac that stays on and logged in | macOS only. It hosts the app, the index and the schedule. |
| Admin rights on that Mac | For installing software and sharing the staging folder. |
| Network access to the NAS | Volumes must mount under `/Volumes` with stable names. |
| A Supabase project (free tier is fine) | Holds the logins. You need its URL and the **anon / publishable** key. Never the service-role key. |
| The ClipStage v5.0 zip | `clipstage-v5.0.zip` |

---

## Step 1 - Install the tools

Install Homebrew first if you do not have it (see brew.sh), then:

```sh
brew install python@3.12 ffmpeg
python3 --version          # must be 3.10 or newer
ffmpeg -version | head -1
ffprobe -version | head -1
```

**Typesense** (the search engine): install the macOS build from the official Typesense downloads
page (typesense.org/docs - "Install" -> macOS), or via the Homebrew tap they publish. Then check:

```sh
which typesense-server     # e.g. /opt/homebrew/bin/typesense-server
```

If it is installed somewhere unusual, you will set `TYPESENSE_BIN` in Step 4.

---

## Step 2 - Set up Supabase (logins)

1. In your Supabase project, open **Authentication -> Providers** and make sure Email is enabled,
   then **disable public sign-ups** (Authentication settings) so only people you create can log in.
2. **Authentication -> Users -> Add user**: create yourself (the admin) and each editor, with
   passwords. Tick "auto confirm".
3. Open the **SQL editor**, paste in the contents of `supabase_profiles_setup.sql` and run it. It
   creates the `clipstage_profiles` table with the correct security policy.
4. Give each user a role and an expiry date. In the SQL editor (change the e-mail addresses and
   dates):

   ```sql
   insert into public.clipstage_profiles (user_id, role, valid_through, editor_name)
   select id, 'admin', '2027-12-31'::date, null
   from auth.users where email = 'you@example.com'
   on conflict (user_id) do update
     set role = excluded.role, valid_through = excluded.valid_through, editor_name = excluded.editor_name;

   insert into public.clipstage_profiles (user_id, role, valid_through, editor_name)
   select id, 'editor', '2027-12-31'::date, 'Meena R'
   from auth.users where email = 'meena@example.com'
   on conflict (user_id) do update
     set role = excluded.role, valid_through = excluded.valid_through, editor_name = excluded.editor_name;
   ```

   `editor_name` locks that login to one staging folder (it must match a name you list in
   `editors.json`). Leave it `null` for a shared login that can act as any editor.
5. **Project Settings -> API**: copy the **Project URL** and the **anon / publishable** key. You
   will paste them into `.env` in Step 4.

Check: `select * from public.clipstage_profiles;` shows your rows.

---

## Step 3 - Install ClipStage

```sh
cd ~/Documents                       # or wherever the app should live
unzip clipstage-v5.0.zip
cd ClipStage-v5.0
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Check: `.venv/bin/python -c "import fastapi, typesense, requests; print('ok')"` prints `ok`.

---

## Step 4 - Configure

```sh
cp .env.sample .env
cp editors.sample.json editors.json
openssl rand -hex 24                 # copy this output for the Typesense key
open -e .env                         # edit in TextEdit (or use any editor)
```

Edit `.env` - the lines you **must** change:

| Setting | Put |
| --- | --- |
| `TYPESENSE_KEY` | the random value you just generated (never reuse one from a chat, email or old file) |
| `SUPABASE_URL` | your project URL |
| `SUPABASE_ANON_KEY` | the anon / publishable key |
| `CLIPSTAGE_SCAN_VOLUMES` | your NAS volume names, comma separated, exactly as they appear in `/Volumes` |
| `CLIPSTAGE_SMB_HOST` | the Mac's address as editors see it (used by the "open in Finder" action) |

Rules for `.env`: one `KEY=value` per line; spaces inside a value are fine
(`SHARE FOLDER`); no quotes needed; never commit this file anywhere.

Settings worth a look: `CLIPSTAGE_STAGING_PATH` (default `/Users/Shared/staging`),
`CLIPSTAGE_BIND_HOST` (leave `127.0.0.1` if you will use HTTPS in Step 9, otherwise see below),
`TYPESENSE_BIN` (only if Step 1 found Typesense somewhere unusual).

Edit `editors.json`: the list of names editors choose when staging. A name must **exactly** match
the `editor_name` of any bound login from Step 2. Letters, numbers, spaces and dots are fine;
slashes are not. The file must look exactly like this - the key is `"editors"` (a typo such as
`"editors.jason"` leaves the dropdown empty):

```json
{ "editors": ["GOKUL", "PRIYA"] }
```

> **Who can reach the app?** With `CLIPSTAGE_BIND_HOST=127.0.0.1` only this Mac can open it - that
> is the right setting once HTTPS (Step 9) is in front. To let editors connect straight away over
> plain HTTP on the LAN, set it to the Mac's LAN address (or `0.0.0.0`). Passwords and cookies are
> then unencrypted on your network - treat that as temporary.

---

## Step 5 - Mount the NAS and prepare staging

1. Mount every NAS share (Finder -> Go -> Connect to Server). Check the names:

   ```sh
   ls /Volumes
   ```

   They must match `CLIPSTAGE_SCAN_VOLUMES` letter for letter (case does not matter). Set the shares
   to reconnect at login (System Settings -> General -> Login Items), or add a `mount_volumes.sh`
   to the ClipStage folder - the start and nightly scripts run it automatically if it exists.
2. Create the staging folder and share it so editors can reach their staged clips:

   ```sh
   mkdir -p /Users/Shared/staging
   ```

   Then add `/Users/Shared/staging` to **System Settings -> General -> Sharing -> File Sharing**
   (names of menus differ slightly between macOS versions). Editors' Macs must also have the NAS
   volumes mounted under the **same names**, because staged clips are symlinks to the NAS paths.

---

## Step 6 - Start ClipStage and build the first index

```sh
bash start_clipstage.sh
```

Leave that window open. It mounts volumes, starts Typesense (listening on this Mac only), then
starts the API. You should see uvicorn report it is running, and possibly a message that the
collection `clips_v2` was not found - normal on a fresh install.

In a **second** Terminal window:

```sh
cd ~/Documents/ClipStage-v5.0
curl -s http://127.0.0.1:8000/health          # {"status":"ok"}
./clipstage_run.sh indexer.py --dry             # counts files only - safe, writes nothing
```

If the count looks right, build the real index. Test with one volume first if you like:

```sh
./manual_index.sh PLAYOUT      # one volume (name from your list) - a quick trial
./manual_index.sh EDIT2 PLAYOUT   # or several volumes at once
./manual_index.sh              # then everything (prints progress; leave it running)
```

The first full run is the slowest; later runs skip unchanged folders and take far less. You can use
ClipStage while it indexes - progress shows in the status line.

Then make thumbnails (optional now; the nightly job also does it, 5,000 a night):

```sh
./clipstage_run.sh generate_thumbs.py --limit 2000
```

---

## Step 7 - Verify everything (do all of these once)

1. **Open the app:** `http://127.0.0.1:8000` on this Mac. Sign in as the admin.
2. **Search:** try a word from a real filename, then two words in any order. Searching
   `first laun` must find a clip called `Launch_First...` (only the last word may be partial).
3. **Preview:** click a clip - video plays (a live transcode, so no seeking).
4. **Notes:** add a note to a clip, close it, search for a word from the note - the clip is found.
5. **Stage:** pick your name, select two clips, **Stage**. Look in `/Users/Shared/staging/<NAME>/`:
   two links should be there. From an **editor's computer**, open the shared staging folder and
   check the clips play. If they do not, the share does not expose symlinks, or that Mac lacks the
   same NAS volume names (see Step 5).
6. **Admin:** the Sync button and volume picker are visible to admins only. `GET /admin/health` (as
   admin) shows version `5.0.0`, a document count above zero and `token_separators` containing `_`.
7. **Lock-out test:** five wrong passwords in a row blocks logins for a few minutes (rate limit).

---

## Step 8 - Schedule the nightly index

```sh
bash install_nightly.sh
launchctl kickstart -k gui/$(id -u)/com.clipstage.indexer     # run it now to test
tail -f indexer.log
```

You want to see `=== Scheduled index ...`, a scan summary, `Pruned N stale entries`, a thumbnail
summary, and `=== Finished (exit 0)`. It then runs daily at 17:00 (change `Hour` in
`com.clipstage.indexer.plist`, re-run `install_nightly.sh`). It only runs while this Mac user is
logged in. To receive failure alerts in Slack/Teams/Discord, set `CLIPSTAGE_ALERT_WEBHOOK` in `.env`.

If the scheduled run sees empty volumes although a manual run works, macOS is blocking background
access to the network volumes - allow `/bin/bash` (or Terminal) access to files on network volumes
in **System Settings -> Privacy & Security**.

---

## Step 9 - HTTPS (recommended before real editors use it)

Over plain HTTP, passwords and the login cookie can be read by anyone on the network.

1. `brew install caddy`
2. Copy `Caddyfile.sample`; set `clipstage.lan` to a name editors' computers resolve (router DNS
   entry or the Mac's Bonjour name such as `newsroom-mac.local`).
3. `caddy trust` on the server. On **every** editor computer install Caddy's root certificate once
   and mark it "Always Trust" (path is written in the sample file). Without it, browsers show
   warnings.
4. In `.env`: `CLIPSTAGE_BIND_HOST=127.0.0.1` and `CLIPSTAGE_COOKIE_SECURE=1`. Restart:
   `bash start_clipstage.sh`.
5. From another computer, both must **fail**: `curl -m 3 http://<mac-ip>:8000` and
   `curl -m 3 http://<mac-ip>:8108/health`. And `https://clipstage.lan` must open the app.

---

## Step 10 - Hand over checklist

- [ ] An admin can log in; every editor can log in; expiry dates are set (`valid_through`).
- [ ] Search, preview, notes and staging work from an **editor's** computer, not only the server.
- [ ] Nightly job tested with `kickstart`; the log ends in `exit 0`.
- [ ] **Backup of `clipstage.db` scheduled** - it holds all notes and cannot be rebuilt:
      `sqlite3 clipstage.db ".backup '/backup/clipstage-$(date +%F).db'"`. Also keep copies of
      `.env` and `editors.json`.
- [ ] HTTPS in place (Step 9) or an agreed date to do it.
- [ ] The Typesense key in `.env` was generated fresh and is stored somewhere safe.
- [ ] You know where alerts go (`CLIPSTAGE_ALERT_WEBHOOK`) and who reads them.
- [ ] Read "Daily operation" and "When an alert says Prune REFUSED" in
      `README-RUNNING-MACHINE.md`.

## If something goes wrong

| Symptom | Fix |
| --- | --- |
| `TYPESENSE_KEY ... required` | `.env` is missing or not in the same folder as `api.py`. |
| "typesense-server not found" | Set `TYPESENSE_BIN=/full/path/to/typesense-server` in `.env`. |
| Nothing is indexed / "volume not mounted" | `ls /Volumes`; names must match `CLIPSTAGE_SCAN_VOLUMES`. |
| Login says unavailable (503) | See "Login returns 503" in `README-RUNNING-MACHINE.md`, Troubleshooting. Most common: the app was started without `.env` (use `bash start_clipstage.sh`), or the profile table has a recursive policy (re-run `supabase_profiles_setup.sql`). |
| Login works but "account expired" | The user has no row in `clipstage_profiles`, or `valid_through` is in the past. |
| Editors cannot open the page | `CLIPSTAGE_BIND_HOST=127.0.0.1` without the HTTPS proxy; see the note in Step 4. |
| `TYPESENSE_KEY environment variable is required` when running a Python tool | Run it as `./clipstage_run.sh <script>.py` so `.env` is loaded. |
| Search shows an error / 503 | Typesense is down, its key differs from `.env`, or `clips_v2` does not exist yet (Step 6). |
| Editor dropdown is empty | Not signed in, `editors.json` missing, or its key is not exactly `"editors"`. |
| Everything else | `README-RUNNING-MACHINE.md`, Troubleshooting section. |
