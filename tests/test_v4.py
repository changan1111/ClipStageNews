"""v4.0 additions: indexer safety guards, backup/restore, thumbnail generator (real ffmpeg),
shared configuration, the safe .env loader, launch scripts, and packaging hygiene."""
import json
import os
import plistlib
import re
import shutil
import subprocess
import time
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import generate_thumbs as gt
import indexer

ROOT = Path(__file__).resolve().parent.parent


# -- indexer: prune guard ------------------------------------------------------

class RecClient:
    """Records deletes / imports. Mimics client.collections[name].documents[...]."""
    def __init__(self):
        self.deleted, self.imports = [], []

    @property
    def collections(self):
        return {indexer.COLLECTION: self}

    @property
    def documents(self):
        return self

    def delete(self, params=None):
        self.deleted.append(params)
        return {}

    def __getitem__(self, uid):
        outer = self
        return type("D", (), {"delete": lambda s: outer.deleted.append(uid)})()

    def import_(self, batch, params):
        self.imports.append((list(batch), params))
        return [{"success": True}] * len(batch)

    def export(self):
        return "\n".join(json.dumps(d) for batch, _ in self.imports for d in batch)

    def retrieve(self):
        return {"fields": [{"name": f["name"]} for f in indexer.SCHEMA["fields"]],
                "token_separators": indexer.TOKEN_SEPARATORS}

    def update(self, x):
        pass


