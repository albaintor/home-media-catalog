"""Regression contracts for cross-platform streaming features."""
import asyncio
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from home_media_catalog import streaming
from home_media_catalog.streaming import build_streaming_router

WEB = Path(__file__).resolve().parents[1] / "home_media_catalog" / "static"


def test_streaming_web_exposes_native_watch_and_rating_filters():
    html = (WEB / "index.html").read_text()
    script = (WEB / "streaming.js").read_text()
    for identifier in (
        "streaming-watch-filter",
        "streaming-rating-min",
        "streaming-rating-max",
        "streaming-user-rating-min",
        "streaming-user-rating-max",
        "streaming-genre-options",
    ):
        assert f'id="{identifier}"' in html
    for key in (
        "watch_filter",
        "rating_min",
        "rating_max",
        "user_rating_min",
        "user_rating_max",
        "visible_services",
    ):
        assert f"'{key}'" in script
    assert "streaming/catalogue-metrics" in script
    assert "matchesStreamingFilters" in script
    assert "persistPreference(" in script
    # A heart toggle no longer tombstones a streaming title used by a list.
    assert "liked:liked?'true':'false'" in script
    assert "deleted:false" in script
    # One ordered category preference is shared with native iOS.
    assert "category_order" in script
    assert "special_category_" in script
    assert "data-streaming-move" in script


def test_streaming_web_manages_subscriptions_lists_and_collections():
    html = (WEB / "index.html").read_text()
    script = (WEB / "streaming.js").read_text()
    assert 'id="streaming-subscription-options"' in html
    assert "data-streaming-subscription" in script
    assert "persistPreference('subscriptions'" in script
    assert "renderListCollectionActions()" in script
    assert "ensureWorkID(title)" in script
    assert "data-streaming-assign" in script
    assert "data-streaming-new" in script
    assert "collections/'+encodeURIComponent(targetID)+'/items" in script
    assert "lists/'+encodeURIComponent(targetID)+'/items" in script
    assert "response.status===204?null:response.json()" in script


def test_streaming_cast_has_two_source_person_filmography():
    html = (WEB / "index.html").read_text()
    script = (WEB / "streaming.js").read_text()
    assert 'id="streaming-person-dialog"' in html
    assert "data-streaming-person=" in script
    assert "streaming/people/" in script
    assert "person_tmdb_id:" in script
    assert "data-streaming-person-tab" in script
    assert "data-streaming-person-more" in script
    assert "streaming/availability/" in script
    assert "const selection=Object.keys(names).join(',')" in script
    assert "personAvailabilityCache" in script
    assert 'value="auto"' in html
    assert "new Intl.Locale" in script


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js unavailable")
def test_web_rating_and_watch_predicates_match_ios():
    script = r"""
const assert=require('node:assert/strict'),fs=require('node:fs');
const src=fs.readFileSync(process.argv[1],'utf8');
const predicate=src.slice(src.indexOf('function scoreLimit('),
                          src.indexOf('function personalRows('));
const prefs=new Map();
const state={metrics:new Map()};
const matches=Function('state','preference','keyFor','api','failure','$',
  predicate+'\nreturn matchesStreamingFilters;')(
  state,(key,fallback)=>prefs.get(key)??fallback,
  title=>title.kind+':'+title.id,()=>{},()=>{},()=>{}
);
const movie={id:7,kind:'movie',rating:8.5};
assert.equal(matches(movie),true);
prefs.set('watch_filter','unwatched');
assert.equal(matches(movie),true);
prefs.set('watch_filter','watched');
assert.equal(matches(movie),false);
state.metrics.set('movie:7',{watch_status:'watched',user_rating:9.0,rating:8.5});
assert.equal(matches(movie),true);
prefs.set('user_rating_min','9');
assert.equal(matches(movie),true);
prefs.set('user_rating_max','8.5');
assert.equal(matches(movie),false);
prefs.set('user_rating_max','10');
prefs.set('rating_min','9');
assert.equal(matches(movie),false);
prefs.set('rating_min','8.5');
assert.equal(matches(movie),true);
"""
    result = subprocess.run(
        ["node", "-e", script, str(WEB / "streaming.js")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_tmdb_person_endpoint_uses_id_unfiltered_filmography(monkeypatch):
    async def fake_tmdb(_token, path, params):
        assert path == "/person/42"
        assert params["append_to_response"] == "combined_credits"
        return {
            "id": 42,
            "name": "Test Person",
            "biography": "Biography",
            "profile_path": "/test.jpg",
            "combined_credits": {
                "cast": [
                    {"id": 100, "media_type": "movie", "title": "Example Movie",
                     "release_date": "2024-01-01", "vote_average": 8.0},
                    {"id": 100, "media_type": "movie", "title": "Example Movie",
                     "release_date": "2024-01-01", "character": "Role"},
                    {"id": 101, "media_type": "tv", "name": "Example Series",
                     "first_air_date": "2025-01-01"},
                    {"id": 999, "media_type": "person", "name": "Other"},
                ],
                "crew": [],
            },
        }

    monkeypatch.setattr(streaming, "_tmdb", fake_tmdb)
    streaming._CACHE.clear()
    router = build_streaming_router(object(), SimpleNamespace(tmdb_api_token="test"))
    endpoint = next(
        route.endpoint for route in router.routes
        if route.path == "/api/v1/streaming/people/{person_id}"
    )
    response = asyncio.run(endpoint(42, language="fr-FR"))
    assert response["name"] == "Test Person"
    assert {(row["kind"], row["id"]) for row in response["filmography"]} == {
        ("movie", 100),
        ("tv", 101),
    }
    assert response["filmography"][0]["id"] == 101
    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoint(0, language="fr-FR"))
    assert exc.value.status_code == 422
    streaming._CACHE.clear()
