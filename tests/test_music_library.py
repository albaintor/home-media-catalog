"""Music library: Kodi AudioLibrary, MyMusic SQL fallback and physical collections."""

import asyncio
import time
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest

from home_media_catalog.connectors.base import ConnectorError, Record
from home_media_catalog.connectors.kodi_music import (
    fetch_kodi_music_snapshot,
    normalize_song,
    sql_song,
)
from home_media_catalog.connectors.music import (
    artist_key,
    music_match_keys,
    parse_clz_music_xml,
    parse_discogs_csv,
    parse_music_csv,
    physical_format,
)

DISCOGS = (
    b"Catalog#,Artist,Title,Label,Format,Rating,Released,release_id,CollectionFolder,"
    b"Date Added,Collection Media Condition,Collection Sleeve Condition,Collection Notes\n"
    b"PCS 7088,The Beatles,Abbey Road,Apple Records,\"Vinyl, LP, Album\",5,1969,123,"
    b"Uncategorized,2024-01-02 10:00:00,Near Mint (NM or M-),Very Good Plus (VG+),\n"
    b"CDP 7 46446 2,The Beatles,Abbey Road (Remastered),Parlophone,\"CD, Album, RE\",4,1987,"
    b"456,Uncategorized,2024-02-02 10:00:00,,,\n"
    b"MAN 1,Nirvana (2),Local Hero,Pye,\"7\"\", Single\",,1968,789,Uncategorized,,,,\n"
)


def kodi_music_rows():
    artists = [
        {"artistid": 1, "artist": "The Beatles", "musicbrainzartistid": ["b10bbbfc-cf9e-42e0-be17-e2c3e1d2600d"]},
        {"artistid": 2, "artist": "Miles Davis"},
    ]
    albums = [
        {
            "albumid": 10,
            "title": "Abbey Road",
            "artist": ["The Beatles"],
            "artistid": [1],
            "displayartist": "The Beatles",
            "year": 1969,
            "genre": ["Rock"],
            "thumbnail": "image://smb%3a%2f%2fnas%2fmusic%2fabbey.jpg/",
        },
        {
            "albumid": 11,
            "title": "Kind of Blue",
            "artist": ["Miles Davis"],
            "artistid": [2],
            "displayartist": "Miles Davis",
            "year": 1959,
        },
    ]
    songs = [
        {
            "songid": 100 + n,
            "title": title,
            "albumid": 10,
            "album": "Abbey Road",
            "artist": ["The Beatles"],
            "displayartist": "The Beatles",
            "albumartist": ["The Beatles"],
            "track": n + 1,
            "disc": 1,
            "duration": 200 + n,
            "file": f"smb://nas/music/abbey/{n:02d}.flac",
        }
        for n, title in enumerate(["Come Together", "Something", "Oh! Darling"])
    ] + [
        {
            "songid": 200,
            "title": "So What",
            "albumid": 11,
            "album": "Kind of Blue",
            "artist": ["Miles Davis"],
            "track": 1,
            "duration": 562,
            "file": "smb://nas/music/kob/01.mp3",
        }
    ]
    return artists, albums, songs


def fake_kodi(artists, albums, songs, calls=None):
    data = {"GetArtists": ("artists", artists), "GetAlbums": ("albums", albums), "GetSongs": ("songs", songs)}

    async def rpc(method, params):
        if method == "JSONRPC.Ping":
            return "pong"
        if calls is not None:
            calls.append((method, params.get("limits", {}).get("start")))
        key, rows = data[method.split(".")[-1]]
        start, end = params["limits"]["start"], params["limits"]["end"]
        return {"limits": {"total": len(rows)}, key: rows[start:end]}

    @asynccontextmanager
    async def connection(_config, **_kwargs):
        yield rpc

    return connection


def test_physical_formats_and_artist_keys():
    assert physical_format("Vinyl, LP, Album") == "Vinyl"
    assert physical_format("2xLP, Album, RE") == "Vinyl"
    assert physical_format("CD, Album, Remastered") == "CD"
    assert physical_format("Cassette, Album") == "Cassette"
    assert physical_format("File, FLAC, Album") == "Digital"
    assert physical_format("Hybrid SACD") == "SACD"
    assert artist_key("The Beatles") == artist_key("Beatles, The") == "beatles"
    assert artist_key("Nirvana (2)") == "nirvana"
    keys = music_match_keys("album", "Abbey Road (2019 Remaster)", {"artist": "The Beatles"})
    assert keys == music_match_keys("album", "Abbey Road", {"album_artist": "Beatles, The"})