@pytest.fixture()
def idx(monkeypatch, tmp_path):
    rc = RecClient()
    monkeypatch.setattr(indexer.backend, "client", rc)
    monkeypatch.setattr(indexer, "STATUS_FILE", tmp_path / "status.json")
    monkeypatch.setattr(indexer, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(indexer, "ALLOW_MASS_PRUNE", False)
    indexer.WARNINGS.clear()
    return rc


def _world(total, missing, vol="EDIT", ok=True):
    existing = {f"id{i}": {"id": f"id{i}", "volume": vol, "path": f"/Volumes/{vol}/f{i}.mxf"}
                for i in range(total)}
    seen = {f"id{i}" for i in range(missing, total)}
    return existing, SimpleNamespace(vols={vol: {"ok": ok, "seen": seen}})


def _deleted_count(rc):
    n = 0
    for d in rc.deleted:
        n += len(d["filter_by"].split(",")) if isinstance(d, dict) else 1
    return n


def test_prune_allows_normal_deletions(idx):
    existing, ctx = _world(1000, 10)
    indexer.prune_missing(existing, ctx, dry=False)
    assert _deleted_count(idx) == 10 and not indexer.WARNINGS


def test_prune_refuses_mass_deletion(idx):
    existing, ctx = _world(1000, 400)          # 40 % "missing" = half-mounted NAS
    indexer.prune_missing(existing, ctx, dry=False)
    assert _deleted_count(idx) == 0
    assert indexer.WARNINGS and "REFUSED" in indexer.WARNINGS[0] and "EDIT" in indexer.WARNINGS[0]


def test_prune_refuses_when_volume_looks_empty_even_if_small(idx):
    existing, ctx = _world(30, 30)             # under the 50-clip floor, but the scan saw NOTHING
    indexer.prune_missing(existing, ctx, dry=False)
    assert _deleted_count(idx) == 0 and indexer.WARNINGS


def test_prune_is_per_volume(idx):
    e1, c1 = _world(1000, 400, "EDIT")
    e2, c2 = _world(1000, 5, "INGEST")
    existing = {**e1, **{k.replace("id", "x"): {**v, "id": k.replace("id", "x")} for k, v in e2.items()}}
    seen2 = {k.replace("id", "x") for k in c2.vols["INGEST"]["seen"]}
    ctx = SimpleNamespace(vols={"EDIT": c1.vols["EDIT"], "INGEST": {"ok": True, "seen": seen2}})
    indexer.prune_missing(existing, ctx, dry=False)
    assert _deleted_count(idx) == 5            # INGEST pruned, EDIT protected
    assert any("EDIT" in w for w in indexer.WARNINGS)


def test_prune_override_and_unreadable_volume(idx, monkeypatch):
    existing, ctx = _world(1000, 400)
    monkeypatch.setattr(indexer, "ALLOW_MASS_PRUNE", True)
    indexer.prune_missing(existing, ctx, dry=False)
    assert _deleted_count(idx) == 400
    idx.deleted.clear()
    existing, ctx = _world(1000, 400, ok=False)   # scan had read errors -> never prune
    indexer.prune_missing(existing, ctx, dry=False)
    assert _deleted_count(idx) == 0


def test_prune_dry_run_deletes_nothing(idx):
    existing, ctx = _world(1000, 10)
    indexer.prune_missing(existing, ctx, dry=True)
    assert _deleted_count(idx) == 0


# -- indexer: backup / restore -------------------------------------------------

def test_backup_and_restore_roundtrip(idx, monkeypatch, tmp_path):
    docs = {"a": {"id": "a", "notes": "keep me", "tags_custom": "flood", "use_count": 3},
            "b": {"id": "b", "notes": "", "reporter": "Meena"}}
    f = indexer.backup_existing(docs)
    assert f.exists() and len(f.read_text().splitlines()) == 2
    resynced = []
    monkeypatch.setattr(indexer, "resync_user_data", lambda existing: resynced.append(existing))
    indexer.restore_backup(f)
    restored = [d for batch, _ in idx.imports for d in batch]
    assert {d["id"]: d for d in restored} == docs
    assert idx.imports[0][1] == {"action": "upsert"}
    assert resynced, "restore must re-apply live SQLite notes so it can never roll user data back"


def test_backup_keeps_only_recent_files(idx):
    for _ in range(7):
        time.sleep(1.05)
        indexer.backup_existing({"a": {"id": "a"}}, keep=5)
    assert len(list(indexer.BACKUP_DIR.glob("clips_*.jsonl"))) == 5


def test_force_backup_failure_aborts_before_anything_is_deleted(idx, monkeypatch, tmp_path):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")                                    # backups/ cannot be created here
    monkeypatch.setattr(indexer, "BACKUP_DIR", blocker / "backups")
    with pytest.raises(OSError):
        indexer.backup_existing({"a": {"id": "a"}})
    assert idx.deleted == []


def test_schema_carries_all_editor_fields():
    names = {f["name"] for f in indexer.SCHEMA["fields"]}
    assert {"tags_custom", "reporter", "location", "description", "notes", "use_count", "hidden"} <= names
    assert "_" in indexer.SCHEMA["token_separators"]


# -- shared configuration ------------------------------------------------------

def test_one_clip_id_everywhere():
    import api
    import clipstage_config as cfg
    p = "/Volumes/EDIT/NEWS/Launch_First.mxf"
    assert api.clip_id_for(p) == cfg.clip_uid(p) == gt.clip_uid(p)
    assert len(cfg.clip_uid(p)) == 16


def test_programs_share_volumes_and_collection():
    import api
    import clipstage_config as cfg
    assert indexer.SCAN_VOLUMES == api.SCAN_VOLUMES == gt.SCAN_VOLUMES == cfg.SCAN_VOLUMES
    assert indexer.COLLECTION == api.COLLECTION == gt.COLLECTION == cfg.COLLECTION


def test_api_does_not_import_the_indexer_module():
    src = (ROOT / "api.py").read_text()
    assert not re.search(r"^\s*(from|import)\s+indexer\b", src, re.M)


# -- api: UI file + thumbnails -------------------------------------------------

@pytest.fixture()
def client(monkeypatch, tmp_path):
    import api
    from fastapi.testclient import TestClient
    who = api.Who("a@x.com", "admin", date.today() + timedelta(days=30), "")
    monkeypatch.setattr(api, "_current_identity", lambda token: who if token else None)
    monkeypatch.setattr(api, "THUMB_DIR", tmp_path / "thumbs")
    (tmp_path / "thumbs").mkdir()
    return TestClient(api.app), api


def test_newest_ui_file_wins(client, monkeypatch, tmp_path):
    c, api = client
    static, base = tmp_path / "static", tmp_path / "base"
    static.mkdir(); base.mkdir()
    (static / "index.html").write_text("<html>OLD static copy</html>")
    (base / "index.html").write_text("<html>NEW next to api.py</html>")
    os.utime(static / "index.html", (time.time() - 3600,) * 2)
    monkeypatch.setattr(api, "STATIC_DIR", static)
    monkeypatch.setattr(api, "BASE_DIR", base)
    assert "NEW next to api.py" in c.get("/").text
    os.utime(base / "index.html", (time.time() - 7200,) * 2)   # now the static copy is newer
    assert "OLD static copy" in c.get("/").text


def test_zero_byte_thumbnail_is_treated_as_missing(client, tmp_path):
    c, api = client
    h = {"Authorization": "Bearer t"}
    (tmp_path / "thumbs" / "empty000empty000.jpg").write_bytes(b"")
    (tmp_path / "thumbs" / "good0000good0000.jpg").write_bytes(b"\xff\xd8jpegdata")
    assert c.get("/thumb/empty000empty000", headers=h).status_code == 404
    assert c.get("/thumb/good0000good0000", headers=h).status_code == 200


# -- thumbnails (real ffmpeg) --------------------------------------------------

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _mk_video(path, secs):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc=duration={secs}:size=320x180:rate=10", "-pix_fmt", "yuv420p", str(path)],
                   check=True)


