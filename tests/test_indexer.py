"""Indexer tests with a fake Typesense: user data merged from SQLite, hidden respected,
a rewritten document never loses its note, prune still works, resync fixes drift."""
import hashlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

TMP = Path(tempfile.mkdtemp())
os.environ.update(SEARCH_BACKEND="typesense", TYPESENSE_KEY="k", CLIPSTAGE_DB=str(TMP / "i.db"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import indexer
import store


class FakeDocs:
    def __init__(self):
        self.saved = {}

    def import_(self, batch, opts):
        for d in batch:
            if opts.get("action") == "update":
                self.saved.setdefault(d["id"], {}).update(d)
            else:
                self.saved[d["id"]] = dict(d)
        return [{"success": True}] * len(batch)

    def export(self):
        import json
        return "\n".join(json.dumps(d) for d in self.saved.values())

    def delete(self, *_a, **_k):
        pass


class FakeColl:
    def __init__(self):
        self.documents = FakeDocs()


class FakeClient:
    def __init__(self):
        self.c = FakeColl()
        self.collections = {indexer.COLLECTION: self.c}


_SAVED = {}


def teardown_module(_):
    """Put back what setup_module changed so other test files see the real configuration."""
    for k, v in _SAVED.items():
        setattr(indexer, k, v)


def setup_module(_):
    for k in ("VOLUMES_ROOT", "SCAN_VOLUMES", "DIRCACHE_FILE", "STATUS_FILE", "FFPROBE"):
        _SAVED[k] = getattr(indexer, k)
    (TMP / "vol" / "EDIT" / "NEWS").mkdir(parents=True)
    indexer.VOLUMES_ROOT = TMP / "vol"
    indexer.SCAN_VOLUMES = ["EDIT"]
    indexer.DIRCACHE_FILE = TMP / "dc.json"
    indexer.STATUS_FILE = TMP / "st.json"
    indexer.FFPROBE = None


def uid_for(p):
    return hashlib.md5(str(p).encode()).hexdigest()[:16]


def test_schema_has_separators_and_hidden():
    assert "_" in indexer.SCHEMA["token_separators"]
    assert {"hidden", "notes", "use_count"} <= {f["name"] for f in indexer.SCHEMA["fields"]}
    assert "tags" not in {f["name"] for f in indexer.SCHEMA["fields"]}


def test_merges_sqlite_user_data_and_keeps_note_on_rewrite(monkeypatch):
    f = TMP / "vol" / "EDIT" / "NEWS" / "Launch_First.mxf"
    f.write_bytes(b"x" * 2048)
    uid = uid_for(f)
    store.init()
    store.set_notes(uid, "from sqlite", str(f))
    store.bump_use_count(uid, str(f))
    store.set_hidden(uid, True, str(f))
    fc = FakeClient()
    monkeypatch.setattr(indexer.backend, "client", fc)
    indexer.index_archive({}, full=True)
    d = fc.c.documents.saved[uid]
    assert d["notes"] == "from sqlite" and d["use_count"] == 1 and d["hidden"] is True
    assert "tags" not in d and d["volume"] == "EDIT" and d["folder"] == "NEWS"

    # File changed on disk; the OLD document (no SQLite row) held a note -> must survive the rewrite
    g = TMP / "vol" / "EDIT" / "NEWS" / "Other.mxf"
    g.write_bytes(b"y" * 4096)
    gid = uid_for(g)
    prev = {"id": gid, "path": str(g), "size_mb": 0.0, "date": "2000-01-01", "volume": "EDIT",
            "folder": "NEWS", "notes": "legacy note", "use_count": 3, "hidden": False}
    indexer.index_archive({gid: prev}, full=True)
    assert fc.c.documents.saved[gid]["notes"] == "legacy note"
    assert fc.c.documents.saved[gid]["use_count"] == 3


def test_resync_repairs_drift(monkeypatch):
    fc = FakeClient()
    monkeypatch.setattr(indexer.backend, "client", fc)
    store.init()
    store.set_notes("drift0000000001", "newer", "/p")
    fc.c.documents.saved["drift0000000001"] = {"id": "drift0000000001", "notes": "stale"}
    indexer.resync_user_data(dict(fc.c.documents.saved))
    assert fc.c.documents.saved["drift0000000001"]["notes"] == "newer"


# -- multi-volume runs (--only A,B) --------------------------------------------

def test_parse_only_accepts_lists_repeats_and_any_case(monkeypatch):
    import indexer
    monkeypatch.setattr(indexer, "SCAN_VOLUMES", ["EDIT", "EDIT2", "PLAYOUT"])
    assert indexer.parse_only(["--prune"]) == []
    assert indexer.parse_only(["--only", "playout"]) == ["PLAYOUT"]
    assert indexer.parse_only(["--only", "PLAYOUT,edit"]) == ["EDIT", "PLAYOUT"]      # canonical order
    assert indexer.parse_only(["--only", "EDIT", "--only", "PLAYOUT", "--only", "edit"]) == ["EDIT", "PLAYOUT"]


def test_parse_only_rejects_typos_instead_of_running_everything(monkeypatch):
    import indexer
    monkeypatch.setattr(indexer, "SCAN_VOLUMES", ["EDIT", "PLAYOUT"])
    with pytest.raises(ValueError, match="PLAYOT"):
        indexer.parse_only(["--only", "EDIT,PLAYOT"])


# ── Stop requests ──────────────────────────────────────────────────────────────────────────────

def test_stop_flag_stops_the_run_before_save_or_prune(monkeypatch, tmp_path):
    import indexer
    flag = tmp_path / "index_stop.flag"
    monkeypatch.setattr(indexer, "INDEX_STOP_FILE", flag)
    monkeypatch.setattr(indexer, "DRY", True)                    # no status file writes
    indexer._STOP_EVENT.clear(); indexer._stop_probe["t"] = 0.0
    assert indexer.stop_requested() is False
    indexer.check_stop()                                         # no flag -> no exception
    flag.write_text("1")
    indexer._stop_probe["t"] = 0.0
    assert indexer.stop_requested() is True
    with pytest.raises(indexer.IndexerStopped):
        indexer.check_stop()
    indexer._STOP_EVENT.clear()


def test_stale_stop_flag_is_ignored_but_a_fresh_one_is_kept(monkeypatch, tmp_path):
    import os, time, indexer
    flag = tmp_path / "index_stop.flag"
    monkeypatch.setattr(indexer, "INDEX_STOP_FILE", flag)
    flag.write_text("old"); old = time.time() - 3600; os.utime(flag, (old, old))
    indexer._clear_stale_stop_flag()
    assert not flag.exists()
    flag.write_text("fresh")
    indexer._clear_stale_stop_flag()
    assert flag.exists()


def test_scan_drains_without_work_after_a_stop(monkeypatch, tmp_path):
    import indexer
    seen = []
    monkeypatch.setattr(indexer, "_process_dir", lambda ctx, q, *a: seen.append(a))
    monkeypatch.setattr(indexer, "stop_requested", lambda: True)
    monkeypatch.setattr(indexer, "set_status", lambda *a, **k: None)
    root = tmp_path / "EDIT"; root.mkdir()
    ctx = indexer.scan_all([root], {}, False, {}, True)
    assert seen == [] and ctx is not None                        # returned promptly, did no folder work


def test_stop_never_reaches_prune(monkeypatch, tmp_path):
    """index_archive must raise IndexerStopped right after the scan, before save/prune are called."""
    import indexer
    (tmp_path / "EDIT").mkdir()
    monkeypatch.setattr(indexer, "VOLUMES_ROOT", tmp_path)
    monkeypatch.setattr(indexer, "SCAN_VOLUMES", ["EDIT"])
    monkeypatch.setattr(indexer, "set_status", lambda *a, **k: None)
    monkeypatch.setattr(indexer, "_load_dircache", lambda: {})
    calls = []
    monkeypatch.setattr(indexer, "save_docs", lambda d: calls.append("save") or 0)
    monkeypatch.setattr(indexer, "probe_and_save", lambda d, p: calls.append("save") or (0, 0.0, 0.0))
    monkeypatch.setattr(indexer, "prune_missing", lambda *a, **k: calls.append("prune"))
    monkeypatch.setattr(indexer, "stop_requested", lambda: True)
    with pytest.raises(indexer.IndexerStopped):
        indexer.index_archive({}, dry=False, prune=True, force=True)
    assert calls == []


# ── Checkpoints: a long first run must not lose its work ───────────────────────────────────────

def _fake_docs(n):
    return [{"id": f"id{i}", "path": f"/Volumes/EDIT/c{i}.mxf"} for i in range(n)]


def test_progress_is_saved_every_checkpoint(monkeypatch):
    import indexer
    monkeypatch.setattr(indexer, "CHECKPOINT", 3)
    monkeypatch.setattr(indexer, "DRY", True)                        # status writes are no-ops
    monkeypatch.setattr(indexer, "FFPROBE", "ffprobe")
    monkeypatch.setattr(indexer, "get_duration", lambda p: ("00:01:00", True))
    saved = []
    monkeypatch.setattr(indexer, "_import_batches", lambda docs: saved.append([d["id"] for d in docs]) or 0)
    docs = _fake_docs(8)
    failed, _, _ = indexer.probe_and_save(docs, docs)
    assert failed == 0
    assert saved == [["id0", "id1", "id2"], ["id3", "id4", "id5"], ["id6", "id7"]]
    assert all(d["duration"] == "00:01:00" and d["probed"] for d in docs)


def test_stop_keeps_what_was_already_saved(monkeypatch):
    import indexer
    monkeypatch.setattr(indexer, "CHECKPOINT", 3)
    monkeypatch.setattr(indexer, "DRY", True)
    monkeypatch.setattr(indexer, "FFPROBE", "ffprobe")
    monkeypatch.setattr(indexer, "get_duration", lambda p: ("00:00:10", True))
    saved = []
    def save(docs):
        saved.append(len(docs))
        if len(saved) == 2:                                          # Stop pressed after 2 checkpoints
            indexer._STOP_EVENT.set()
        return 0
    monkeypatch.setattr(indexer, "_import_batches", save)
    indexer._STOP_EVENT.clear()
    try:
        with pytest.raises(indexer.IndexerStopped):
            indexer.probe_and_save(_fake_docs(10), _fake_docs(10))
    finally:
        indexer._STOP_EVENT.clear()
    assert saved == [3, 3]                                           # 6 clips are safely in the index


def test_only_clips_that_need_it_are_probed(monkeypatch):
    import indexer
    monkeypatch.setattr(indexer, "DRY", True)
    monkeypatch.setattr(indexer, "FFPROBE", "ffprobe")
    probed = []
    monkeypatch.setattr(indexer, "get_duration", lambda p: probed.append(p) or ("00:00:05", True))
    monkeypatch.setattr(indexer, "_import_batches", lambda docs: 0)
    docs = _fake_docs(5)
    indexer.probe_and_save(docs, [docs[1], docs[3]])
    assert sorted(probed) == [docs[1]["path"], docs[3]["path"]]
