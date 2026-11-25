"""Main-catalogue Streaming visibility is independent of persisted watch/rating state."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from home_media_catalog.connectors.base import Record
from home_media_catalog.streaming import StreamingStateRow, _merge


def streaming_work(store, tmdb_id: int, kind: str = "movie") -> str:
    created = store.save_internal_media({
        "id": str(uuid4()),
        "kind": kind,
        "title": f"Streaming {tmdb_id}",
        "tmdb_id": str(tmdb_id),
        "format": "",
        "streaming_reference": True,
    })
    return created["id"]


def journal_like(store, tmdb_id: int, *, liked: bool, kind: str = "movie",
                 seconds: int = 0) -> None:
    _merge(store, [StreamingStateRow(
        key=f"item:{'tv' if kind == 'tvshow' else 'movie'}:{tmdb_id}",
        payload={
            "status": "saved",
            "liked": "true" if liked else "false",
            "tmdb_id": str(tmdb_id),
        },
        modified_at=(datetime(2026, 11, 23, tzinfo=UTC)
                     + timedelta(seconds=seconds)).isoformat(),
        deleted=False,
    )])


def test_default_grid_hides_only_unliked_streaming_only_works(store):
    rated = streaming_work(store, 111101)
    watched = streaming_work(store, 111102)
    liked = streaming_work(store, 111103)
    listed = streaming_work(store, 111104)
    collected = streaming_work(store, 111105)
    mixed = streaming_work(store, 111106)
    store.update_personal(rated, {"user_rating": 8.0})
    store.update_personal(watched, {"watched": True})
    journal_like(store, 111103, liked=True)
    # A non-liked journal row must not count as a heart.
    journal_like(store, 111101, liked=False)
    user_list = store.save_list({"name": "Watch later", "description": ""})
    store.change_list_item(user_list["id"], listed)
    with store.connect() as db:
        db.execute(
            "INSERT INTO collections"
            "(id,name,normalized_name,merge_key,revision,is_manual) "
            "VALUES('visibility-manual','Keep','keep','keep',1,1)"
        )
        db.execute(
            "INSERT INTO collection_items(collection_id,work_id) "
            "VALUES('visibility-manual',?)", (collected,)
        )
    kodi = store.save_source({"name": "Kodi", "type": "kodi"})["id"]
    store.apply_snapshot(kodi, [Record(
        "movie:111106", "movie", "Streaming 111106",
        identifiers={"tmdb": "111106"},
    )])
    mixed_titles = [m for m in store.media()["items"]
                    if m["title"] == "Streaming 111106"]
    assert len(mixed_titles) == 1
    mixed = mixed_titles[0]["id"]
    assert {"kodi", "internal"} <= set(mixed_titles[0]["source_types"])
    # Default/raw API still returns all six works for local-first replication.
    assert store.media()["total"] == 6
    visible = store.media(include_streaming_personal=False)
    assert visible["total"] == 4
    assert {m["id"] for m in visible["items"]} == {liked, listed, collected, mixed}
    assert rated not in {m["id"] for m in visible["items"]}
    assert watched not in {m["id"] for m in visible["items"]}
    # Pagination, count and filters use the same server-side SQL predicate.
    first = store.media(include_streaming_personal=False, limit=2, offset=0)
    second = store.media(include_streaming_personal=False, limit=2, offset=2)
    assert first["total"] == second["total"] == 4
    assert len(first["items"]) == len(second["items"]) == 2
    assert not set(m["id"] for m in first["items"]).intersection(
        m["id"] for m in second["items"]
    )


def test_streaming_heart_toggle_and_tv_identity(store):
    tv = streaming_work(store, 222201, kind="tvshow")
    assert store.media(kind="tvshow", include_streaming_personal=False)["total"] == 0
    journal_like(store, 222201, liked=True, kind="tvshow", seconds=0)
    visible = store.media(kind="tvshow", include_streaming_personal=False)
    assert [item["id"] for item in visible["items"]] == [tv]
    journal_like(store, 222201, liked=False, kind="tvshow", seconds=1)
    assert store.media(kind="tvshow", include_streaming_personal=False)["total"] == 0
    # A raw query still contains the title after the heart is removed.
    assert store.media(kind="tvshow")["total"] == 1
