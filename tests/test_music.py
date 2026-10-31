"""Music library: matching, publication, Kodi AudioLibrary and physical imports."""

import asyncio
from contextlib import asynccontextmanager

import pytest

from home_media_catalog.connectors.base import ConnectorError
from home_media_catalog.connectors.kodi_music import (
    KodiMusicConnector,
    fetch_kodi_music_snapshot,
    normalize_album,
    normalize_song,
    normalize_sql_album,
    normalize_sql_song,
)
from home_media_catalog.connectors.music_base import (
    MusicAlbumRecord,
    MusicArtistRecord,
    MusicSnapshot,
    MusicTrackRecord,
    canonical_music_format,
)
from home_media_catalog.connectors.music_collections import (
    parse_clz_music,
    parse_music_collection,
)
from home_media_catalog.music import MusicCatalogue, album_match_key, name_key, title_key


def _source(store, source_type, name=None):
    return store.save_source({"name": name or source_type, "type": source_type, "config": {}})["id"]


def _kodi_snapshot():
    return MusicSnapshot(
        artists=[
            MusicArtistRecord(external_key="artist:1", name="Radiohead", mbid="a74b1b7f"),
            MusicArtistRecord(external_key="artist:2", name="The Beatles"),
        ],
        albums=[
            MusicAlbumRecord(
                external_key="album:10",
                title="OK Computer",
                artists=["Radiohead"],
                artist_keys=["artist:1"],
                year=1997,
                genres=["Rock"],
                release_group_mbid="rg-okc",
                thumb=["http://example.org/okc.jpg"],
            ),
            MusicAlbumRecord(
                external_key="album:11",
                title="Abbey Road (Remastered 2009)",
                artists=["The Beatles"],
                artist_keys=["artist:2"],
                year=1969,
            ),
        ],
        tracks=[
            MusicTrackRecord(
                external_key="song:100",
                title="Airbag",
                album_key="album:10",
                artists=["Radiohead"],
                track=1,
                duration_seconds=284,
                file="smb://nas/music/okc/01.flac",
                playable=True,
                codec="FLAC",
            ),
            MusicTrackRecord(
                external_key="song:101",
                title="Paranoid Android",
                album_key="album:10",
                artists=["Radiohead"],
                track=2,
                duration_seconds=387,
                file="smb://nas/music/okc/02.flac",
                playable=True,
            ),
            MusicTrackRecord(
                external_key="song:200",
                title="Come Together",
                album_key="album:11",
                artists=["The Beatles"],
                track=1,
                duration_seconds=259,
                file="smb://nas/music/abbey/01.flac",
                playable=True,
            ),
        ],
    )


def test_keys_ignore_case_accents_articles_and_edition_qualifiers():
    assert name_key("The Beatles") == name_key("beatles")
    assert name_key("Simon & Garfunkel") == name_key("Simon and Garfunkel")
    assert title_key("Abbey Road (Remastered 2009)") == title_key("Abbey Road")
    assert title_key("Discovery [Deluxe Edition]") == "discovery"
    assert album_match_key("Björk", "Homogenic") == album_match_key("Bjork", "HOMOGENIC")
    assert album_match_key("x", "Now 42", compilation=True).startswith("various artists|")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("LP, Album, RE", "Vinyl"),
        ("2xLP, 180g", "Vinyl"),
        ('7", Single', "Vinyl"),
        ("CD, Album", "CD"),
        ("CD, EP", "CD"),
        ("Hybrid SACD", "SACD"),
        ("Cass, Album", "Cassette"),
        ("Vinyle", "Vinyl"),
        ("FLAC", "Digital"),
        ("", "Other"),
    ],
)
def test_canonical_music_formats(value, expected):
    assert canonical_music_format(value) == expected


def test_kodi_snapshot_publishes_artists_albums_and_tracks(store):
    source = _source(store, "kodi")
    music = MusicCatalogue(store)
    result = music.apply_snapshot(source, _kodi_snapshot())
    assert result["music_albums"] == 2 and result["music_tracks"] == 3
    status = music.status()
    assert (status["artists"], status["albums"], status["tracks"]) == (2, 2, 3)
    assert status["revision"] == 1
    albums = music.albums()["items"]
    assert [a["title"] for a in albums] == ["Abbey Road (Remastered 2009)", "OK Computer"]
    okc = next(a for a in albums if a["title"] == "OK Computer")
    assert okc["playable"] and okc["track_count"] == 2 and okc["duration_seconds"] == 671
    assert okc["formats"] == ["Digital"] and okc["thumb"] == "http://example.org/okc.jpg"
    detail = music.album(okc["id"])
    assert [t["title"] for t in detail["tracks"]] == ["Airbag", "Paranoid Android"]
    assert detail["tracks"][0]["audio"] == {"codec": "FLAC"}
    artist = music.artist(okc["artist_id"])
    assert artist["name"] == "Radiohead" and artist["album_count"] == 1
    assert artist["track_count"] == 2


