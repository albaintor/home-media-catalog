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
    assert "if(!titles.length)return ''" in js
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
