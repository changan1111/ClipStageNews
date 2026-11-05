# ClipStage - one-command install (macOS and Windows)

Two kinds of machine:

* **Middle machine** - runs ClipStage (search, API, indexer) and shares the staging folder. One per newsroom.
* **Editor machine** - an editor's computer. Opens ClipStage, stages clips, drags them into the editing software.

Every installer is **one command**, asks only what it cannot know, and is **safe to run again** (it repairs
and upgrades; it keeps your `.env`, notes and index).

| Machine | Command (run in the ClipStage folder) |
| --- | --- |
| Middle machine - **Mac** | `bash install-mac-host.sh` |
| Middle machine - **Windows** | `powershell -ExecutionPolicy Bypass -File .\install-win-host.ps1` |
| Editor - **Mac** | `bash install-mac-editor.sh --host <middle-machine-ip>` |
| Editor - **Windows** | the one line the Windows installer prints at the end (see below) |

Use the same system on both sides: a **Windows** middle machine with **Windows** editors, or a **Mac** middle machine with **Mac** editors. Mixing the two is not set up or tested.

---

## What every installer shows you first

1. **A plan:** what is already installed and what *will be* installed (on a Mac this includes Homebrew, and the
   installer says so and asks before installing it; it is installed only when something is actually missing).
   Answer **N** and nothing is changed.
2. **Your entries, for checking:** Supabase URL and key, NAS address and login (the password is hidden), volumes,
   this machine's address, editor names - with a live check of Supabase and the NAS. Type **C** to confirm, or **E**
   to edit: it asks every entry again (Enter keeps the current value) and shows the summary once more.

## Before you start (have these ready)

* Supabase **Project URL** and **anon/publishable key** (Supabase -> Project Settings -> API). Not the service-role key.
* The **NAS address** and a NAS login (domain, user, password), and the share names (default list is in `.env.sample`).
* Internet access on the machine (downloads Python, ffmpeg, Typesense, ...).
* An administrator account on the machine.

## Middle machine - Mac

```bash
cd ~/Downloads/ClipStage        # wherever you unzipped it
bash install-mac-host.sh
```

It installs Homebrew, Python, ffmpeg and Typesense; builds the Python environment; asks for the Supabase and NAS
details; writes `.env` (owner-only) with a **new random Typesense key**; mounts the NAS; creates and shares the
staging folder; opens the firewall for Python; installs the three background services (API at login, nightly
index + backup at 17:00, NAS watchdog); checks `/health`; offers to start the first index.
The project is moved to `~/Documents/ClipStage` if you ran it from Downloads/Desktop.

Everyday: `./clipstage.sh status | restart | logs | index | stop-index | backup | open`

> **One thing macOS may insist on doing by hand:** switching on *File Sharing*. If the installer cannot, it opens
> System Settings and tells you the three clicks (Sharing -> File Sharing on -> add `/Users/Shared/staging`, name `staging`).

## Middle machine - Windows

