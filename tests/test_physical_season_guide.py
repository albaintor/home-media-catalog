"""CLZ season discs retain individual artwork and expose read-only episode metadata."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import patch

from home_media_catalog.connectors.base import Record
from home_media_catalog.enrichment import enrich_tmdb_season_guide


def test_physical_seasons_keep_three_covers_and_cached_episode_guides(client, admin):
    store = client.app.state.store
    source = store.save_source({"name": "CLZ", "type": "clz"})
    records = [
        Record(
            external_key=f"disc:{number}",
            kind="tvshow",
            title="The Mandalorian",
            identifiers={"imdb": "tt8111088"},
            poster=[f"https://clzmovies.r.sizr.io/core/covers/lg/a{number}/s{number}.jpg"],
            metadata={
                "physical_season": number,
                "physical_title": f"The Mandalorian: Season {number}",
                "physical_episode_count": 8,
            },
        )
        for number in (1, 2, 3)
    ]
    store.apply_snapshot(source["id"], records)
    shows = store.media(kind="tvshow")["items"]
    assert len(shows) == 1
    series_id = shows[0]["id"]

    response = client.get(f"/api/v1/media/{series_id}/seasons", headers=admin)
    assert response.status_code == 200, response.text
    seasons = response.json()
    assert [s["season"] for s in seasons] == [1, 2, 3]
    assert [s["physical_episode_count"] for s in seasons] == [8, 8, 8]
    assert [s["episode_count"] for s in seasons] == [0, 0, 0]
    assert len({s["poster"] for s in seasons}) == 3
    for number in (1, 2, 3):
        assert store.season_poster(series_id, number) == [
            f"https://clzmovies.r.sizr.io/core/covers/lg/a{number}/s{number}.jpg"
        ]

    sample = [{
        "episode": 1,
        "title": "Chapter 1: The Mandalorian",
        "plot": "A bounty hunter.",
        "runtime_minutes": 39,
        "still": "https://image.tmdb.org/t/p/w300/chapter1.jpg",
    }]
    assert store.save_season_guide(series_id, 1, sample)
    assert store.cached_season_guide(series_id, 1) == sample
    guide = client.get(f"/api/v1/media/{series_id}/seasons/1/guide", headers=admin)
    assert guide.status_code == 200, guide.text
    assert guide.json() == sample
    assert client.get(f"/api/v1/media/{series_id}/seasons/1/guide").status_code == 401

    # A metadata refresh must preserve the separately fetched guide.
    store.save_enrichment(series_id, {"people": [], "trailers": []})
    assert store.cached_season_guide(series_id, 1) == sample
    # A reference episode is never a Kodi item or a watchable catalogue work.
    digital = client.get(f"/api/v1/media/{series_id}/seasons/1/episodes", headers=admin)
    assert digital.status_code == 200
    assert digital.json()["total"] == 0


def test_tmdb_episode_guide_uses_known_imdb_identity_and_validates_stills():
    @asynccontextmanager
    async def fake_connection(_token):
        async def get(path, **parameters):
            if path == "find/tt8111088":
                assert parameters["external_source"] == "imdb_id"
                return {"tv_results": [{"id": 82856}]}
            assert path == "tv/82856/season/2"
            assert parameters["language"] == "fr-FR"
            return {
                "episodes": [
                    {"episode_number": 2, "name": "Épisode 2",
                     "overview": "Synopsis", "runtime": 40,
                     "still_path": "javascript:invalid"},
                    {"episode_number": 1, "name": "Épisode 1",
                     "overview": "Synopsis 1", "runtime": 37,
                     "still_path": "/chapter.jpg"},
                ]
            }
        yield get

    with patch("home_media_catalog.enrichment.tmdb_connection", fake_connection):
        items = asyncio.run(enrich_tmdb_season_guide(
            {"kind": "tvshow", "identifiers": {"imdb": ["tt8111088"]}}, 2, "test"
        ))
    assert [item["episode"] for item in items] == [1, 2]
    assert items[0]["still"] == "https://image.tmdb.org/t/p/w300/chapter.jpg"
    assert items[1]["still"] is None
