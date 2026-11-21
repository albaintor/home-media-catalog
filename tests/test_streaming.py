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
    assert store.media(kind="movie")["total"] == 1
    assert store.media(kind="tvshow")["total"] == 1
    assert _streaming_edition_uuid("item:movie:550").count("-") == 4
    film = next(item for item in listed if item["kind"] == "movie")
    assert store.detail(film["id"])["streaming_services"] == ["netflix", "prime_video"]


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