Open PowerShell (any window), go to the ClipStage folder, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\install-win-host.ps1
```

Accept the administrator prompt. It installs Python and ffmpeg, builds the environment (search runs on SQLite, built into Python - no Typesense, no WSL), asks the same
questions as the Mac installer, then sets up NAS access, the staging share, firewall rules, the background tasks
and an **editor kit**. At the end it prints the one-line command for the editors' PCs.

* No restart is needed: the default Windows search engine (SQLite) does not use WSL2. Only if you choose the older
  Typesense engine (`SEARCH_BACKEND=typesense`) will Windows restart once to switch on WSL2; the installer then
  continues automatically when you sign in again.
* Project goes to `C:\ClipStage`, staging to `C:\ClipStage-Staging`, tools to `C:\ClipStage-tools`.
* Everyday: `cd C:\ClipStage; .\clipstage.ps1 status` (also `start`, `stop`, `restart`, `logs`, `index`, `stop-index`, `backup`, `open`).

## Editor machine - Windows

On the editor PC, paste the single line printed by the middle machine's installer. It looks like:

```powershell
cmdkey /add:10.1.10.5 /user:MIDDLEPC\clipstage-editor /pass:<password>; powershell -ExecutionPolicy Bypass -File \\10.1.10.5\clipstage-setup\install-win-editor.ps1
```

(Print it again any time: `.\clipstage.ps1 editor-kit` on the middle machine.) It asks for administrator rights
once, then: lets Windows follow staged links, stores the logins, connects `C:\Volumes\<share>` to the NAS shares
(same names as on the middle machine), installs Node.js + the ClipStage desktop app, and puts a **ClipStage
shortcut on the desktop**. It asks for the editor's NAS login (skip if the PC already reaches the NAS).

## Editor machine - Mac

Copy the ClipStage folder to the Mac and run `bash install-mac-editor.sh --host <middle-machine-ip>`. It mounts the
NAS shares and the staging share (and re-mounts them every 5 minutes), and installs `/Applications/ClipStage.app`
(`--no-app` to skip).

---

## Big first index (100,000+ clips)

The first index reads every clip once (roughly hours for a large NAS). Progress is **saved every 2,000 clips**, so a
crash, restart or Stop keeps what is already done - the next run does not read those clips again. When a run finishes
cleanly, later runs only check folder times (minutes). The log's `Timings:` line shows where the time went
(`./clipstage.sh logs` / `.\clipstage.ps1 logs`).

## Stopping an index that is running

While an index runs, admins see a red **■ Stop** button on the progress bar under the header (editors can watch the
progress but not stop it). It asks for confirmation, then the index ends within seconds, at its next checkpoint.
**Nothing is deleted** - a stopped run never prunes and never marks folders as done, so the next run (or the nightly
one) simply carries on. If the indexer is stuck (for example waiting on an unreachable NAS) and has not stopped after
30 seconds (`CLIPSTAGE_STOP_GRACE` in `.env`), ClipStage ends it by force - only after confirming the process really
is `indexer.py`. Same on Mac and Windows; from a terminal: `./clipstage.sh stop-index` / `.\clipstage.ps1 stop-index`.

## The one step nobody can script: logins

Users live in Supabase. After the installer finishes (it copies the SQL to your clipboard and opens the SQL editor):
**Authentication -> Users -> Add user** for yourself and each editor, then run `supabase_profiles_setup.sql`
and give each user a role + expiry (examples at the bottom of the file). Then open `http://<middle-machine-ip>:8000`.

---

## How the Windows version works (and what differs from the Mac)

| Mac | Windows |
| --- | --- |
| NAS at `/Volumes/<share>` | `C:\Volumes\<share>` - a folder link to `\\NAS\<share>`; same layout, so indexed paths and staged links work the same |
| Search: Typesense (`SEARCH_BACKEND=typesense`) | Search: **SQLite FTS5** (`SEARCH_BACKEND=sqlite`, built into Python - nothing to install or run). Same code base and same results in the web page. (Typesense can still be used on Windows inside WSL2, but it is no longer needed.) |
| launchd agents | Task Scheduler tasks `ClipStage-API`, `-Typesense`, `-Nightly`, `-MountWatch` (run while the installing user is signed in, like the Mac agents) |
| macOS File Sharing, smb:// | Windows share `\\HOST\staging` (read-only for editors through a dedicated `clipstage-editor` account) |
| Staged clips = symlinks | Same. Editors' PCs must allow remote symlinks (the editor installer sets `fsutil ... SymlinkEvaluation`) and have the same `C:\Volumes\<share>` names |
| Finder button | "Open staging folder": the desktop app opens Explorer; in a plain browser the `\\HOST\staging\NAME` path is copied for you |

Security notes: the editors' share is read-only, so nobody can write through a staged link into the footage;
`C:\ClipStage` and the tool folders are locked to administrators (they hold scripts that run elevated);
`.env` holds the NAS password in plain text (owner/admin access only), like on the Mac.

## If something does not work

| Symptom | Fix |
| --- | --- |
| Windows: "Typesense did not start" | `wsl -l -v` must list `ClipStage` as version 2; BIOS virtualization (VT-x/SVM) must be on; log: `C:\ClipStage\typesense.log` |
| Windows installer says it cannot download the Linux image | `wsl --install -d Ubuntu-24.04` (make any user, `exit`), then re-run with `-WslDistro Ubuntu-24.04` |
| Editor sees staged clips but cannot open them (Windows) | "The symbolic link cannot be followed" -> run the editor installer again (as admin); also check `C:\Volumes\<share>` opens |
| Mac: staging share missing | System Settings -> General -> Sharing -> File Sharing |
| NAS volume shows FAILED | wrong NAS address/login or share name; re-run the installer with `--reconfigure` / `-Reconfigure` |
| Anything else | `./clipstage.sh status` or `.\clipstage.ps1 status`, then the logs: `api.log`, `indexer.log` |

Windows install log: `C:\ClipStage-install.log`. Editor PC log: `%TEMP%\clipstage-editor-install.log`.
