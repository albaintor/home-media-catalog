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
    _streaming_trailers,
    _title_metadata,
)


def _row(key: str, stamp: datetime, value: str, deleted: bool = False):
    return StreamingStateRow(
        key=key,
        payload={"value": value},
        modified_at=stamp.isoformat(),
        deleted=deleted,
    )


def test_independent_streaming_personal_watched_and_rating_sync_without_heart(tmp_path):
    """Streaming personal state must never require or create a catalogue work."""
    store = Store(tmp_path / "personal.sqlite")
    store.initialize()
    now = datetime(2026, 11, 21, tzinfo=UTC)
    original = StreamingStateRow(
        key="personal:movie:12345",
        payload={"watched": "true", "rating": "8.5"},
        modified_at=now.isoformat(), deleted=False,
    )
    received = _merge(store, [original])
    personal = next(r for r in received if r["key"] == original.key)
    assert personal["payload"] == {"watched": "true", "rating": "8.5"}
    assert store.media(kind="movie")["total"] == 0

    # Device B modifies only the watched status, retaining the rating.
    revision = StreamingStateRow(
        key=original.key,
        payload={"watched": "false", "rating": "8.5"},
        modified_at=(now + timedelta(seconds=1)).isoformat(),
        deleted=False,
    )
    changed = _merge(store, [revision])
    assert next(r for r in changed if r["key"] == original.key)["payload"] == {
        "watched": "false", "rating": "8.5",
    }
    assert _merge(store, [original]) == changed  # stale device cannot overwrite B
    assert store.media(kind="movie")["total"] == 0

    # Clearing the rating is an explicit override and stays synchronized.
    cleared = StreamingStateRow(
        key=original.key,
        payload={"watched": "false", "rating_cleared": "true"},
        modified_at=(now + timedelta(seconds=2)).isoformat(),
        deleted=False,
    )
    result = _merge(store, [cleared])
    assert next(r for r in result if r["key"] == original.key)["payload"] == {
        "watched": "false", "rating_cleared": "true",
    }
    assert store.media(kind="movie")["total"] == 0


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


def test_category_and_region_preferences_merge_bidirectionally(tmp_path):
    store = Store(tmp_path / "catalogue.sqlite")
    store.initialize()
    start = datetime(2026, 11, 21, tzinfo=UTC)
    initial = _merge(store, [
        _row("prefs:genres", start, "action,drama,k_drama"),
        _row("prefs:catalogue_region", start, "FR"),
    ])
    by_key = {row["key"]: row["payload"]["value"] for row in initial}
    assert by_key == {
        "prefs:genres": "action,drama,k_drama",
        "prefs:catalogue_region": "FR",
    }

    # A second device can change either preference. Both devices receive
    # the same merged journal, with other keys left untouched.
    newer = _merge(store, [
        _row("prefs:genres", start + timedelta(seconds=2), "anime,crime"),
        _row("prefs:catalogue_region", start + timedelta(seconds=3), "automatic"),
    ])
    by_key = {row["key"]: row["payload"]["value"] for row in newer}
    assert by_key["prefs:genres"] == "anime,crime"
    assert by_key["prefs:catalogue_region"] == "automatic"

    # A stale device must not overwrite a newer change.
    stale = _merge(store, [
        _row("prefs:genres", start + timedelta(seconds=1), "drama"),
        _row("prefs:catalogue_region", start + timedelta(seconds=1), "US"),
    ])
    assert {row["key"]: row["payload"]["value"] for row in stale} == by_key

    # The selected list is permitted to be empty and remains a real value.
    cleared = _merge(store, [
        _row("prefs:genres", start + timedelta(seconds=4), ""),
    ])
    assert next(row for row in cleared if row["key"] == "prefs:genres")["payload"] == {
        "value": ""
    }


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
    assert _provider_ids("crunchyroll,rakuten_viki,mubi") == "283|344|11"
    with pytest.raises(HTTPException) as exc:
        _provider_ids("invalid-provider")
    assert exc.value.status_code == 422


