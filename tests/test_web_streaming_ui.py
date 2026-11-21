"""Web streaming state must use the same NAS-synchronized records as iOS."""

from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "home_media_catalog" / "static"


def test_streaming_tab_is_routed_between_video_and_music():
    html = (STATIC / "index.html").read_text()
    js = (STATIC / "app.js").read_text()
    music = (STATIC / "music.js").read_text()
    assert html.index('id="video-link"') < html.index('id="streaming-link"') < html.index('id="music-link"')
    assert 'id="streaming-page"' in html
    assert "startStreaming();" in js
    assert "window.location.hash === '#streaming'" in music
    assert "streaming-open-media" in js


def test_streaming_web_reads_same_server_sync_state():
    js = (STATIC / "streaming.js").read_text()
    assert "api/v1/streaming/state" in js
    assert "item:(movie|tv)" in js
    assert "row.deleted" in js
    assert "p.work_id" in js
    assert "credentials:'same-origin'" in js
    assert "cache:'no-store'" in js


def test_uhd_disc_is_not_a_4k_video_duplicate():
    js = (STATIC / "app.js").read_text()
    category = js.split('function formatCategory(label)', 1)[1].split('function updateFormatSelections', 1)[0]
    assert "blu-ray" in category
    assert "4k ultra hd blu-ray" in js
    assert "?'Blu-ray 4K'" in js
    css = (STATIC / "app.css").read_text()
    assert ".format-720p" in css and ".format-1080p" in css