def test_physical_editions_merge_with_digital_album_and_ids_are_stable(store):
    kodi = _source(store, "kodi")
    vinyls = _source(store, "discogs")
    music = MusicCatalogue(store)
    music.apply_snapshot(kodi, _kodi_snapshot())
    first_ids = {a["title"]: a["id"] for a in music.albums()["items"]}
    csv = (
        b"Catalog#,Artist,Title,Label,Format,Rating,Released,release_id,CollectionFolder,"
        b"Date Added,Collection Media Condition,Collection Sleeve Condition,Collection Notes\n"
        b'NODATA 02,Radiohead,OK Computer,Parlophone,"2xLP, Album, RE",5,1997,1234,'
        b"Uncategorized,2024-01-01,Mint (M),Near Mint (NM or M-),Gatefold\n"
        b'PCS 7088,Beatles (2),Abbey Road,Apple,"LP, Album",4,1969,555,Uncategorized,'
        b"2024-02-01,,,\n"
        b'XL 001,Adele,21,XL,"CD, Album",,2011,777,Uncategorized,2024-03-01,,,\n'
    )
    result = music.apply_snapshot(vinyls, parse_music_collection(csv, "discogs"))
    assert result["music_albums"] == 3
    albums = {a["title"]: a for a in music.albums()["items"]}
    # Discogs "(2)" disambiguation, missing "The" and edition qualifiers still match.
    assert albums["OK Computer"]["id"] == first_ids["OK Computer"]
    assert albums["Abbey Road (Remastered 2009)"]["id"] == first_ids["Abbey Road (Remastered 2009)"]
    assert albums["OK Computer"]["formats"] == ["Digital", "Vinyl"]
    assert albums["OK Computer"]["owned_physical"] and albums["OK Computer"]["playable"]
    assert albums["21"]["formats"] == ["CD"] and not albums["21"]["playable"]
    detail = music.album(albums["OK Computer"]["id"])
    vinyl = next(e for e in detail["editions"] if e["format"] == "Vinyl")
    assert vinyl["catalog_number"] == "NODATA 02" and vinyl["discogs_id"] == "1234"
    assert vinyl["media_condition"] == "Mint (M)" and vinyl["format_detail"] == "2xLP, Album, RE"
    assert music.status()["artists"] == 3
    # Filters.
    assert {a["title"] for a in music.albums(fmt="Vinyl")["items"]} == {
        "OK Computer",
        "Abbey Road (Remastered 2009)",
    }
    assert [a["title"] for a in music.albums(playable=False)["items"]] == ["21"]
    # Re-importing the Kodi library keeps album IDs (lists and caches stay valid).
    music.apply_snapshot(kodi, _kodi_snapshot())
    assert {a["title"]: a["id"] for a in music.albums()["items"]}["OK Computer"] == albums[
        "OK Computer"
    ]["id"]


def test_removing_a_source_keeps_albums_owned_elsewhere(store):
    kodi = _source(store, "kodi")
    collection = _source(store, "music_csv")
    music = MusicCatalogue(store)
    music.apply_snapshot(kodi, _kodi_snapshot())
    music.apply_snapshot(
        collection,
        parse_music_collection(b"Artiste;Titre;Support\nRadiohead;OK Computer;CD\n", "music_csv"),
    )
    store.delete_source(kodi)
    albums = music.albums()["items"]
    assert [a["title"] for a in albums] == ["OK Computer"]
    assert albums[0]["formats"] == ["CD"] and not albums[0]["playable"]
    assert music.tracks()["total"] == 0
    assert music.status()["artists"] == 1


def test_empty_snapshot_prunes_and_search_finds_tracks(store):
    kodi = _source(store, "kodi")
    music = MusicCatalogue(store)
    music.apply_snapshot(kodi, _kodi_snapshot())
    found = music.search("paranoid")
    assert [t["title"] for t in found["tracks"]] == ["Paranoid Android"]
    assert [a["title"] for a in music.search("radiohead")["albums"]] == ["OK Computer"]
    assert music.tracks(query="come tog")["items"][0]["artist"] == "The Beatles"
    music.apply_snapshot(kodi, MusicSnapshot())
    status = music.status()
    assert (status["artists"], status["albums"], status["tracks"]) == (0, 0, 0)
    assert music.search("paranoid")["tracks"] == []