def test_tmdb_popularity_is_distinct_from_public_rating():
    movie = _item({
        "id": 123, "title": "Film", "release_date": "2026-11-22",
        "popularity": 147.825, "vote_average": 6.4,
    }, "movie")
    assert movie["popularity"] == 147.825
    assert movie["rating"] == 6.4
    assert _item({"id": 124, "title": "Unknown"}, "movie")["popularity"] is None
    assert _item({"id": 125, "title": "Invalid", "popularity": float("nan")},
                 "movie")["popularity"] is None
    assert _item({"id": 126, "title": "Invalid", "popularity": -2},
                 "movie")["popularity"] is None


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



def test_streaming_tmdb_details_expose_only_valid_youtube_trailers():
    raw = {
        "id": 42, "title": "Demo", "release_date": "2026-11-21",
        "videos": {"results": [
            {"site": "Vimeo", "type": "Trailer", "key": "EXAMPLE1234"},
            {"site": "YouTube", "type": "Clip", "key": "EXAMPLE1234"},
            {"site": "YouTube", "type": "Trailer", "key": "../../invalid"},
            {"site": "YouTube", "type": "Trailer", "key": "EXAMPLE1234",
             "name": "Version française", "official": True, "iso_639_1": "fr"},
            {"site": "YouTube", "type": "Trailer", "key": "EXAMPLE1234"},
            {"site": "YouTube", "type": "Teaser", "key": "ANOTHER1234",
             "name": "Teaser", "official": False, "iso_639_1": "en"},
        ]},
    }
    trailers = _streaming_trailers(raw)
    assert [trailer["key"] for trailer in trailers] == [
        "EXAMPLE1234", "ANOTHER1234",
    ]
    assert trailers[0]["official"] is True
    assert _title_metadata(raw, "movie")["popularity"] is None
    assert _title_metadata(raw, "movie")["trailers"] == trailers
    assert _title_metadata({"id": 42, "title": "No video"}, "movie")["trailers"] == []


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


def test_new_svod_providers_filter_region_and_exclude_rentals():
    offers = {
        "results": {
            "FR": {
                "flatrate": [{"provider_id": 283}, {"provider_id": 344}],
                "rent": [{"provider_id": 11}],
                "buy": [{"provider_id": 11}],
            },
            "JP": {
                "flatrate": [{"provider_id": 11}],
            },
        }
    }
    selected = "crunchyroll,rakuten_viki,mubi"
    assert _availability_keys(offers, selected, "FR") == [
        "crunchyroll", "rakuten_viki"
    ]
    assert _availability_keys(offers, selected, "JP") == ["mubi"]
    assert _availability_keys(offers, selected, "US") == []


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


def test_category_search_preserves_genre_language_country_keyword_and_svod(monkeypatch):
    requested = []

    async def fake_tmdb(_token, path, _params):
        requested.append(path)
        if path == "/search/tv":
            return {"results": [
                {"id": 101, "name": "Matching K-drama", "genre_ids": [18],
                 "original_language": "ko", "origin_country": ["KR"]},
                {"id": 102, "name": "Wrong genre", "genre_ids": [35],
                 "original_language": "ko", "origin_country": ["KR"]},
                {"id": 103, "name": "Wrong language", "genre_ids": [18],
                 "original_language": "en", "origin_country": ["KR"]},
                {"id": 104, "name": "Wrong keyword", "genre_ids": [18],
                 "original_language": "ko", "origin_country": ["KR"]},
            ]}
        if path == "/tv/101/keywords":
            return {"results": [{"id": 701}]}
        if path == "/tv/104/keywords":
            return {"results": [{"id": 999}]}
        if path == "/tv/101/watch/providers":
            return {"results": {"FR": {"flatrate": [{"provider_id": 8}],
                                        "rent": [{"provider_id": 11}]}}}
        raise AssertionError(path)

    monkeypatch.setattr(streaming, "_tmdb", fake_tmdb)
    result = asyncio.run(streaming._search(
        "token", "netflix,mubi", "Drama", "tv", "FR",
        genre=18, origin_country="KR", original_language="ko",
        keyword_id=701,
    ))
    assert [title["id"] for title in result["items"]] == [101]
    assert result["items"][0]["service_keys"] == ["netflix"]
    assert "/tv/102/keywords" not in requested
    assert "/tv/103/keywords" not in requested
    assert "/tv/104/watch/providers" not in requested


