"""SQLite FTS5 search backend (the Windows engine): same behaviour the Typesense setup gives."""
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import search_backend as sb


def mk(i, fn, **kw):
    d = dict(id=f"id{i:04d}", filename=fn, path="/V/" + fn, size_mb=float(i), date=f"2026-01-{(i % 28) + 1:02d}",
             category="NEWS", volume="EDIT", folder="NEWS", ext=fn.rsplit(".", 1)[-1].upper())
    d.update(kw)
    return d


@pytest.fixture()
def b():
    be = sb.SqliteBackend(Path(tempfile.mkdtemp()) / "s.db")
    assert be.upsert([
        mk(1, "AMMA_UNAVAGAM-LAUNCH.mxf"),
        mk(2, "Launch_First_Movie.mxf", use_count=5),
        mk(3, "மழை_செய்தி-நேரலை.mov", notes="சென்னை மழை"),
        mk(4, "Weather_Report.mxf", reporter="Gokul launch"),
        mk(5, "x_launch.mxf", hidden=True),
        mk(6, "CM_speech(final)+v2.mp4", volume="EDIT2", folder="CM"),
    ], "upsert") == 0
    return be


def names(r):
    return sorted(h["document"]["filename"] for h in r["hits"])


def test_words_any_order_and_last_word_prefix(b):
    assert names(b.search("launch amma")) == ["AMMA_UNAVAGAM-LAUNCH.mxf"]
    assert names(b.search("amma laun")) == ["AMMA_UNAVAGAM-LAUNCH.mxf"]
    assert names(b.search("unavagam launch mxf")) == ["AMMA_UNAVAGAM-LAUNCH.mxf"]
    assert names(b.search("speech final v2")) == ["CM_speech(final)+v2.mp4"]


def test_earlier_words_must_be_complete(b):
    assert b.search("laun amma")["total"] == 0


def test_all_words_must_match_inside_one_field(b):
    b.update("id0002", {"reporter": "Gokul"})
    # "Launch_First_Movie" has launch in the filename and gokul in the reporter: not ONE field -> no match
    assert names(b.search("launch gokul")) == ["Weather_Report.mxf"]     # reporter holds both words
    assert names(b.search("gokul launch")) == ["Weather_Report.mxf"]


def test_tamil_words_and_prefixes(b):
    assert names(b.search("சென்னை")) == ["மழை_செய்தி-நேரலை.mov"]
    assert names(b.search("மழ")) == ["மழை_செய்தி-நேரலை.mov"]
    assert names(b.search("நேரலை")) == ["மழை_செய்தி-நேரலை.mov"]


def test_hidden_clips_never_appear_and_restore_brings_them_back(b):
    assert "x_launch.mxf" not in names(b.search("launch"))
    b.update("id0005", {"hidden": False})
    assert "x_launch.mxf" in names(b.search("launch"))
    b.update("id0005", {"hidden": True})
    assert b.list_hidden()[1] == 1


def test_filters_facets_and_empty_query(b):
    assert b.search("speech", volume="EDIT")["total"] == 0
    assert b.search("speech", volume="EDIT2")["total"] == 1
    r = b.search("mxf")
    assert r["facets"]["volume"] == [{"value": "EDIT", "count": 3}]
    assert b.search("---")["total"] == 0
    assert names(b.search("mxf", ext="mxf")) == names(b.search("mxf"))


def test_sorts(b):
    order = lambda s: [h["document"]["filename"] for h in b.search("mxf", sort=s)["hits"]]
    assert order("size_desc")[0] == "Weather_Report.mxf"
    assert order("size_asc")[0] == "AMMA_UNAVAGAM-LAUNCH.mxf"
    assert order("most_used")[0] == "Launch_First_Movie.mxf"


def test_popularity_breaks_ties_within_a_relevance_band(b):
    docs = [mk(100 + i, f"Clip_{i}.mxf", use_count=i) for i in range(10)]
    b.upsert(docs, "upsert")
    top = b.search("clip")["hits"][0]["document"]
    assert top["use_count"] >= 8                       # best band, most used first


def test_highlight_marks_whole_matching_words(b):
    h = b.search("amma laun")["hits"][0]["hl"]
    assert h == f"{sb.HL_START}AMMA{sb.HL_END}_UNAVAGAM-{sb.HL_START}LAUNCH{sb.HL_END}.mxf"


def test_notes_update_is_searchable_and_emplace_merges(b):
    b.update("id0001", {"notes": "breaking story"})
    assert names(b.search("breaking")) == ["AMMA_UNAVAGAM-LAUNCH.mxf"]
    b.upsert([{"id": "id0006", "duration": "00:01:00", "probed": True}], "emplace")
    d = b.get("id0006")
    assert d["filename"] == "CM_speech(final)+v2.mp4" and d["duration"] == "00:01:00" and d["probed"] is True


def test_update_of_missing_clip_fails_and_delete_removes(b):
    with pytest.raises(KeyError):
        b.update("nope", {"notes": "x"})
    b.delete_ids(["id0002"])
    assert b.search("movie")["total"] == 0 and b.get("id0002") is None


def test_paging_and_truncation(b):
    b.upsert([mk(200 + i, f"Page_{i}.mxf") for i in range(30)], "upsert")
    assert len(b.search("page", want_all=False, per_page=10, page=3)["hits"]) == 10
    r = b.search("page", max_results=12)
    assert len(r["hits"]) == 12 and r["truncated"] and r["total"] == 30


def test_export_and_setup_force(b):
    assert len(b.export_all()) == 6
    b.setup(force=True)
    assert b.export_all() == {}
