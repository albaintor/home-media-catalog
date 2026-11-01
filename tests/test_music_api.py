"""Music REST API: sources, imports, browsing, export and streaming tickets."""

import time

from home_media_catalog.connectors.music_base import (
    MusicAlbumRecord,
    MusicSnapshot,
    MusicTrackRecord,
)
from home_media_catalog.music import MusicCatalogue

DISCOGS = (
    b"Catalog#,Artist,Title,Label,Format,Rating,Released,release_id,CollectionFolder,"
    b"Date Added,Collection Media Condition,Collection Sleeve Condition,Collection Notes\n"
    b'NODATA 02,Radiohead,OK Computer,Parlophone,"2xLP, Album, RE",5,1997,1234,'
    b"Uncategorized,2024-01-01,Mint (M),Near Mint (NM or M-),\n"
)


def _wait(client, admin, job_id):
    for _ in range(100):
        job = client.get(f"/api/v1/jobs/{job_id}", headers=admin).json()
        if job["status"] in ("completed", "failed"):
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_music_collection_import_and_browsing(client, admin):
    created = client.post(
        "/api/v1/sources",
        headers=admin,
        json={"name": "Mes vinyles", "type": "discogs", "config": {}},
    )
    assert created.status_code == 201
    source_id = created.json()["id"]
    # Music collections cannot be "synchronized" like Kodi sources.
    assert client.post(f"/api/v1/sources/{source_id}/sync", headers=admin).status_code == 422
    response = client.post(
        "/api/v1/imports/music/discogs",
        headers=admin,
        data={"source_id": source_id},
        files={"file": ("collection.csv", DISCOGS, "text/csv")},
    )
    assert response.status_code == 202
    assert response.json()["operation"] == "music_import"
    job = _wait(client, admin, response.json()["id"])
    assert job["status"] == "completed", job
    assert job["result"]["music_albums"] == 1

    status = client.get("/api/v1/music/status", headers=admin).json()
    assert status["albums"] == 1 and status["physical_albums"] == 1
    assert status["formats"] == {"Vinyl": 1} and status["revision"] == 1
    albums = client.get("/api/v1/music/albums", headers=admin, params={"format": "Vinyl"}).json()
    assert albums["total"] == 1
    album = albums["items"][0]
    assert album["title"] == "OK Computer" and album["owned_physical"]
    detail = client.get(f"/api/v1/music/albums/{album['id']}", headers=admin).json()
    assert detail["editions"][0]["catalog_number"] == "NODATA 02"
    artist = client.get(f"/api/v1/music/artists/{album['artist_id']}", headers=admin).json()
    assert artist["name"] == "Radiohead" and artist["albums"][0]["id"] == album["id"]
    search = client.get("/api/v1/music/search", headers=admin, params={"q": "computer"}).json()
    assert [a["id"] for a in search["albums"]] == [album["id"]]
    facets = client.get("/api/v1/music/facets", headers=admin).json()
    assert facets["formats"] == ["Vinyl"] and facets["years"] == [1997]
    sources = client.get("/api/v1/sources", headers=admin).json()
    assert next(s for s in sources if s["id"] == source_id)["music_count"] == 1

    # Wrong collection type for this source.
    wrong = client.post(
        "/api/v1/imports/music/clz_music",
        headers=admin,
        data={"source_id": source_id},
        files={"file": ("x.xml", b"<musicinfo/>", "text/xml")},
    )
    assert wrong.status_code == 422
    assert client.delete(f"/api/v1/sources/{source_id}", headers=admin).status_code == 204
    assert client.get("/api/v1/music/status", headers=admin).json()["albums"] == 0


def test_music_export_and_stream_ticket(client, admin):
    source = client.post(
        "/api/v1/sources",
        headers=admin,
        json={"name": "Kodi", "type": "kodi", "config": {"music_enabled": True}},
    ).json()
    assert source["config"]["music_enabled"] is True
    store = client.app.state.store
    MusicCatalogue(store).apply_snapshot(
        source["id"],
        MusicSnapshot(
            albums=[MusicAlbumRecord(external_key="album:1", title="Moon Safari", artists=["Air"])],
            tracks=[
                MusicTrackRecord(
                    external_key=f"song:{i}",
                    title=f"Track {i}",
                    album_key="album:1",
                    artists=["Air"],
                    track=i,
                    file=f"smb://nas/music/air/{i:02}.flac",
                    playable=True,
                )
                for i in range(1, 6)
            ],
        ),
    )
    page = client.get("/api/v1/music/export/tracks", headers=admin, params={"limit": 3}).json()
    assert len(page["items"]) == 3 and page["next"] is not None and page["total"] == 5
    rest = client.get(
        "/api/v1/music/export/tracks", headers=admin, params={"after": page["next"]}
    ).json()
    assert len(rest["items"]) == 2 and rest["next"] is None
    player = client.post(
        "/api/v1/players",
        headers=admin,
        json={"name": "Salon", "source_id": source["id"], "host": "kodi.local"},
    )
    assert player.status_code == 201, player.text
    track_id = page["items"][0]["id"]
    ticket = client.post(
        f"/api/v1/players/{player.json()['id']}/music-stream",
        headers=admin,
        json={"track_id": track_id},
    )
    assert ticket.status_code == 200, ticket.text
    assert ticket.json()["path"].endswith("/audio.flac")
    assert client.get("/api/v1/music/albums", headers=admin).json()["items"][0]["playable"]
    unauthenticated = client.get("/api/v1/music/albums")
    assert unauthenticated.status_code == 401