def test_movie_category_search_uses_verified_production_country(monkeypatch):
    async def fake_tmdb(_token, path, _params):
        if path == "/search/movie":
            return {"results": [
                {"id": 201, "title": "French movie",
                 "genre_ids": [35], "original_language": "fr"},
                {"id": 202, "title": "Foreign movie",
                 "genre_ids": [35], "original_language": "fr"},
            ]}
        if path == "/movie/201":
            return {"production_countries": [{"iso_3166_1": "FR"}]}
        if path == "/movie/202":
            return {"production_countries": [{"iso_3166_1": "CA"}]}
        if path == "/movie/201/watch/providers":
            return {"results": {"FR": {"flatrate": [{"provider_id": 8}]}}}
        raise AssertionError(path)

    monkeypatch.setattr(streaming, "_tmdb", fake_tmdb)
    result = asyncio.run(streaming._search(
        "token", "netflix", "movie", "movie", "FR",
        genre=35, origin_country="FR", original_language="fr",
    ))
    assert [title["id"] for title in result["items"]] == [201]


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



def test_discover_applies_public_score_bounds_before_pagination(monkeypatch):
    seen = {}

    async def fake_tmdb(_token, path, params):
        seen.update(params)
        assert path == "/discover/tv"
        return {"page": 3, "total_pages": 12, "results": []}

    monkeypatch.setattr(streaming, "_tmdb", fake_tmdb)
    result = asyncio.run(streaming._discover(
        "token", "netflix", "tv", 18, 3, "FR",
        "popularity", "desc", "KR", "ko", 8.0, 9.5,
    ))
    assert result["page"] == 3
    assert result["total_pages"] == 12
    assert seen["vote_average.gte"] == 8.0
    assert seen["vote_average.lte"] == 9.5
    assert seen["vote_count.gte"] == 1
    assert seen["with_origin_country"] == "KR"
    assert seen["with_original_language"] == "ko"


def test_kdrama_and_korean_movie_categories_keep_subscription_and_region(monkeypatch):
    captured = []

    async def fake_tmdb(_token, path, params):
        captured.append((path, params))
        return {"page": 1, "total_pages": 1, "results": []}

    monkeypatch.setattr(streaming, "_tmdb", fake_tmdb)
    asyncio.run(streaming._discover(
        "token", "netflix,disney_plus", "tv", 18, 1, "FR",
        "popularity", "desc", "KR", "ko",
    ))
    asyncio.run(streaming._discover(
        "token", "netflix", "movie", None, 1, "FR",
        "popularity", "desc", "KR", "ko",
    ))
    for path, params in captured:
        assert path.startswith("/discover/")
        assert params["with_origin_country"] == "KR"
        assert params["with_original_language"] == "ko"
        assert params["watch_region"] == "FR"
        assert params["with_watch_monetization_types"] == "flatrate"
    assert captured[0][0] == "/discover/tv"
    assert captured[0][1]["with_genres"] == 18
    assert captured[0][1]["with_watch_providers"] == "8|337"
    assert captured[1][0] == "/discover/movie"
    assert "with_genres" not in captured[1][1]


@pytest.mark.parametrize(
    "kind,sort,order,expected",
    [
        ("movie", "popularity", "desc", "popularity.desc"),
        ("movie", "title", "asc", "title.asc"),
        ("tv", "title", "desc", "name.desc"),
        ("movie", "original_title", "asc", "original_title.asc"),
        ("tv", "original_title", "desc", "original_name.desc"),
        ("movie", "year", "asc", "primary_release_date.asc"),
        ("tv", "year", "desc", "first_air_date.desc"),
        ("movie", "rating", "asc", "vote_average.asc"),
        ("tv", "rating", "desc", "vote_average.desc"),
        ("tv", "recent", "asc", "first_air_date.desc"),
    ],
)
def test_streaming_discovery_sorting_precedes_pagination(
    monkeypatch, kind, sort, order, expected
):
    seen = {}

    async def fake_tmdb(_token, path, params):
        seen.update(params)
        assert path == "/discover/" + kind
        return {"page": 2, "total_pages": 6, "results": [
            {"id": 1, "title": "Example", "name": "Example", "vote_average": 7.5}
        ]}

    monkeypatch.setattr(streaming, "_tmdb", fake_tmdb)
    result = asyncio.run(streaming._discover(
        "token", "netflix", kind, None, 2, "FR", sort, order
    ))
    assert seen["page"] == 2
    assert seen["sort_by"] == expected
    assert result["page"] == 2
    assert result["total_pages"] == 6
    assert len(result["items"]) == 1
    assert result["ranking_type"] == "tmdb_" + sort
    if sort == "rating":
        assert seen["vote_count.gte"] == 100
    if sort in {"year", "recent"}:
        assert seen["first_air_date.lte" if kind == "tv" else "primary_release_date.lte"]

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
    assert store.media(kind="tvshow")["items"][0]["streaming_reference"] is False
    with store.connect() as conn:
        store._rebuild(conn, item_id)
    assert store.artwork(item_id, "poster")
    assert store.artwork(item_id, "fanart")


