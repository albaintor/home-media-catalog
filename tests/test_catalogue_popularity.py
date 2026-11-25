"""Popularity is canonical TMDB metadata, not a watched state or a rating."""

from uuid import uuid4

from home_media_catalog.connectors.base import Record


def test_catalogue_popularity_sort_is_numeric_and_missing_last(store):
    source = store.save_source({"name": "Kodi", "type": "kodi"})["id"]
    store.apply_snapshot(source, [
        Record("popular", "movie", "Popular", identifiers={"tmdb": "91101"}),
        Record("less", "movie", "Less popular", identifiers={"tmdb": "91102"}),
        Record("unranked", "movie", "Unranked", identifiers={"tmdb": "91103"}),
    ])
    rows = {row["title"]: row["id"] for row in store.media(limit=10)["items"]}
    store.save_enrichment(rows["Popular"], {"tmdb_popularity": 234.5})
    store.save_enrichment(rows["Less popular"], {"tmdb_popularity": 13.2})

    ascending = store.media(sort="popularity", order="asc", limit=10)["items"]
    descending = store.media(sort="popularity", order="desc", limit=10)["items"]
    assert [item["title"] for item in ascending] == [
        "Less popular", "Popular", "Unranked",
    ]
    assert [item["title"] for item in descending] == [
        "Popular", "Less popular", "Unranked",
    ]
    assert ascending[0]["popularity"] == 13.2
    assert descending[0]["popularity"] == 234.5
    assert descending[-1]["popularity"] is None


def test_streaming_work_can_carry_popularity_without_rating(store):
    created = store.save_internal_media({
        "id": str(uuid4()), "title": "Trending series", "kind": "tvshow",
        "tmdb_id": "92201", "format": "", "streaming_reference": True,
        "popularity": 102.25,
    })
    item = store.media(sort="popularity")["items"][0]
    assert item["id"] == created["id"]
    assert item["popularity"] == 102.25
    assert item["rating"] is None


def test_existing_rating_refresh_updates_popularity_without_new_lookup(store):
    source = store.save_source({"name": "Kodi", "type": "kodi"})["id"]
    store.apply_snapshot(source, [
        Record("rated", "movie", "Rated", identifiers={"tmdb": "93301"}),
    ])
    work_id = store.media()["items"][0]["id"]
    assert store.save_tmdb_rating(work_id, 8.2, popularity=73.4) == "accepted"
    assert store.media()["items"][0]["popularity"] == 73.4
    cursor = store.rating_changes()
    assert cursor["changes"][0]["popularity"] == 73.4
    # The unchanged public vote can still carry a later popularity change.
    assert store.save_tmdb_rating(work_id, 8.2, popularity=97.6) == "accepted"
    assert store.media()["items"][0]["popularity"] == 97.6
