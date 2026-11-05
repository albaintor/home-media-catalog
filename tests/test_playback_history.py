"""Regression tests for offline history reconciliation and deletion tombstones."""

from home_media_catalog.playback_history import HistoryState, RecentEntry, _merge


def entry(identifier: str, category: str, updated: float, position: float = 4):
    return RecentEntry(
        id=identifier, category=category, kind="movie" if category == "video" else "song",
        title=identifier, position=position, total=20, updatedAt=updated
    )


def test_recent_playback_merges_newest_entry_and_keeps_both_categories(tmp_path):
    database = tmp_path / "recent.db"
    _merge(database, HistoryState(entries=[
        entry("video|a", "video", 100), entry("music|b", "music", 120)
    ]))
    merged = _merge(database, HistoryState(entries=[
        entry("video|a", "video", 90, 1), entry("music|c", "music", 130)
    ]))
    by_id = {item["id"]: item for item in merged["entries"]}
    assert by_id["video|a"]["position"] == 4
    assert set(by_id) == {"video|a", "music|b", "music|c"}


def test_clear_tombstones_prevent_offline_resurrection(tmp_path):
    database = tmp_path / "recent.db"
    _merge(database, HistoryState(entries=[
        entry("video|a", "video", 100), entry("music|b", "music", 100)
    ]))
    cleared = _merge(database, HistoryState(clearedAt={"video": 150}))
    assert [item["id"] for item in cleared["entries"]] == ["music|b"]
    replayed = _merge(database, HistoryState(entries=[
        entry("video|a", "video", 120), entry("video|c", "video", 151)
    ], clearedAt={"video": 120}))
    assert {item["id"] for item in replayed["entries"]} == {"music|b", "video|c"}
    assert replayed["clearedAt"]["video"] == 150


def test_category_clear_does_not_hide_newer_records(tmp_path):
    database = tmp_path / "recent.db"
    first = _merge(database, HistoryState(entries=[
        entry("music|album", "music", 200), entry("video|v", "video", 100)
    ], clearedAt={"music": 180}))
    assert len(first["entries"]) == 2
    final = _merge(database, HistoryState(clearedAt={"music": 210}))
    assert [item["id"] for item in final["entries"]] == ["video|v"]