def test_streaming_episodic_tracking_requires_contract_nine():
    # Future additive contracts must retain the episode tracking guarantee.
    assert MAX_MOBILE_CONTRACT >= 9


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





def test_streaming_season_episodes_are_readable_without_catalogue_import(monkeypatch):
    """The discovery sheet must not require a Streaming heart or catalog work."""
    from types import SimpleNamespace

    queries = []

    async def fake_tmdb(_token, path, params):
        queries.append((path, params))
        return {"episodes": [
            {"id": 123456, "episode_number": 1,
             "name": "First episode", "overview": "Synopsis",
             "air_date": "2026-01-01", "still_path": "/still.jpg"},
            {"id": 123457, "episode_number": 2,
             "name": "", "overview": "", "air_date": None,
             "still_path": None},
        ]}

    monkeypatch.setattr(streaming, "_tmdb", fake_tmdb)
    router = streaming.build_streaming_router(
        None, SimpleNamespace(tmdb_api_token="test"))
    endpoint = next(route.endpoint for route in router.routes
                    if route.path.endswith(
                        "/titles/tv/{tmdb_id}/seasons/{season_number}/episodes"))
    result = asyncio.run(
        endpoint(tmdb_id=987654321, season_number=2, language="fr-FR"))
    assert queries == [
        ("/tv/987654321/season/2", {"language": "fr-FR"})
    ]
    assert len(result["episodes"]) == 2
    assert result["episodes"][0] == {
        "season": 2, "episode": 1, "tmdb_id": 123456,
        "title": "First episode", "overview": "Synopsis",
        "air_date": "2026-01-01", "still_path": "/still.jpg",
    }
    assert result["episodes"][1]["title"] == "Episode 2"

    # The second request comes from the metadata cache, without a second
    # TMDB lookup or a media-catalog write.
    again = asyncio.run(
        endpoint(tmdb_id=987654321, season_number=2, language="fr-FR"))
    assert again == result
    assert len(queries) == 1


def test_streaming_keyword_search_and_keyword_discover(monkeypatch):
    from types import SimpleNamespace

    observed = []

    async def fake_tmdb(_token, path, params):
        observed.append((path, params))
        if path == "/search/keyword":
            return {"results": [
                {"id": 12345, "name": "time travel"},
                {"id": 12345, "name": "duplicate"},
                {"id": -1, "name": "invalid"},
            ]}
        return {"page": 1, "total_pages": 3, "results": []}

    monkeypatch.setattr(streaming, "_tmdb", fake_tmdb)
    router = streaming.build_streaming_router(
        None, SimpleNamespace(tmdb_api_token="test"))
    search = next(route.endpoint for route in router.routes
                  if route.path.endswith("/categories/search"))
    result = asyncio.run(search(q="travel"))
    assert result == {"results": [{"id": 12345, "name": "time travel"}]}

    discovery = asyncio.run(streaming._discover(
        "test", "netflix", "tv", None, 1, "FR", "popularity",
        keyword_id=12345,
    ))
    assert discovery["total_pages"] == 3
    assert observed[-1][0] == "/discover/tv"
    assert observed[-1][1]["with_keywords"] == 12345
    assert observed[-1][1]["with_watch_monetization_types"] == "flatrate"