def test_discogs_csv_creates_artists_and_physical_albums():
    records = parse_discogs_csv(DISCOGS)
    kinds = [r.kind for r in records]
    assert kinds.count("artist") == 2 and kinds.count("album") == 3
    vinyl = next(r for r in records if r.external_key == "album:discogs:123")
    assert vinyl.formats == ["Vinyl"]
    assert vinyl.user_rating == 10
    assert vinyl.metadata["physical"] is True and vinyl.metadata["playable"] is False
    assert vinyl.metadata["media_condition"].startswith("Near Mint")
    assert vinyl.parent_key == "artist:beatles"
    single = next(r for r in records if r.external_key == "album:discogs:789")
    assert single.metadata["artist"] == "Nirvana" and single.formats == ["Vinyl"]


def test_generic_csv_accepts_french_headers_and_semicolons():
    data = "Artiste;Album;Année;Support;Genre\nIAM;L'École du micro d'argent;1997;CD;Rap\n"
    records = parse_music_csv(data.encode("cp1252"))
    album = next(r for r in records if r.kind == "album")
    assert album.title == "L'École du micro d'argent"
    assert album.year == 1997 and album.formats == ["CD"] and album.genres == ["Rap"]
    with pytest.raises(ConnectorError):
        parse_music_csv(b"foo,bar\n1,2\n")


def test_clz_music_xml_reads_tracks():
    xml = b"""<?xml version="1.0"?><musicinfo><musiclist><music>
      <id>42</id><title>Blue Train</title>
      <artists><artist><displayname>John Coltrane</displayname></artist></artists>
      <format><displayname>Vinyl</displayname></format>
      <releasedate><year><displayname>1957</displayname></year></releasedate>
      <genres><genre><displayname>Jazz</displayname></genre></genres>
      <discs><disc><tracks>
        <track><title>Blue Train</title><position>1</position><length>10:43</length></track>
        <track><title>Moment's Notice</title><position>2</position><length>9:10</length></track>
      </tracks></disc></discs>
    </music></musiclist></musicinfo>"""
    records = parse_clz_music_xml(xml)
    album = next(r for r in records if r.kind == "album")
    assert album.external_key == "album:clz:42" and album.year == 1957
    assert album.metadata["track_count"] == 2
    assert album.metadata["duration_seconds"] == 643 + 550
    assert album.metadata["tracks"][1]["title"] == "Moment's Notice"


def test_kodi_song_and_sql_track_packing():
    song = normalize_song({"songid": 5, "title": "x", "albumid": 3, "track": 4, "disc": 2,
                           "duration": 61, "file": "/music/x.flac"})
    assert (song.season, song.episode, song.formats) == (2, 4, ["FLAC"])
    assert song.parent_key == "album:3" and song.runtime_minutes == 2
    packed = sql_song({"idSong": 9, "strTitle": "y", "idAlbum": 3, "iTrack": (2 << 16) | 7,
                       "strPath": "smb://nas/a/", "strFileName": "07.ogg", "iDuration": 100,
                       "strReleaseDate": "1999-01-01"})
    assert (packed.season, packed.episode, packed.year) == (2, 7, 1999)
    assert packed.metadata["file"] == "smb://nas/a/07.ogg" and packed.formats == ["Ogg Vorbis"]


def test_jsonrpc_music_snapshot_pages_and_totals():
    artists, albums, songs = kodi_music_rows()
    calls = []
    with patch(
        "home_media_catalog.connectors.kodi_music.connection", fake_kodi(artists, albums, songs, calls)
    ):
        records = asyncio.run(fetch_kodi_music_snapshot({"host": "kodi"}))
    assert [c[0] for c in calls] == [
        "AudioLibrary.GetArtists", "AudioLibrary.GetAlbums", "AudioLibrary.GetSongs"
    ]
    abbey = next(r for r in records if r.external_key == "album:10")
    assert abbey.metadata["track_count"] == 3 and abbey.formats == ["FLAC"]
    assert abbey.parent_key == "artist:1"
    assert abbey.identifiers == {}
    assert abbey.poster == ["smb://nas/music/abbey.jpg"]