def test_browser_music_stream_uses_matching_smb_share_without_kodi(client, admin, monkeypatch):
    """A browser audio GET must stream without a Kodi player or a POST ticket."""
    from fastapi.responses import StreamingResponse

    from home_media_catalog import local_stream

    source = client.post(
        "/api/v1/sources",
        headers=admin,
        json={"name": "Kodi music", "type": "kodi", "config": {"music_enabled": True}},
    ).json()
    MusicCatalogue(client.app.state.store).apply_snapshot(
        source["id"],
        MusicSnapshot(
            albums=[MusicAlbumRecord(external_key="album:1", title="Album", artists=["Artist"])],
            tracks=[MusicTrackRecord(
                external_key="song:1", title="Song", album_key="album:1",
                artists=["Artist"], track=1,
                file="smb://nas/Music/Artist/Song.flac", playable=True,
            )],
        ),
    )
    location = client.post(
        "/api/v1/playback/locations", headers=admin,
        json={"name": "NAS", "kind": "smb", "target": "smb://nas/Music"},
    )
    assert location.status_code == 201, location.text

    seen = []

    async def fake_stream(location, relative, range_header, *args, **kwargs):
        seen.append((location["kind"], relative, range_header))
        return StreamingResponse(
            iter([b"audio bytes"]), status_code=206, media_type="audio/flac",
            headers={"Content-Range": "bytes 0-10/11", "Content-Length": "11"},
        )

    monkeypatch.setattr(local_stream, "stream_location", fake_stream)
    track_id = client.get(
        "/api/v1/music/tracks", headers=admin
    ).json()["items"][0]["id"]
    path = f"/api/v1/players/music/tracks/{track_id}/stream"
    assert client.get(path).status_code == 401
    response = client.get(path, headers={**admin, "Range": "bytes=0-10"})
    assert response.status_code == 206, response.text
    assert response.content == b"audio bytes"
    assert seen == [("smb", "Artist/Song.flac", "bytes=0-10")]


def _browser_audio_fixture(client, admin, *, with_player=True, with_share=False):
    source_response = client.post(
        "/api/v1/sources", headers=admin,
        json={"name": "Audio Kodi", "type": "kodi", "config": {"music_enabled": True}},
    )
    assert source_response.status_code == 201, source_response.text
    source_id = source_response.json()["id"]
    MusicCatalogue(client.app.state.store).apply_snapshot(
        source_id,
        MusicSnapshot(
            albums=[MusicAlbumRecord(
                external_key="test-album", title="Test Album", artists=["Test Artist"]
            )],
            tracks=[MusicTrackRecord(
                external_key="test-track", title="Test Track",
                album_key="test-album", artists=["Test Artist"], track=1,
                file="smb://nas/Music/Test Artist/01.flac", playable=True,
            )],
        ),
    )
    if with_player:
        created = client.post(
            "/api/v1/players", headers=admin,
            json={
                "name": "Offline Kodi", "source_id": source_id,
                "host": "kodi.local", "port": 8080,
                "username": "remote", "password": "test-secret",
            },
        )
        assert created.status_code == 201, created.text
    if with_share:
        created = client.post(
            "/api/v1/playback/locations", headers=admin,
            json={"name": "Audio NAS", "kind": "smb", "target": "smb://nas/Music"},
        )
        assert created.status_code == 201, created.text
    return client.get("/api/v1/music/tracks", headers=admin).json()["items"][0]["id"]


def test_browser_music_transcode_uses_private_kodi_configuration(client, admin, monkeypatch):
    """Public player views deliberately lack config; transcoding must load it privately."""
    from fastapi import HTTPException

    from home_media_catalog import local_stream

    track_id = _browser_audio_fixture(client, admin)
    assert "config" not in client.app.state.store.players()[0]
    assert "config" in client.app.state.store.player(
        client.app.state.store.players()[0]["id"]
    )
    monkeypatch.setattr(local_stream.shutil, "which", lambda command: "/usr/bin/ffmpeg")
    seen = []

    def fake_kodi_source(url, auth, key):
        seen.append((url, auth, key))
        raise HTTPException(502, "Kodi is deliberately unavailable in this test.")

    monkeypatch.setattr(local_stream, "kodi_source", fake_kodi_source)
    path = f"/api/v1/players/music/tracks/{track_id}/transcode.mp3"
    response = client.get(path, headers=admin)
    assert response.status_code == 502, response.text
    assert "KeyError" not in response.text
    assert len(seen) == 1
    assert "/vfs/smb%3A%2F%2Fnas%2FMusic" in seen[0][0]
    assert seen[0][1] == ("remote", "test-secret")
    assert seen[0][2] == "music:" + track_id