@pytest.fixture()
def thumbs(tmp_path, monkeypatch):
    td = tmp_path / "thumbs"
    td.mkdir()
    monkeypatch.setattr(gt, "THUMB_DIR", str(td))
    return td


@needs_ffmpeg
def test_thumb_generated_for_normal_and_very_short_clips(thumbs, tmp_path):
    long_, short = tmp_path / "long.mp4", tmp_path / "short.mp4"
    _mk_video(long_, 6)
    _mk_video(short, 1)                               # 1 s clip has no frame at 3 s -> needs fallback
    assert gt.make_thumb("long1", str(long_)) == "generated"
    assert gt.make_thumb("short1", str(short)) == "generated"
    for uid in ("long1", "short1"):
        assert (thumbs / f"{uid}.jpg").stat().st_size > 0
    assert not list(thumbs.glob("*.tmp"))             # nothing half-written left behind


@needs_ffmpeg
def test_failed_thumb_leaves_marker_not_an_empty_jpg(thumbs, tmp_path):
    bad = tmp_path / "corrupt.mp4"
    bad.write_bytes(os.urandom(2048))
    assert gt.make_thumb("bad1", str(bad)) == "failed"
    assert not (thumbs / "bad1.jpg").exists() and (thumbs / "bad1.fail").exists()


def test_unmounted_volume_is_missing_not_failed(thumbs):
    assert gt.make_thumb("gone1", "/Volumes/NOT_MOUNTED/x.mxf") == "missing"
    assert not list(thumbs.iterdir())


def test_pending_logic_retry_window_legacy_zero_byte_and_limit(thumbs):
    (thumbs / "have.jpg").write_bytes(b"jpegdata")
    (thumbs / "zero.jpg").write_bytes(b"")                       # legacy timeout leftover
    (thumbs / "recent.fail").write_text("1")
    (thumbs / "old.fail").write_text("1")
    os.utime(thumbs / "old.fail", (time.time() - 30 * 86400,) * 2)
    clips = [(u, f"/p/{u}.mxf") for u in ("have", "zero", "recent", "old", "new1", "new2")]
    todo, cached, cooling = gt.pending(clips, retry_failed=False, limit=0)
    assert [u for u, _ in todo] == ["zero", "old", "new1", "new2"] and cached == 1 and cooling == 1
    assert not (thumbs / "zero.jpg").exists()                    # legacy empty file cleared
    todo, _, cooling = gt.pending(clips, retry_failed=True, limit=0)
    assert "recent" in [u for u, _ in todo] and cooling == 0
    assert len(gt.pending(clips, False, limit=2)[0]) == 2


def test_thumbs_read_the_configured_collection(monkeypatch):
    seen = {}

    class C:
        collections = {}
    class Coll:
        class documents:
            @staticmethod
            def export(_p=None):
                return json.dumps({"id": "a1", "path": "/Volumes/EDIT/x.mxf"})
    class FakeTS:
        def __init__(self, cfg): pass
        @property
        def collections(self):
            class M(dict):
                def __getitem__(s, k):
                    seen["name"] = k
                    return Coll
            return M()
    import search_backend
    monkeypatch.setattr(search_backend.get_backend(), "client", FakeTS(None))
    assert gt.clips_from_index() == [("a1", "/Volumes/EDIT/x.mxf")]
    assert seen["name"] == gt.COLLECTION == "clips_v2"


