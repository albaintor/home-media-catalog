"""Web/iOS Streaming page parity and local-first state coverage."""
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "home_media_catalog" / "static"


def test_streaming_route_between_video_and_music():
    html = (STATIC / "index.html").read_text()
    js = (STATIC / "app.js").read_text()
    music = (STATIC / "music.js").read_text()
    assert html.index('id="video-link"') < html.index('id="streaming-link"') < html.index('id="music-link"')
    assert 'id="streaming-page"' in html
    assert "startStreaming();" in js
    assert "window.location.hash === '#streaming'" in music
    assert "streaming-open-media" in js


def test_streaming_web_has_both_saved_horizontal_shelves_and_full_subviews():
    js = (STATIC / "streaming.js").read_text()
    html = (STATIC / "index.html").read_text()
    css = (STATIC / "streaming.css").read_text()
    assert "api('streaming/state')" in js
    assert "item:(movie|tv)" in js
    assert "row.deleted" in js
    assert "'My Movies'" in js and "'My Series'" in js
    assert "function renderSaved()" in js
    assert "if(!visible.length)return ''" in js
    assert "streaming-shelf-track" in js and "overflow-x:auto" in css
    assert 'id="streaming-subview"' in html
    assert "See more titles" in js
    assert "function showSubview(" in js


def test_web_uses_top_discovery_search_and_preferences_from_ios():
    js = (STATIC / "streaming.js").read_text()
    assert "streaming/top" in js
    assert "streaming/discover" in js
    assert "streaming/search?" in js
    assert "show_tops" in js and "show_recent" in js
    assert "preference('genres'" in js and "preference('kinds'" in js
    assert "preference('subscriptions'" in js
    assert "preference('visible_services'" in js
    assert "IntersectionObserver" in js
    assert "streaming/state/sync" in js


def test_web_format_uhd_is_not_4k_resolution():
    app = (STATIC / "app.js").read_text()
    assert "4k ultra hd blu-ray" in app
    css = (STATIC / "app.css").read_text()
    assert ".format-720p" in css and ".format-1080p" in css



def test_streaming_web_fetches_complete_metadata_and_provider_logos():
    js = (STATIC / "streaming.js").read_text()
    assert "streaming/providers?" in js
    assert "providerLogo(s)" in js
    assert "streaming/titles/" in js and "/details?" in js
    assert "streaming-cast-track" in js
    assert "streaming-detail-genres" in js
    assert "streaming-seasons" in js
    assert "state.detailCache.set(key,full)" in js
    css = (STATIC / "streaming.css").read_text()
    assert ".streaming-logo img" in css
    assert ".streaming-person img" in css


def test_web_streaming_new_svod_services_and_branded_genres():
    js = (STATIC / "streaming.js").read_text()
    css = (STATIC / "streaming.css").read_text()
    for key, name in (
        ("crunchyroll", "Crunchyroll"),
        ("rakuten_viki", "Rakuten Viki"),
        ("mubi", "MUBI"),
    ):
        assert f"{key}:'{name}'" in js
        assert f".streaming-logo-fallback.{key}" in css
    for genre in ("action", "adventure", "science_fiction"):
        assert f"{genre}:" in js
    assert "streaming-kind-movie" in css
    assert "streaming-kind-tv" in css
    assert "categoryIcons(kind,genreKey,sort)" in js
    assert "genreIcons(option)" in js


def test_web_streaming_popularity_and_local_rating_filters():
    js = (STATIC / "streaming.js").read_text()
    html = (STATIC / "index.html").read_text()
    for identifier in (
        "streaming-show-popularity",
        "streaming-show-personal-rating",
        "streaming-personal-rating-filter",
        "streaming-subview-sort",
        "streaming-subview-order",
        "streaming-subview-trigger",
    ):
        assert f'id="{identifier}"' in html
    assert "popularityIndex(title)" in js
    assert "Math.round(popularity)" in js
    assert "personalRatingFor(title)" in js
    assert "personal_rating_filter" in js
    assert "sortedSubviewItems(def)" in js
    assert "renderSubviewGrid()" in js
    assert "connectStreamingSubviewObserver()" in js


def test_web_streaming_metadata_cache_is_incremental_and_bounded():
    js = (STATIC / "streaming.js").read_text()
    assert "indexedDB.open('hmc-streaming-public-cache',1)" in js
    assert "STREAMING_WEB_CACHE_LIMIT=50*1024*1024" in js
    assert "STREAMING_WEB_CACHE_TARGET=46*1024*1024" in js
    assert "publicCachedRequest(key,15*60*1000" in js
    assert "publicCachedRequest('details:'+key+':'+language" in js
    assert "stale" not in js or "if(cached)return cached.value" in js
    assert "function refreshFilteredViews()" in js
    assert "refreshFilteredViews();" in js
    # Personal state and authentication responses must never be retained in IDB.
    assert "publicCachedRequest('streaming/state" not in js
    assert "publicCachedRequest('auth/session" not in js
