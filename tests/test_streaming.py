"""Streaming state and TMDB discovery invariants (no external HTTP required)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from home_media_catalog import streaming
from home_media_catalog.compatibility import MAX_MOBILE_CONTRACT
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


def test_streaming_episodic_tracking_requires_contract_nine():
    assert MAX_MOBILE_CONTRACT == 9


def test_streaming_reference_episodes_have_watch_tracking_without_files(tmp_path):
    store = Store(tmp_path / "episode-tracking.sqlite")
    store.initialize()
    created = store.save_internal_media(InternalMediaInput(
        id="c100aaaa-1234-4234-9234-220022002200",
        kind="tvshow", title="Series for Tracking",
        tmdb_id="88888", format="",
        poster_path="/series.jpg",
        streaming_services=["prime_video"],
    ).model_dump())
    series_id = created["id"]
    episode_info = [
        {"season": 1, "episode": 1, "tmdb_id": 88001,
         "title": "First episode", "overview": "First", "air_date": "2025-01-01"},
        {"season": 1, "episode": 2, "tmdb_id": 88002,
         "title": "Second episode", "overview": "Second", "air_date": "2025-01-08"},
    ]
    assert store.import_streaming_episodes(series_id, 88888, episode_info)["added"] == 2
    assert store.import_streaming_episodes(series_id, 88888, episode_info)["added"] == 0
    assert store.seasons(series_id)[0]["episode_count"] == 2
    assert store.detail(series_id)["streaming_services"] == ["prime_video"]
    result = store.set_episode_watch_status(series_id, True, season=1, up_to_episode=1)
    assert result["updated"] == 1
    seasons = store.seasons(series_id)
    assert seasons[0]["watched_count"] == 1
    children = store.media(kind="episode", parent_id=series_id, limit=10)["items"]
    assert len(children) == 2
    assert any(child["watch_status"] == "watched" for child in children)
    assert all(child["source_types"] == ["internal"] for child in children)


def test_legacy_streaming_state_recovers_service_after_catalogue_merge(tmp_path):
    store = Store(tmp_path / "streaming-migration.sqlite")
    store.initialize()
    created = store.save_internal_media(InternalMediaInput(
        id="c200aaaa-1234-4234-9234-220022002200",
        kind="tvshow", title="Older Streamed Series",
        tmdb_id="99999", format="",
    ).model_dump())
    work_id = created["id"]
    _merge(store, [
        StreamingStateRow(
            key="item:tv:99999", payload={"tmdb_id": "99999", "kind": "tv",
                                          "services": "netflix", "status": "saved"},
            modified_at=datetime(2026, 11, 21, tzinfo=UTC).isoformat(),
        )
    ])
    assert store.detail(work_id)["streaming_services"] == ["netflix"]


def test_official_provider_links_validate_identity_and_do_not_accept_search():
    from home_media_catalog.streaming import _official_link
    url, provider_id = _official_link(
        "https://www.netflix.com/title/81234567", "netflix")
    assert provider_id == "81234567"
    assert url == "https://www.netflix.com/title/81234567"
    url, provider_id = _official_link(
        "https://tv.apple.com/fr/show/example/umc.cmc.example", "apple_tv_plus")
    assert provider_id == "umc.cmc.example"
    with pytest.raises(HTTPException):
        _official_link("https://www.netflix.com/search?q=example", "netflix")
    with pytest.raises(HTTPException):
        _official_link("https://netflix.com.evil.example/title/81234567", "netflix")
    with pytest.raises(HTTPException):
        _official_link("http://www.netflix.com/title/81234567", "netflix")
    with pytest.raises(HTTPException):
        _official_link("https://www.netflix.com/title/not-an-id", "netflix")


def test_streaming_provider_link_sync_reconciliation(tmp_path):
    store = Store(tmp_path / "streaming-links.sqlite")
    store.initialize()
    base = datetime(2026, 11, 21, tzinfo=UTC)
    def link(timestamp, url):
        return StreamingStateRow(
            key="link:tv:1399:netflix:s1e2",
            payload={"url": url, "provider_id": "81234567",
                     "source": "official_share_link"},
            modified_at=timestamp.isoformat())
    first = _merge(store, [link(base, "https://www.netflix.com/title/81234567")])
    assert first[0]["payload"]["provider_id"] == "81234567"
    refreshed = _merge(store, [link(base + timedelta(seconds=5),
                                    "https://www.netflix.com/title/87654321")])
    assert refreshed[0]["payload"]["url"].endswith("87654321")
    unchanged = _merge(store, [link(base, "https://www.netflix.com/title/81234567")])
    assert unchanged == refreshed


def test_streaming_sync_rows_have_required_mobile_wire_fields(tmp_path):
    """iOS Decodable expects modified_at, not modifiedAt or a missing timestamp."""
    store = Store(tmp_path / "streaming-contract.sqlite")
    store.initialize()
    row = StreamingStateRow(
        key="prefs:subscriptions",
        payload={"value": "prime_video"},
        modified_at=datetime(2026, 11, 21, tzinfo=UTC).isoformat(),
    )
    response_rows = _merge(store, [row])
    assert len(response_rows) == 1
    assert set(response_rows[0]) == {"key", "payload", "modified_at", "deleted"}
    assert isinstance(response_rows[0]["modified_at"], str)
    assert response_rows[0]["modified_at"].endswith("+00:00")
    # GET /state and POST /state/sync both use _merge, keeping one wire
    # contract; no speculative timestamp should be fabricated by the iOS app.
    assert _merge(store, [])[0] == response_rows[0]


def test_prime_only_accepts_primevideo_host_not_american_retail_asin():
    from home_media_catalog.streaming import _official_link
    url, provider_id = _official_link(
        "https://www.primevideo.com/detail/0TUVXIO58IUNEPNBF8363Z7YGL",
        "prime_video",
    )
    assert provider_id == "0TUVXIO58IUNEPNBF8363Z7YGL"
    assert url.startswith("https://www.primevideo.com/detail/")
    with pytest.raises(HTTPException):
        _official_link("https://www.amazon.fr/gp/video/detail/B09QH98YG1", "prime_video")
    with pytest.raises(HTTPException):
        _official_link("https://www.amazon.com/gp/video/detail/B09QH98YG1", "prime_video")