def test_export_is_keyset_paginated(store):
    kodi = _source(store, "kodi")
    music = MusicCatalogue(store)
    music.apply_snapshot(kodi, _kodi_snapshot())
    first = music.export("tracks", limit=2)
    assert len(first["items"]) == 2 and first["next"] is not None and first["total"] == 3
    second = music.export("tracks", after=first["next"], limit=2)
    assert len(second["items"]) == 1 and second["next"] is None
    albums = music.export("albums")
    assert all(item["artist_ids"] for item in albums["items"])


def test_jsonrpc_normalizers():
    album = normalize_album(
        {
            "albumid": 7,
            "title": "Kid A",
            "artist": ["Radiohead"],
            "artistid": [3],
            "year": 2000,
            "genre": ["Electronic"],
            "musicbrainzreleasegroupid": "rg",
            "art": {"thumb": "image://http%3a%2f%2fexample.org%2fkida.jpg/"},
            "releasetype": "album",
        }
    )
    assert album.external_key == "album:7" and album.artist_keys == ["artist:3"]
    assert album.thumb == ["http://example.org/kida.jpg"] and album.release_group_mbid == "rg"
    song = normalize_song(
        {
            "songid": 9,
            "title": "Idioteque",
            "albumid": 7,
            "artist": ["Radiohead"],
            "track": 8,
            "disc": 1,
            "duration": 309,
            "file": "nfs://nas/m/08.flac",
            "bitrate": 900,
            "samplerate": 44100,
            "channels": 2,
        }
    )
    assert song.album_key == "album:7" and song.playable and song.codec == "FLAC"
    assert (song.track, song.disc, song.sample_rate) == (8, 1, 44100)
    with pytest.raises(ConnectorError):
        normalize_song({"title": "orphan"})


def test_jsonrpc_snapshot_retries_with_conservative_properties():
    calls = []

    async def rpc(method, params):
        calls.append((method, len(params["properties"]), params["limits"]["start"]))
        if method == "AudioLibrary.GetSongs" and "bitrate" in params["properties"]:
            raise ConnectorError(
                "Kodi rejected JSON-RPC method AudioLibrary.GetSongs (code -32602)."
            )
        if method == "AudioLibrary.GetArtists":
            return {"limits": {"total": 1}, "artists": [{"artistid": 1, "artist": "Air"}]}
        if method == "AudioLibrary.GetAlbums":
            return {
                "limits": {"total": 1},
                "albums": [
                    {"albumid": 2, "title": "Moon Safari", "artist": ["Air"], "artistid": [1]}
                ],
            }
        start = params["limits"]["start"]
        songs = [
            {"songid": i, "title": f"T{i}", "albumid": 2, "file": f"/m/{i}.mp3"}
            for i in range(start, min(start + 2000, 2001))
        ]
        return {"limits": {"total": 2001}, "songs": songs}

    @asynccontextmanager
    async def factory(_config, **_kwargs):
        yield rpc

    progress = []
    snapshot = asyncio.run(
        fetch_kodi_music_snapshot({}, lambda *a: progress.append(a), connection_factory=factory)
    )
    assert len(snapshot.tracks) == 2001 and snapshot.albums[0].artists == ["Air"]
    song_calls = [c for c in calls if c[0] == "AudioLibrary.GetSongs"]
    assert song_calls[0][1] > song_calls[1][1]  # retried with the fallback list
    assert progress[-1] == ("music_songs", 2001, 2001)


def test_jsonrpc_snapshot_rejects_truncated_pages():
    async def rpc(method, params):
        return {"limits": {"total": 5}, "artists": [], "albums": [], "songs": []}

    @asynccontextmanager
    async def factory(_config, **_kwargs):
        yield rpc

    with pytest.raises(ConnectorError, match="incomplete"):
        asyncio.run(fetch_kodi_music_snapshot({}, connection_factory=factory))


def test_mymusic_sql_normalizers_handle_version_differences():
    album = normalize_sql_album(
        {
            "idAlbum": 4,
            "strAlbum": "Homogenic",
            "strArtistDisp": "Björk",
            "strReleaseDate": "1997-09-22",
            "strGenres": "Electronic / Pop",
            "strReleaseGroupMBID": "rg-h",
        },
        {"thumb": "smb://nas/music/bjork/folder.jpg"},
        [12],
    )
    assert album.year == 1997 and album.genres == ["Electronic", "Pop"]
    assert album.artist_keys == ["artist:12"] and album.thumb == [
        "smb://nas/music/bjork/folder.jpg"
    ]
    legacy = normalize_sql_album(
        {"idAlbum": 5, "strAlbum": "Post", "strArtists": "Björk", "iYear": 1995}, {}, []
    )
    assert legacy.year == 1995 and legacy.artists == ["Björk"]
    song = normalize_sql_song(
        {
            "idSong": 1,
            "strTitle": "Jóga",
            "idAlbum": 4,
            "iTrack": (2 << 16) | 3,
            "strPath": "smb://nas/music/bjork/",
            "strFileName": "03.flac",
            "iDuration": 305,
            "iBitRate": 1050000,
            "strArtistDisp": "Björk",
        }
    )
    assert (song.disc, song.track) == (2, 3) and song.bitrate == 1050
    assert song.file == "smb://nas/music/bjork/03.flac" and song.codec == "FLAC"