def test_browser_music_transcode_falls_back_from_share_to_kodi(client, admin, monkeypatch):
    """An unavailable SMB share must not prevent another eligible Kodi VFS source."""
    import io

    from fastapi import HTTPException

    from home_media_catalog import local_stream

    track_id = _browser_audio_fixture(client, admin, with_share=True)
    monkeypatch.setattr(local_stream.shutil, "which", lambda command: "/usr/bin/ffmpeg")
    attempted = []

    def failed_share(location, relative, password, mounts):
        attempted.append(("smb", relative))
        raise HTTPException(502, "Simulated offline share")

    def usable_kodi(url, auth, key):
        attempted.append(("kodi", key))
        return object()

    class StubBridge:
        def __init__(self, source, name):
            self.url = "http://127.0.0.1/mock-file"
        def __enter__(self):
            return self
        def close(self):
            pass

    class StubProcess:
        def __init__(self):
            self.stdout = io.BufferedReader(io.BytesIO(b"encoded mp3"))
        def poll(self):
            return 0
        def wait(self):
            return 0

    monkeypatch.setattr(local_stream, "network_source", failed_share)
    monkeypatch.setattr(local_stream, "kodi_source", usable_kodi)
    monkeypatch.setattr(local_stream, "LoopbackBridge", StubBridge)
    monkeypatch.setattr(local_stream.subprocess, "Popen", lambda *args, **kwargs: StubProcess())

    path = f"/api/v1/players/music/tracks/{track_id}/transcode.mp3"
    header = client.head(path, headers=admin)
    assert header.status_code == 200, header.text
    response = client.get(path, headers=admin)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("audio/mpeg")
    assert response.content == b"encoded mp3"
    assert attempted == [
        ("smb", "Test Artist/01.flac"),
        ("kodi", "music:" + track_id),
        ("smb", "Test Artist/01.flac"),
        ("kodi", "music:" + track_id),
    ]


def test_music_artwork_unwraps_and_never_persists(client, admin, monkeypatch):
    """A Kodi image:// SMB folder.jpg is decoded and returned without writing disk cache."""
    from home_media_catalog import music_api

    source = client.post(
        "/api/v1/sources", headers=admin,
        json={"name": "Artwork Kodi", "type": "kodi", "config": {}},
    ).json()
    MusicCatalogue(client.app.state.store).apply_snapshot(
        source["id"], MusicSnapshot(
            albums=[
                MusicAlbumRecord(
                    external_key="album:cover", title="Music with Folder Art",
                    artists=["Artist"],
                    thumb=["image://smb%3A%2F%2Fnas%2FMusic%2Ffolder.jpg/"],
                )
            ]
        ),
    )
    images = []
    jpeg = b"\xff\xd8\xff\xe0" + b"fake-bounded-test-image"

    def network_cover(_request, reference):
        images.append(reference)
        return jpeg

    monkeypatch.setattr(music_api, "_music_network_cover", network_cover)

    def unexpected_save(*_args, **_kwargs):
        raise AssertionError("Music artwork must not persist on the server")

    monkeypatch.setattr(client.app.state.artwork, "_save", unexpected_save)
    album = client.get("/api/v1/music/albums", headers=admin).json()["items"][0]
    response = client.get(
        f"/api/v1/music/albums/{album['id']}/artwork/thumb", headers=admin
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"].startswith("private")
    assert response.content == jpeg
    assert images == ["smb://nas/Music/folder.jpg"]
    assert music_api._unwrapped_music_artwork("image://music%40smb%3A%2F%2Fnas%2Ftrack.flac/") == (
        "music@smb://nas/track.flac"
    )


def test_web_music_controls_and_seek_offset_are_wired():
    from pathlib import Path

    static = Path(__file__).resolve().parents[1] / "home_media_catalog" / "static"
    markup = (static / "index.html").read_text()
    js = (static / "music.js").read_text()
    for control in ("music-play-pause", "music-seek", "music-duration", "music-volume", "music-mute"):
        assert f'id="{control}"' in markup
    assert "audio.pause();" in js
    assert "audio.volume =" in js
    assert "audio.addEventListener(name, updateMusicTransport)" in js
    assert "playCurrent(destination, true)" in js