@needs_ffmpeg
def test_walk_mode_end_to_end(thumbs, tmp_path, monkeypatch):
    vols = tmp_path / "Volumes"
    (vols / "EDIT" / "a").mkdir(parents=True)
    (vols / "EDIT" / "@Recycle").mkdir()
    _mk_video(vols / "EDIT" / "a" / "x.mp4", 4)
    _mk_video(vols / "EDIT" / "@Recycle" / "trash.mp4", 4)       # excluded folder
    monkeypatch.setattr(gt, "VOLUMES_ROOT", vols)
    monkeypatch.setattr(gt, "SCAN_VOLUMES", ["EDIT", "NOPE"])
    assert gt.main(["--walk", "--workers", "2"]) == 0
    jpgs = list(thumbs.glob("*.jpg"))
    assert len(jpgs) == 1 and jpgs[0].stem == gt.clip_uid(str(vols / "EDIT" / "a" / "x.mp4"))
    assert gt.main(["--walk"]) == 0 and len(list(thumbs.glob("*.jpg"))) == 1   # second run: nothing to do


# -- shell: safe .env loader + scripts ----------------------------------------

def test_env_loader_handles_spaces_quotes_and_never_executes(tmp_path):
    shutil.copy(ROOT / "clipstage_env.sh", tmp_path / "clipstage_env.sh")
    marker = tmp_path / "PWNED"
    (tmp_path / ".env").write_text(
        "# comment\n"
        "CLIPSTAGE_SCAN_VOLUMES=EDIT,EDIT2,SHARE FOLDER,TRANSCODER\n"
        'QUOTED="hello world"\n'
        f"EVIL=$(touch {marker})\n"
        "ALREADY=from_file\n"
        "bad-key=1\n")
    env = {**os.environ, "ALREADY": "from_shell"}
    out = subprocess.run(["bash", "-c",
                          '. ./clipstage_env.sh; echo "[$CLIPSTAGE_SCAN_VOLUMES]|[$QUOTED]|[$ALREADY]|[$EVIL]"'],
                         cwd=tmp_path, env=env, capture_output=True, text=True)
    assert "[EDIT,EDIT2,SHARE FOLDER,TRANSCODER]|[hello world]|[from_shell]|[$(touch" in out.stdout, out.stdout + out.stderr
    assert not marker.exists()


def test_env_sample_loads_cleanly_and_has_no_real_secret(tmp_path):
    shutil.copy(ROOT / "clipstage_env.sh", tmp_path / "clipstage_env.sh")
    shutil.copy(ROOT / ".env.sample", tmp_path / ".env")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLIPSTAGE_", "TYPESENSE_"))}
    out = subprocess.run(["bash", "-c", '. ./clipstage_env.sh; echo "[$CLIPSTAGE_SCAN_VOLUMES]|[$TYPESENSE_KEY]"'],
                         cwd=tmp_path, env=env, capture_output=True, text=True)
    assert "SHARE FOLDER,TRANSCODER]" in out.stdout and out.stderr == ""
    assert "[CHANGE_ME" in out.stdout, "the sample must ship a placeholder, never a usable key"


def test_every_script_has_valid_bash_syntax():
    for f in ("clipstage_env.sh", "start_clipstage.sh", "nightly_index.sh", "manual_index.sh", "install_nightly.sh"):
        assert subprocess.run(["bash", "-n", str(ROOT / f)]).returncode == 0, f


def test_launchd_plist_is_valid_xml_with_placeholder():
    data = (ROOT / "com.clipstage.indexer.plist").read_bytes()
    plist = plistlib.loads(data.replace(b"__CLIPSTAGE_HOME__", b"/tmp/x"))
    assert plist["Label"] == "com.clipstage.indexer" and plist["ProgramArguments"][1].endswith("nightly_index.sh")


# -- packaging / documentation hygiene ----------------------------------------

def test_every_env_var_the_code_reads_is_in_env_sample():
    sample = (ROOT / ".env.sample").read_text()
    names = set()
    for f in ROOT.glob("*.py"):
        names |= set(re.findall(r'environ(?:\.get)?\(\s*"((?:CLIPSTAGE|TYPESENSE|SUPABASE)_[A-Z_]+)"', f.read_text()))
        names |= set(re.findall(r'env_int\(\s*"([A-Z_]+)"', f.read_text()))
    names -= {"CLIPSTAGE_OLD_COLLECTION"}            # one-off migration switch, documented in its own header
    missing = sorted(n for n in names if n not in sample)
    assert not missing, f"add to .env.sample: {missing}"


def test_all_three_readmes_and_support_files_ship():
    for f in ("README.md", "README-RUNNING-MACHINE.md", "README-FRESH-INSTALL.md", "architecture.mermaid",
              "requirements.txt", "editors.sample.json", "Caddyfile.sample", "VERSION"):
        assert (ROOT / f).is_file(), f