def test_streaming_heart_state_materializes_media_for_web_video_catalogue(tmp_path):
    """Local-first iOS state must also show up in server /media before journal upload."""
    from home_media_catalog.streaming import _reconcile_streaming_catalogue, _streaming_edition_uuid
    store = Store(tmp_path / "streaming-to-video.sqlite")
    store.initialize()
    movie = StreamingStateRow(
        key="item:movie:550", modified_at=datetime(2026, 11, 21, tzinfo=UTC).isoformat(),
        payload={"kind": "movie", "tmdb_id": "550", "title": "Sample streaming film",
                 "year": "1999", "poster_path": "/movie.jpg",
                 "services": "netflix,prime_video", "status": "saved"}
    )
    series = StreamingStateRow(
        key="item:tv:780", modified_at=datetime(2026, 11, 21, tzinfo=UTC).isoformat(),
        payload={"kind": "tv", "tmdb_id": "780", "title": "Sample streaming series",
                 "services": "disney_plus", "status": "pending"}
    )
    rows = _merge(store, [movie, series])
    assert _reconcile_streaming_catalogue(store, rows) == 2
    assert _reconcile_streaming_catalogue(store, _merge(store, [])) == 0
    listed = store.media(limit=20)["items"]
    titles = {item["title"] for item in listed}
    assert {"Sample streaming film", "Sample streaming series"} <= titles
    assert len(listed) == 2
    assert all(item["streaming_reference"] is True for item in listed)
    assert store.media(kind="movie")["total"] == 1
    assert store.media(kind="tvshow")["total"] == 1
    assert _streaming_edition_uuid("item:movie:550").count("-") == 4
    film = next(item for item in listed if item["kind"] == "movie")
    assert store.detail(film["id"])["streaming_services"] == ["netflix", "prime_video"]


def test_unheart_deletes_only_the_streaming_internal_edition(tmp_path):
    """A tombstoned heart removes the real edition, not just the sync row."""
    from home_media_catalog.streaming import _reconcile_streaming_catalogue, _streaming_edition_uuid

    store = Store(tmp_path / "unheart.sqlite")
    store.initialize()
    stamp = datetime(2026, 11, 22, tzinfo=UTC)
    saved = StreamingStateRow(
        key="item:movie:75501",
        modified_at=stamp.isoformat(),
        payload={"tmdb_id": "75501", "kind": "movie",
                 "title": "Heart-saved film", "services": "netflix",
                 "status": "saved"},
    )
    assert _reconcile_streaming_catalogue(store, _merge(store, [saved])) == 1
    media = store.media(kind="movie")
    assert media["total"] == 1
    assert media["items"][0]["streaming_reference"] is True

    # The same work can contain a physical edition. Removing the heart must
    # leave that separate edition and its media record intact.
    physical_id = "7501aaaa-1234-4234-9234-220022002200"
    store.save_internal_media(InternalMediaInput(
        id=physical_id, kind="movie", title="Heart-saved film",
        tmdb_id="75501", format="Blu-ray",
    ).model_dump())
    assert store.media(kind="movie")["total"] == 1

    removed = StreamingStateRow(
        key=saved.key,
        modified_at=(stamp + timedelta(seconds=1)).isoformat(),
        payload={}, deleted=True,
    )
    merged = _merge(store, [removed])
    assert _reconcile_streaming_catalogue(store, merged) == 1
    assert _reconcile_streaming_catalogue(store, _merge(store, [])) == 0
    remaining = store.media(kind="movie")
    assert remaining["total"] == 1
    assert remaining["items"][0]["streaming_reference"] is False
    with store.connect() as conn:
        edition_ids = {row["external_key"] for row in conn.execute(
            "SELECT external_key FROM records WHERE source_id='internal'"
        )}
    assert physical_id in edition_ids
    assert _streaming_edition_uuid(saved.key) not in edition_ids
    assert next(row for row in merged if row["key"] == saved.key)["deleted"]


