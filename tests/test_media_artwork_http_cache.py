"""Authenticated media images are cached without masking later updates."""

import hashlib


def test_media_artwork_private_etag_and_304(client, admin, monkeypatch):
    image = b"\xff\xd8\xff\xe0" + b"unchanged-artwork-bytes"

    async def image_from_server(_candidates):
        return image, "image/jpeg"

    monkeypatch.setattr(client.app.state.artwork, "get", image_from_server)
    endpoint = "/api/v1/media/test-media-id/artwork/poster"
    first = client.get(endpoint, headers=admin)
    assert first.status_code == 200
    assert first.content == image
    assert first.headers["Cache-Control"] == "private, max-age=86400, must-revalidate"
    expected_etag = '"' + hashlib.sha256(image).hexdigest() + '"'
    assert first.headers["ETag"] == expected_etag

    second = client.get(endpoint, headers={**admin, "If-None-Match": expected_etag})
    assert second.status_code == 304
    assert second.content == b""
    assert second.headers["ETag"] == expected_etag
    assert second.headers["Cache-Control"] == first.headers["Cache-Control"]

    replacement = b"\xff\xd8\xff\xe0" + b"modified-artwork-bytes"

    async def changed_image(_candidates):
        return replacement, "image/jpeg"

    monkeypatch.setattr(client.app.state.artwork, "get", changed_image)
    updated = client.get(endpoint, headers={**admin, "If-None-Match": expected_etag})
    assert updated.status_code == 200
    assert updated.content == replacement
    assert updated.headers["ETag"] != expected_etag


def test_web_cache_uses_per_media_revisions():
    from pathlib import Path

    script = (Path(__file__).resolve().parents[1] /
              "home_media_catalog" / "static" / "app.js").read_text()
    assert "artworkVersions.get(decodeURIComponent(match[1]))" in script
    assert "target.searchParams.set('artrev',String(revision))" in script
    assert "persistArtworkVersions();" in script
