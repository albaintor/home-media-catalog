"""Music cover extraction must respect registered NAS paths and stay bounded."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from home_media_catalog import music_embedded_artwork


def _request(mounted=None):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                artwork=SimpleNamespace(_mounted=lambda _path: mounted),
                store=object(),
                playback_oauth=object(),
                playback_nfs_mounts=object(),
            )
        )
    )


def test_embedded_cover_rejects_unregistered_and_external_urls(monkeypatch):
    monkeypatch.setattr(
        music_embedded_artwork, "_registered_media_location",
        lambda *_args: None,
    )
    request = _request()
    for candidate in (
        "http://example.com/file.mp3",
        "music@https://example.com/file.mp3",
        "music@smb://nas/share/song.mp3?external=1",
        "music@smb://user:pass@nas/share/song.mp3",
        "music@file:///etc/passwd",
        "music@smb://nas/share/song.mp3#fragment",
    ):
        assert music_embedded_artwork.extract_embedded_cover(request, candidate) is None


def test_embedded_cover_reads_only_configured_mount_and_checks_jpeg(monkeypatch):
    commands = []

    def run(command, **kwargs):
        commands.append((command, kwargs))
        return SimpleNamespace(
            returncode=0, stdout=bytes((0xFF, 0xD8, 0xFF)) + b"album-cover"
        )

    monkeypatch.setattr(music_embedded_artwork.subprocess, "run", run)
    request = _request(mounted=Path("/mnt/trusted/Musique/album/track.flac"))
    data = music_embedded_artwork.extract_embedded_cover(
        request, "music@smb://jedi/Musique/album/track.flac"
    )
    assert data == bytes((0xFF, 0xD8, 0xFF)) + b"album-cover"
    assert commands[0][0][commands[0][0].index("-i") + 1].startswith("/mnt/trusted/")
    assert commands[0][1]["timeout"] == 15

    monkeypatch.setattr(
        music_embedded_artwork.subprocess, "run",
        Mock(return_value=SimpleNamespace(returncode=0, stdout=b"not-a-jpeg")),
    )
    assert music_embedded_artwork.extract_embedded_cover(
        request, "music@smb://jedi/Musique/album/track.flac"
    ) is None


def test_embedded_cover_does_not_contact_network_without_saved_location(monkeypatch):
    monkeypatch.setattr(music_embedded_artwork, "_registered_media_location", lambda *_: None)

    def unexpected_network(*_args, **_kwargs):
        raise AssertionError("An unconfigured NAS must not be contacted")

    monkeypatch.setattr(music_embedded_artwork, "network_source", unexpected_network)
    assert music_embedded_artwork.extract_embedded_cover(
        _request(), "music@smb://jedi/Musique/track.m4a"
    ) is None