def test_incomplete_music_page_is_rejected():
    async def rpc(_method, _params):
        return {"limits": {"total": 3}, "artists": []}

    @asynccontextmanager
    async def connection(_config, **_kwargs):
        yield rpc

    with patch("home_media_catalog.connectors.kodi_music.connection", connection):
        with pytest.raises(ConnectorError, match="incomplete"):
            asyncio.run(fetch_kodi_music_snapshot({"host": "kodi"}))


def _kodi_records():
    artists, albums, songs = kodi_music_rows()
    with patch("home_media_catalog.connectors.kodi_music.connection", fake_kodi(artists, albums, songs)):
        return asyncio.run(fetch_kodi_music_snapshot({"host": "kodi"}))


def test_music_and_video_snapshots_are_independent(store):
    kodi = store.save_source({"name": "Kodi", "type": "kodi", "config": {}})["id"]
    store.apply_snapshot(kodi, [Record("movie:1", "movie", "Alien", year=1979)])
    store.apply_snapshot(kodi, _kodi_records(), scope="music")
    # A later video snapshot must not erase the music library, and vice versa.
    store.apply_snapshot(kodi, [Record("movie:1", "movie", "Alien", year=1979)])
    assert store.music_status()["songs"] == 4
    store.apply_snapshot(kodi, [], scope="music")
    assert store.music_status()["songs"] == 0
    assert store.media()["total"] == 1
    with pytest.raises(ValueError, match="outside"):
        store.apply_snapshot(kodi, [Record("song:1", "song", "x")])


def test_physical_editions_join_kodi_albums(store):
    kodi = store.save_source({"name": "Kodi", "type": "kodi", "config": {}})["id"]
    discogs = store.save_source({"name": "Mes vinyles", "type": "discogs", "config": {}})["id"]
    store.apply_snapshot(kodi, _kodi_records(), scope="music")
    store.apply_snapshot(discogs, parse_discogs_csv(DISCOGS), scope="music")
    status = store.music_status()
    # Abbey Road: Kodi FLAC + vinyl + remastered CD = one album with 3 editions.
    assert status["albums"] == 3 and status["artists"] == 3
    assert status["physical_albums"] == 2
    abbey = store.music_albums(q="abbey")["items"]
    assert len(abbey) == 1
    album = store.music_album(abbey[0]["id"])
    assert {e["source_type"] for e in album["editions"]} == {"kodi", "discogs"}
    assert len(album["editions"]) == 3
    assert {"FLAC", "Vinyl", "CD"} <= set(album["formats"])
    assert album["physical"] and album["playable"]
    assert [s["title"] for s in album["songs"]] == ["Come Together", "Something", "Oh! Darling"]
    assert album["songs"][0]["file"] == "smb://nas/music/abbey/00.flac"
    assert album["songs"][0]["duration_seconds"] == 200
    assert album["duration_seconds"] == 200 + 201 + 202
    # The artist from the physical list is the Kodi artist.
    beatles = store.music_artist(album["artist_id"])
    assert beatles["name"] == "The Beatles" and beatles["album_count"] == 1
    assert sorted(t["type"] for t in beatles["media_sources"]) == ["discogs", "kodi"]
    # Physical-only filtering and video isolation.
    physical_only = store.music_albums(physical=True, source_type="discogs")["items"]
    assert {a["title"] for a in physical_only} == {"Abbey Road", "Local Hero"}
    assert store.media()["total"] == 0
    assert "Rock" not in store.facets()["genres"]
    assert "Rock" in store.music_facets()["genres"]


def test_two_kodi_albums_with_same_title_stay_separate(store):
    kodi = store.save_source({"name": "Kodi", "type": "kodi", "config": {}})["id"]
    artists, albums, songs = kodi_music_rows()
    albums.append({**albums[0], "albumid": 12})
    with patch("home_media_catalog.connectors.kodi_music.connection", fake_kodi(artists, albums, songs)):
        records = asyncio.run(fetch_kodi_music_snapshot({"host": "kodi"}))
    store.apply_snapshot(kodi, records, scope="music")
    assert store.music_status()["albums"] == 3