def test_unheart_preserves_independent_personal_state(tmp_path):
    from home_media_catalog.streaming import _reconcile_streaming_catalogue

    store = Store(tmp_path / "unheart-personal.sqlite")
    store.initialize()
    stamp = datetime(2026, 11, 22, tzinfo=UTC)
    saved = StreamingStateRow(
        key="item:tv:75502", modified_at=stamp.isoformat(),
        payload={"tmdb_id": "75502", "kind": "tv", "title": "A saved show",
                 "services": "netflix", "status": "saved"},
    )
    personal = StreamingStateRow(
        key="personal:tv:75502", modified_at=stamp.isoformat(),
        payload={"watched": "true", "rating": "9.0"},
    )
    assert _reconcile_streaming_catalogue(store, _merge(store, [saved, personal])) == 1
    removed = StreamingStateRow(
        key=saved.key, modified_at=(stamp + timedelta(seconds=1)).isoformat(),
        payload={}, deleted=True,
    )
    final = _merge(store, [removed])
    assert _reconcile_streaming_catalogue(store, final) == 1
    assert store.media(kind="tvshow")["total"] == 0
    state = next(row for row in _merge(store, []) if row["key"] == personal.key)
    assert state["payload"] == {"watched": "true", "rating": "9.0"}
    assert state["deleted"] is False


def test_tombstoned_heart_does_not_recreate_deleted_streaming_edition(tmp_path):
    from home_media_catalog.streaming import _reconcile_streaming_catalogue
    store = Store(tmp_path / "streaming-deleted.sqlite")
    store.initialize()
    deleted = StreamingStateRow(
        key="item:movie:552", modified_at=datetime(2026, 11, 21, tzinfo=UTC).isoformat(),
        payload={"kind": "movie", "tmdb_id": "552", "title": "Deleted",
                 "services": "netflix", "status": "saved"},
        deleted=True,
    )
    assert _reconcile_streaming_catalogue(store, _merge(store, [deleted])) == 0
    assert store.media()["total"] == 0



def test_title_metadata_includes_summary_genres_cast_and_tv_seasons():
    from home_media_catalog.streaming import _title_metadata

    raw = {
        "id": 777, "name": "Sample TV", "original_name": "Original TV",
        "overview": "Detailed synopsis.", "first_air_date": "2025-05-18",
        "vote_average": 8.1, "poster_path": "/cover.jpg",
        "genres": [{"id": 18, "name": "Drama"}],
        "credits": {
            "cast": [{"id": 12, "name": "Actor", "character": "Hero",
                      "profile_path": "/actor.jpg"}],
            "crew": [{"id": 20, "name": "Director Name", "job": "Director",
                      "profile_path": "/director.jpg"}],
        },
        "seasons": [{"season_number": 1, "name": "Season 1",
                     "episode_count": 8, "poster_path": "/season.jpg"}],
    }
    result = _title_metadata(raw, "tv")
    assert result["title"] == "Sample TV"
    assert result["overview"] == "Detailed synopsis."
    assert result["genres"] == ["Drama"]
    assert result["cast"][0]["role"] == "Hero"
    assert result["cast"][0]["profile_path"] == "/actor.jpg"
    assert result["crew"][0]["role"] == "Director"
    assert result["seasons"][0]["episode_count"] == 8
    assert result["year"] == 2025
    assert _title_metadata({"id": 8, "title": "Film", "runtime": 95}, "movie")[
        "runtime_minutes"] == 95



def test_synopsis_from_streaming_journal_is_preserved_in_server_catalogue(tmp_path):
    from home_media_catalog.streaming import _reconcile_streaming_catalogue
    store = Store(tmp_path / "streaming-plot.sqlite")
    store.initialize()
    row = StreamingStateRow(
        key="item:movie:889", modified_at=datetime(2026, 11, 21, tzinfo=UTC).isoformat(),
        payload={
            "kind": "movie", "tmdb_id": "889", "title": "Example",
            "original_title": "Original Example", "overview": "Full plot summary",
            "rating": "8.4", "status": "saved", "services": "netflix",
        },
    )
    assert _reconcile_streaming_catalogue(store, _merge(store, [row])) == 1
    result = store.media(kind="movie")["items"][0]
    assert result["plot"] == "Full plot summary"
    assert result["original_title"] == "Original Example"
    assert result["rating"] == 8.4
