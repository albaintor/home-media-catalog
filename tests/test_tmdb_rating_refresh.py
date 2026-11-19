"""TMDB public rating: gap filling, explicit refresh and local-first merge."""

import json
from datetime import UTC, datetime, timedelta

from home_media_catalog.connectors.base import Record
from home_media_catalog.enrichment import tmdb_rating


def _movie(store, rating=None):
    source = store.save_source({"name": "CLZ", "type": "clz"})["id"]
    store.apply_snapshot(source, [Record(
        "disc", "movie", "Rated film", rating=rating,
        identifiers={"imdb": "tt1234567"},
    )])
    return store.media(source_type="clz")["items"][0]["id"]


def _enrichment(**changes):
    return {"tmdb_id": "4400", "people": [], "trailers": [], **changes}


def _device_item(media_id, **changes):
    return {
        "media_id": media_id, "imdb_id": "tt1234567", "tmdb_id": "4400",
        "people": [], "trailers": [], **changes,
    }


def _iso(delta=timedelta()):
    return (datetime.now(UTC) + delta).isoformat(timespec="seconds")


def test_tmdb_vote_average_is_normalized():
    assert tmdb_rating(7.345) == 7.3
    assert tmdb_rating(0) is None
    assert tmdb_rating(11) is None
    assert tmdb_rating(True) is None
    assert tmdb_rating("8") is None


def test_automatic_enrichment_only_fills_a_missing_rating(store):
    rated = _movie(store, rating=6.0)
    store.save_enrichment(rated, _enrichment(tmdb_rating=8.1))
    assert store.detail(rated)["rating"] == 6.0

    store.save_enrichment(rated, _enrichment(tmdb_rating=8.1, tmdb_rating_refreshed_at=_iso()))
    assert store.detail(rated)["rating"] == 8.1


def test_missing_source_rating_uses_tmdb(store):
    movie = _movie(store)
    store.save_enrichment(movie, _enrichment(tmdb_rating=7.4))
    assert store.detail(movie)["rating"] == 7.4


def test_refresh_without_vote_keeps_last_refreshed_rating(store):
    movie = _movie(store, rating=5.0)
    store.save_enrichment(movie, _enrichment(tmdb_rating=7.9, tmdb_rating_refreshed_at=_iso()))
    store.save_enrichment(movie, _enrichment())
    assert store.detail(movie)["rating"] == 7.9


def test_device_refresh_is_merged_when_newer(store):
    movie = _movie(store, rating=5.0)
    store.save_enrichment(movie, _enrichment(
        tmdb_rating=6.5, tmdb_rating_refreshed_at=_iso(-timedelta(days=2))))
    result = store.merge_device_tmdb_enrichment([_device_item(
        movie, tmdb_rating=7.2, tmdb_rating_refreshed_at=_iso(-timedelta(days=1)))])
    assert result["accepted"] == [movie]
    assert store.detail(movie)["rating"] == 7.2

    # An older device refresh must not roll back the newer value.
    result = store.merge_device_tmdb_enrichment([_device_item(
        movie, tmdb_rating=4.0, tmdb_rating_refreshed_at=_iso(-timedelta(days=3)))])
    assert result["unchanged"] == [movie]
    assert store.detail(movie)["rating"] == 7.2


def test_device_gap_fill_without_refresh_keeps_source_rating(store):
    movie = _movie(store, rating=5.0)
    store.merge_device_tmdb_enrichment([_device_item(movie, tmdb_rating=8.0)])
    assert store.detail(movie)["rating"] == 5.0


def test_future_device_timestamp_is_clamped(store):
    movie = _movie(store, rating=5.0)
    store.merge_device_tmdb_enrichment([_device_item(
        movie, tmdb_rating=7.0, tmdb_rating_refreshed_at=_iso(timedelta(days=365)))])
    assert store.detail(movie)["rating"] == 7.0
    with store.connect() as conn:
        payload = json.loads(conn.execute(
            "SELECT payload FROM enrichment WHERE work_id=?", (movie,)
        ).fetchone()["payload"])
    stored = datetime.fromisoformat(payload["tmdb_rating_refreshed_at"])
    assert stored <= datetime.now(UTC)


def test_invalid_device_rating_is_ignored(store):
    movie = _movie(store, rating=5.0)
    store.merge_device_tmdb_enrichment([_device_item(
        movie, tmdb_rating=42, tmdb_rating_refreshed_at=_iso())])
    assert store.detail(movie)["rating"] == 5.0