def test_music_connector_selects_mymusic_database():
    class Cursor:
        def execute(self, _sql):
            pass

        def fetchall(self):
            return [
                {"Database": "MyVideos131"},
                {"Database": "MyMusic82"},
                {"Database": "MyMusic83"},
            ]

    choices = KodiMusicConnector._visible_video_databases(Cursor())
    assert choices[0] == (83, "MyMusic83")
    connector = KodiMusicConnector({"database": "MyVideos131", "music_database": "MyMusic82"})
    assert connector.config["database"] == "MyMusic82"


def test_generic_csv_french_headers_and_inline_tracklist():
    data = (
        "Artiste;Album;Année;Support;Référence;Code-barres;Genre;Pistes\n"
        "Daft Punk;Discovery;2001;Vinyle 2LP;V2940;724384960612;Electro;"
        "A1 One More Time|A2 Aerodynamic|B1 Digital Love\n"
        ";Sans artiste;;CD;;;;\n"
    ).encode("cp1252")
    snapshot = parse_music_collection(data, "music_csv")
    assert len(snapshot.albums) == 1 and snapshot.report["music_rows_skipped"] == 1
    album = snapshot.albums[0]
    assert (album.format, album.year, album.barcode) == ("Vinyl", 2001, "724384960612")
    assert [t.position for t in snapshot.tracks] == ["A1", "A2", "B1"]
    assert snapshot.tracks[2].title == "Digital Love" and not snapshot.tracks[0].playable


def test_generic_json_list_and_missing_columns():
    snapshot = parse_music_collection(
        b'[{"artist": "Nina Simone", "title": "Pastel Blues", "format": "LP"}]', "music_csv"
    )
    assert snapshot.albums[0].format == "Vinyl"
    with pytest.raises(ConnectorError, match="artist"):
        parse_music_collection(b"Name,Year\nx,1999\n", "music_csv")


def test_clz_music_xml_with_discs_and_tracks():
    xml = b"""<?xml version="1.0"?>
<musicinfo><musiclist>
  <music>
    <index>12</index><title>The Wall</title>
    <artists><artist><displayname>Pink Floyd</displayname></artist></artists>
    <format><displayname>CD</displayname></format>
    <releasedate><year><displayname>1979</displayname></year></releasedate>
    <labels><label><displayname>Harvest</displayname></label></labels>
    <barcode>5099902894428</barcode>
    <discs>
      <disc><tracks><track><title>In the Flesh?</title><position>1</position><length>3:19</length></track></tracks></disc>
      <disc><tracks><track><title>Hey You</title><position>1</position><length>4:40</length></track></tracks></disc>
    </discs>
  </music>
</musiclist></musicinfo>"""
    snapshot = parse_clz_music(xml)
    album = snapshot.albums[0]
    assert (album.title, album.year, album.label, album.format) == (
        "The Wall",
        1979,
        "Harvest",
        "CD",
    )
    assert album.disc_count == 2
    assert [(t.disc, t.title, t.duration_seconds) for t in snapshot.tracks] == [
        (1, "In the Flesh?", 199.0),
        (2, "Hey You", 280.0),
    ]
    with pytest.raises(ConnectorError):
        parse_clz_music(b"<nope/>")


def test_backup_round_trip_keeps_music_and_accepts_older_backups(store):
    from home_media_catalog.backup import MUSIC_TABLES, export_backup, import_backup

    kodi = _source(store, "kodi")
    music = MusicCatalogue(store)
    music.apply_snapshot(kodi, _kodi_snapshot())
    album_ids = {a["id"] for a in music.albums()["items"]}
    document = export_backup(store)
    assert set(MUSIC_TABLES) <= set(document["tables"])
    music.apply_snapshot(kodi, MusicSnapshot())
    import_backup(store, document)
    assert {a["id"] for a in music.albums()["items"]} == album_ids
    assert [t["title"] for t in music.tracks(query="airbag")["items"]] == ["Airbag"]
    older = {
        **document,
        "tables": {k: v for k, v in document["tables"].items() if k not in MUSIC_TABLES},
    }
    import_backup(store, older)
    assert music.status()["albums"] == 0