def test_music_revision_and_export_pages(store):
    kodi = store.save_source({"name": "Kodi", "type": "kodi", "config": {}})["id"]
    store.apply_snapshot(kodi, _kodi_records(), scope="music")
    first = store.music_status()["revision"]
    page = store.music_export("song", limit=3)
    assert page["total"] == 4 and page["has_more"] and page["revision"] == first
    rest = store.music_export("song", limit=3, offset=3)
    ids = [s["id"] for s in page["items"] + rest["items"]]
    assert len(set(ids)) == 4
    albums = store.music_export("album")["items"]
    assert all("editions" in album for album in albums)
    store.apply_snapshot(kodi, _kodi_records()[:-1], scope="music")
    assert store.music_status()["revision"] != first
    assert store.music_songs(sort="title")["items"][0]["title"] == "Come Together"


def wait_job(client, admin, job_id):
    for _ in range(100):
        job = client.get(f"/api/v1/jobs/{job_id}", headers=admin).json()
        if job["status"] in ("completed", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_music_api_imports_and_kodi_sync(client, admin):
    discovery = client.get("/api/v1/discovery").json()
    assert "music_library" in discovery["capabilities"]
    sid = client.post(
        "/api/v1/sources", headers=admin, json={"name": "Discogs", "type": "discogs"}
    ).json()["id"]
    response = client.post(
        "/api/v1/imports/discogs",
        headers=admin,
        data={"source_id": sid},
        files={"file": ("collection.csv", DISCOGS, "text/csv")},
    )
    assert response.status_code == 202, response.text
    job = wait_job(client, admin, response.json()["id"])
    assert job["status"] == "completed" and job["operation"] == "music_import"
    assert job["result"]["albums"] == 3

    kodi = client.post(
        "/api/v1/sources",
        headers=admin,
        json={"name": "Kodi", "type": "kodi", "config": {"music_enabled": True}},
    ).json()
    assert kodi["config"]["music_enabled"] is True
    player = client.post(
        "/api/v1/players",
        headers=admin,
        json={"name": "Salon", "source_id": kodi["id"], "host": "kodi"},
    )
    assert player.status_code in (200, 201), player.text
    artists, albums, songs = kodi_music_rows()
    fake = fake_kodi(artists, albums, songs)
    with patch("home_media_catalog.sync.connection", fake), patch(
        "home_media_catalog.connectors.kodi_music.connection", fake
    ):
        response = client.post(f"/api/v1/sources/{kodi['id']}/sync-music", headers=admin)
        assert response.status_code == 202, response.text
        job = wait_job(client, admin, response.json()["id"])
    assert job["status"] == "completed", job["error"]
    assert job["operation"] == "kodi_music_sync" and job["result"]["import_method"] == "jsonrpc"

    status = client.get("/api/v1/music/status", headers=admin).json()
    assert status["albums"] == 3 and status["songs"] == 4
    albums_page = client.get("/api/v1/music/albums?sort=year", headers=admin).json()
    assert [a["year"] for a in albums_page["items"]] == [1959, 1968, 1969]
    abbey = client.get("/api/v1/music/albums?q=abbey", headers=admin).json()["items"][0]
    detail = client.get(f"/api/v1/music/albums/{abbey['id']}", headers=admin).json()
    assert len(detail["songs"]) == 3 and len(detail["editions"]) == 3
    artists_page = client.get("/api/v1/music/artists", headers=admin).json()
    assert [a["name"] for a in artists_page["items"]] == ["Miles Davis", "Nirvana", "The Beatles"]
    songs_page = client.get(f"/api/v1/music/songs?album_id={abbey['id']}", headers=admin).json()
    assert songs_page["total"] == 3
    export = client.get("/api/v1/music/export?kind=artist&limit=2", headers=admin).json()
    assert export["has_more"] and export["revision"]
    assert client.get("/api/v1/music/export?kind=movie", headers=admin).status_code == 422
    # Video endpoints remain video-only.
    assert client.get("/api/v1/media", headers=admin).json()["total"] == 0
    assert client.get("/api/v1/music/albums/unknown", headers=admin).status_code == 404


def test_music_sync_requires_kodi_source(client, admin):
    sid = client.post(
        "/api/v1/sources", headers=admin, json={"name": "CSV", "type": "music_csv"}
    ).json()["id"]
    assert client.post(f"/api/v1/sources/{sid}/sync-music", headers=admin).status_code == 422
    kodi = client.post(
        "/api/v1/sources", headers=admin, json={"name": "Kodi", "type": "kodi"}
    ).json()["id"]
    response = client.post(f"/api/v1/sources/{kodi}/sync-music", headers=admin)
    job = wait_job(client, admin, response.json()["id"])
    assert job["status"] == "failed" and "MariaDB" in job["error"]
