"""Regression tests for reusable web video/audio format logos."""

from pathlib import Path


def test_format_badges_use_eager_static_pngs():
    script = (Path(__file__).resolve().parents[1] / "home_media_catalog" / "static" / "app.js").read_text()
    badge_renderer = script.split("function formatBadges(", 1)[1].split("function detailFormatBadges(", 1)[0]
    assert 'loading="eager"' in badge_renderer
    assert 'loading="lazy"' not in badge_renderer
    assert 'decoding="async"' in badge_renderer


def test_static_format_logos_are_browser_cacheable(client):
    for asset in (
        "/static/format-icons/format-4k.png",
        "/static/format-icons/format-blu-ray.png",
        "/static/format-logos.svg",
    ):
        response = client.get(asset)
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "public, max-age=604800"
        etag = response.headers["ETag"]
        revalidated = client.get(asset, headers={"If-None-Match": etag})
        assert revalidated.status_code == 304
        assert revalidated.headers["Cache-Control"] == "public, max-age=604800"

    # Application code, the HTML shell and private API responses stay uncached.
    assert client.get("/static/app.js").headers["Cache-Control"] == "private, no-store"
    assert client.get("/").headers["Cache-Control"] == "private, no-store"
    assert client.get("/api/v1/media").headers["Cache-Control"] == "private, no-store"
