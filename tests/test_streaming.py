"""Streaming state and TMDB discovery invariants (no external HTTP required)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from home_media_catalog import streaming
from home_media_catalog.models import InternalMediaInput
from home_media_catalog.store import Store
from home_media_catalog.streaming import (
    StreamingStateRow,
    _availability_keys,
    _item,
    _merge,
    _provider_ids,
    _provider_keys,
)


def _row(key: str, stamp: datetime, value: str, deleted: bool = False):
    return StreamingStateRow(
        key=key,
        payload={"value": value},
        modified_at=stamp.isoformat(),
        deleted=deleted,
    )


def test_streaming_rows_merge_per_key_and_keep_tombstones(tmp_path):
    store = Store(tmp_path / "catalogue.sqlite")
    store.initialize()
    start = datetime(2026, 11, 21, tzinfo=UTC)
    first = _merge(
        store,
        [_row("prefs:subscriptions", start, "netflix"),
         _row("prefs:genres", start, "drama")],
    )
    assert len(first) == 2

    changed = _merge(store, [_row("prefs:subscriptions", start + timedelta(seconds=1),
                                  "netflix,disney_plus")])
    assert next(r for r in changed if r["key"] == "prefs:genres")["payload"] == {
        "value": "drama"
    }
    old = _merge(store, [_row("prefs:subscriptions", start, "older")])
    assert next(r for r in old if r["key"] == "prefs:subscriptions")["payload"] == {
        "value": "netflix,disney_plus"
    }
    deleted = _merge(store, [_row("prefs:subscriptions", start + timedelta(seconds=2),
                                  "", deleted=True)])
    assert next(r for r in deleted if r["key"] == "prefs:subscriptions")["deleted"]
    assert len(_merge(store, [])) == 2


def test_same_timestamp_reconciliation_is_independent_of_input_order(tmp_path):
    store = Store(tmp_path / "catalogue.sqlite")
    store.initialize()
    stamp = datetime(2026, 11, 21, tzinfo=UTC)
    a = _row("prefs:kinds", stamp, "movie")
    b = _row("prefs:kinds", stamp, "tv")
    a_result = _merge(store, [a, b])
    b_result = _merge(store, [b, a])
    assert a_result == b_result


def test_providers_are_allowlisted():
    assert _provider_ids("netflix") == "8"
    assert "531" in _provider_ids("paramount_plus")
    with pytest.raises(HTTPException) as exc:
        _provider_ids("invalid-provider")
    assert exc.value.status_code == 422


def test_tmdb_media_mapping():
    value = _item({
        "id": 123,
        "name": "Sample",
        "first_air_date": "2026-11-20",
        "poster_path": "/sample.jpg",
        "vote_average": 7.5,
    }, "tv")
    assert value["id"] == 123
    assert value["title"] == "Sample"
    assert value["kind"] == "tv"
    assert value["year"] == 2026
    assert value["rating"] == 7.5
    assert value["poster_path"] == "/sample.jpg"


def test_multi_provider_selection_is_validated_and_deduplicated():
    assert _provider_keys("netflix,disney_plus,netflix") == ["netflix", "disney_plus"]
    assert _provider_ids("netflix,disney_plus") == "8|337"
    with pytest.raises(HTTPException):
        _provider_ids("netflix,unknown")


def test_only_flatrate_providers_qualify_in_france():
    offers = {
        "results": {
            "FR": {
                "flatrate": [{"provider_id": 8}],
                "rent": [{"provider_id": 337}],
            }
        }
    }
    assert _availability_keys(offers, "netflix,disney_plus", "FR") == ["netflix"]
    assert _availability_keys(offers, "netflix,disney_plus", "BE") == []


def test_search_is_consolidated_and_excludes_rentals(monkeypatch):
    async def fake_tmdb(_token, path, _params):
        if path == "/search/movie":
            return {"results": [{"id": 101, "title": "Example Movie",
                                  "release_date": "2025-11-21"}]}
        if path == "/search/tv":
            return {"results": [{"id": 202, "name": "Example Show",
                                  "first_air_date": "2025-11-21"}]}
        if path == "/movie/101/watch/providers":
            return {"results": {"FR": {"flatrate": [{"provider_id": 8}]}}}
        if path == "/tv/202/watch/providers":
            return {"results": {"FR": {"rent": [{"provider_id": 337}]}}}
        raise AssertionError(path)

    monkeypatch.setattr(streaming, "_tmdb", fake_tmdb)
    result = asyncio.run(streaming._search(
        "fake-token", "netflix,disney_plus", "Example", "both", "FR"
    ))
    assert len(result["items"]) == 1
    assert result["items"][0]["id"] == 101
    assert result["items"][0]["service_keys"] == ["netflix"]
    assert result["ranking_type"] == "search"


def test_discover_intersects_subscriptions_and_genre(monkeypatch):
    seen = {}

    async def fake_tmdb(_token, path, params):
        seen.update(params)
        assert path == "/discover/tv"
        return {"page": 1, "total_pages": 1, "results": []}

    monkeypatch.setattr(streaming, "_tmdb", fake_tmdb)
    result = asyncio.run(streaming._discover(
        "token", "netflix,disney_plus", "tv", 18, 1, "FR", "popularity"
    ))
    assert result["items"] == []
    assert seen["watch_region"] == "FR"
    assert seen["with_watch_providers"] == "8|337"
    assert seen["with_watch_monetization_types"] == "flatrate"
    assert seen["with_genres"] == 18


def test_streaming_series_poster_and_backdrop_survive_catalogue_rebuild(tmp_path):
    store = Store(tmp_path / "streaming-media.sqlite")
    store.initialize()
    payload = InternalMediaInput(
        id="2200aaaa-1234-4234-9234-220022002200",
        kind="tvshow",
        title="Example Series",
        tmdb_id="12345",
        poster_path="/series-poster.jpg",
        backdrop_path="/series-backdrop.jpg",
        format="",
    )
    created = store.save_internal_media(payload.model_dump())
    item_id = created["id"]
    assert store.artwork(item_id, "poster") == [
        "https://image.tmdb.org/t/p/w500/series-poster.jpg"
    ]
    assert store.artwork(item_id, "fanart") == [
        "https://image.tmdb.org/t/p/w1280/series-backdrop.jpg"
    ]
    with store.connect() as conn:
        store._rebuild(conn, item_id)
    assert store.artwork(item_id, "poster")
    assert store.artwork(item_id, "fanart")
