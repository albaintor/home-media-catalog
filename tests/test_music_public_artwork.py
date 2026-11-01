"""MusicBrainz artwork must be exact-ID and restricted to trusted endpoints."""
import asyncio

from home_media_catalog.music_public_artwork import _trusted, exact_musicbrainz_cover


def test_public_artwork_rejects_untrusted_hosts_and_insecure_scheme():
    assert _trusted("https://coverartarchive.org/release-group/test/front-500")
    assert _trusted("https://ia800.us.archive.org/download/artwork/cover.jpg")
    for url in (
        "https://archive.org.evil.test/artwork",
        "http://coverartarchive.org/release-group/test/front-500",
        "https://127.0.0.1/latest",
        "https://localhost/internal",
        "https://user:secret@archive.org/image",
        "https://archive.org:8080/image",
        "file:///etc/passwd",
    ):
        assert not _trusted(url)


def test_musicbrainz_fallback_refuses_non_uuid_without_network():
    for value in (None, "", "not-a-uuid", "../secrets", "0000", "foo;DROP TABLE"):
        assert asyncio.run(exact_musicbrainz_cover(value)) is None
