"""Kodi music library: JSON-RPC AudioLibrary snapshot and MyMusic MariaDB fallback.

Kodi stores music in a database separate from video (``MyMusicNN``). The
JSON-RPC AudioLibrary namespace exists since Kodi 12; only properties available
on Kodi 17+ are requested so that older-but-supported versions keep working.
A music snapshot is published only when artists, albums and songs were all read.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from ..players import connection
from ..utils import date_value, number, plain_text, positive
from .base import ConnectorError, Record
from .kodi import KodiConnector, artwork_candidates

ARTIST_PAGE = 500
ALBUM_PAGE = 500
SONG_PAGE = 1000

ARTIST_PROPERTIES = [
    "description",
    "genre",
    "thumbnail",
    "fanart",
    "musicbrainzartistid",
    "dateadded",
]
ALBUM_PROPERTIES = [
    "title",
    "artist",
    "artistid",
    "displayartist",
    "genre",
    "year",
    "rating",
    "userrating",
    "playcount",
    "lastplayed",
    "dateadded",
    "thumbnail",
    "fanart",
    "musicbrainzalbumid",
    "musicbrainzreleasegroupid",
    "label",
    "compilation",
    "releasetype",
    "description",
]
SONG_PROPERTIES = [
    "title",
    "artist",
    "artistid",
    "displayartist",
    "albumartist",
    "album",
    "albumid",
    "track",
    "disc",
    "duration",
    "year",
    "genre",
    "file",
    "rating",
    "userrating",
    "playcount",
    "lastplayed",
    "dateadded",
    "musicbrainztrackid",
]

AUDIO_FORMATS = {
    "flac": "FLAC",
    "mp3": "MP3",
    "m4a": "AAC",
    "aac": "AAC",
    "alac": "ALAC",
    "ogg": "Ogg Vorbis",
    "oga": "Ogg Vorbis",
    "opus": "Opus",
    "wav": "WAV",
    "aif": "AIFF",
    "aiff": "AIFF",
    "wma": "WMA",
    "ape": "APE",
    "wv": "WavPack",
    "dsf": "DSD",
    "dff": "DSD",
    "mka": "Matroska Audio",
}


def audio_format(path: str | None) -> str | None:
    match = re.search(r"\.([A-Za-z0-9]{2,5})$", path or "")
    return AUDIO_FORMATS.get(match[1].lower()) if match else None


def _names(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if isinstance(v, str) and v.strip()]
    if isinstance(value, str):
        return [v.strip() for v in value.split(" / ") if v.strip()]
    return []


def _ids(value: Any) -> list[int]:
    return [v for v in value if type(v) is int and v >= 0] if isinstance(value, list) else []


def _valid_id(row: dict, field: str, kind: str) -> int:
    value = row.get(field)
    if type(value) is not int or value < 0:
        raise ConnectorError(f"Kodi returned a {kind} without a valid {field}.")
    return value


def _mbid(value: Any) -> str | None:
    if isinstance(value, list):
        value = next((v for v in value if v), None)
    text = str(value or "").strip().lower()
    return text if re.fullmatch(r"[0-9a-f-]{36}", text) else None


def normalize_artist(row: dict) -> Record:
    artist_id = _valid_id(row, "artistid", "artist")
    name = str(row.get("artist") or row.get("label") or "Unknown artist")
    ids = {}
    if mbid := _mbid(row.get("musicbrainzartistid")):
        ids["musicbrainz_artist"] = mbid
    return Record(
        external_key=f"artist:{artist_id}",
        kind="artist",
        title=name,
        identifiers=ids,
        plot=plain_text(row.get("description")) or None,
        genres=_names(row.get("genre")),
        date_added=date_value(row.get("dateadded")),
        poster=artwork_candidates(row.get("thumbnail")),
        fanart=artwork_candidates(row.get("fanart")),
        metadata={"kodi_id": artist_id, "physical": False},
    )


def normalize_album(row: dict) -> Record:
    album_id = _valid_id(row, "albumid", "album")
    artists = _names(row.get("artist"))
    artist_ids = _ids(row.get("artistid"))
    display = str(row.get("displayartist") or " / ".join(artists) or "Unknown artist")
    ids = {}
    if group := _mbid(row.get("musicbrainzreleasegroupid")):
        ids["musicbrainz_releasegroup"] = group
    play_count = row.get("playcount") if type(row.get("playcount")) is int else None
    year = row.get("year") if type(row.get("year")) is int and row["year"] > 0 else None
    return Record(
        external_key=f"album:{album_id}",
        kind="album",
        title=str(row.get("title") or row.get("label") or "Untitled"),
        identifiers=ids,
        year=year,
        plot=plain_text(row.get("description")) or None,
        rating=positive(row.get("rating")),
        user_rating=positive(row.get("userrating")),
        genres=_names(row.get("genre")),
        date_added=date_value(row.get("dateadded")),
        last_played=date_value(row.get("lastplayed")),
        watched=None,
        play_count=play_count,
        poster=artwork_candidates(row.get("thumbnail")),
        fanart=artwork_candidates(row.get("fanart")),
        parent_key=f"artist:{artist_ids[0]}" if artist_ids else None,
        metadata={
            "kodi_id": album_id,
            "artist": display,
            "album_artist": artists[0] if artists else display,
            "artists": artists or [display],
            "artist_ids": artist_ids,
            "label": row.get("label") or None,
            "compilation": bool(row.get("compilation")),
            "release_type": row.get("releasetype") or None,
            "musicbrainz_album": _mbid(row.get("musicbrainzalbumid")),
            "physical": False,
            "playable": True,
        },
    )


def normalize_song(row: dict) -> Record:
    song_id = _valid_id(row, "songid", "song")
    album_id = row.get("albumid")
    track = row.get("track") if type(row.get("track")) is int and row["track"] > 0 else None
    disc = row.get("disc") if type(row.get("disc")) is int and row["disc"] > 0 else None
    duration = row.get("duration") if isinstance(row.get("duration"), (int, float)) else 0
    play_count = row.get("playcount") if type(row.get("playcount")) is int else None
    file = str(row.get("file") or "")
    fmt = audio_format(file)
    artists = _names(row.get("artist"))
    year = row.get("year") if type(row.get("year")) is int and row["year"] > 0 else None
    return Record(
        external_key=f"song:{song_id}",
        kind="song",
        title=str(row.get("title") or row.get("label") or "Untitled"),
        year=year,
        runtime_minutes=int((duration + 59) // 60) if duration > 0 else None,
        rating=positive(row.get("rating")),
        user_rating=positive(row.get("userrating")),
        genres=_names(row.get("genre")),
        formats=[fmt] if fmt else [],
        date_added=date_value(row.get("dateadded")),
        last_played=date_value(row.get("lastplayed")),
        watched=None,
        play_count=play_count,
        total_seconds=float(duration or 0),
        parent_key=f"album:{album_id}" if type(album_id) is int and album_id > 0 else None,
        season=disc,
        episode=track,
        metadata={
            "kodi_id": song_id,
            "file": file,
            "artist": str(row.get("displayartist") or " / ".join(artists)) or None,
            "artists": artists,
            "album": row.get("album") or None,
            "album_artist": next(iter(_names(row.get("albumartist"))), None),
            "kodi_album_id": album_id if type(album_id) is int else None,
            "track": track,
            "disc": disc,
            "duration_seconds": int(duration) if duration else None,
            "musicbrainz_track": _mbid(row.get("musicbrainztrackid")),
            "physical": False,
            "playable": True,
        },
    )


def _finalize(artists: list[Record], albums: list[Record], songs: list[Record]) -> list[Record]:
    """Attach track counts/durations and drop dangling parent keys."""
    known_artists = {a.external_key for a in artists}
    known_albums = {a.external_key for a in albums}
    totals: dict[str, list[float]] = defaultdict(lambda: [0, 0])
    formats: dict[str, set[str]] = defaultdict(set)
    for song in songs:
        if song.parent_key not in known_albums:
            song.parent_key = None
            continue
        totals[song.parent_key][0] += 1
        totals[song.parent_key][1] += song.total_seconds
        formats[song.parent_key].update(song.formats)
    for album in albums:
        if album.parent_key not in known_artists:
            album.parent_key = None
        count, seconds = totals.get(album.external_key, (0, 0))
        album.metadata["track_count"] = int(count) or None
        album.metadata["duration_seconds"] = int(seconds) or None
        album.formats = sorted(formats.get(album.external_key, set()))
        album.total_seconds = float(seconds)
        album.runtime_minutes = int((seconds + 59) // 60) if seconds else None
    return [*artists, *albums, *songs]


async def _paged(rpc, method: str, key: str, params: dict, page: int, progress, phase: str):
    rows, start = [], 0
    while True:
        payload = await rpc(method, {**params, "limits": {"start": start, "end": start + page}})
        if not isinstance(payload, dict) or not isinstance(payload.get("limits"), dict):
            raise ConnectorError(f"Kodi returned an incomplete {method} response.")
        items = payload.get(key, [])
        total = payload["limits"].get("total")
        if (
            not isinstance(items, list)
            or type(total) is not int
            or total < 0
            or len(items) > page
            or (total > start and not items)
        ):
            raise ConnectorError(f"Kodi returned an incomplete {method} page.")
        rows.extend(items)
        start += len(items)
        if progress:
            progress(phase, min(start, total), total)
        if start >= total:
            return rows


async def fetch_kodi_music_snapshot(player_config, progress=None) -> list[Record]:
    """Complete AudioLibrary snapshot: album artists, albums then songs."""
    async with connection(player_config, timeout=60.0) as rpc:
        artists = await _paged(
            rpc,
            "AudioLibrary.GetArtists",
            "artists",
            {"albumartistsonly": True, "properties": ARTIST_PROPERTIES},
            ARTIST_PAGE,
            progress,
            "music_artists",
        )
        albums = await _paged(
            rpc,
            "AudioLibrary.GetAlbums",
            "albums",
            {"properties": ALBUM_PROPERTIES},
            ALBUM_PAGE,
            progress,
            "music_albums",
        )
        songs = await _paged(
            rpc,
            "AudioLibrary.GetSongs",
            "songs",
            {"properties": SONG_PROPERTIES},
            SONG_PAGE,
            progress,
            "music_songs",
        )
    return _finalize(
        [normalize_artist(row) for row in artists],
        [normalize_album(row) for row in albums],
        [normalize_song(row) for row in songs],
    )


# --- MyMusic SQL fallback ----------------------------------------------------


def _sql_year(row: dict) -> int | None:
    for column in ("strOrigReleaseDate", "strReleaseDate", "iYear"):
        if match := re.match(r"(\d{4})", str(row.get(column) or "")):
            year = int(match[1])
            if year > 0:
                return year
    return None


def _first_value(row: dict, *columns: str) -> Any:
    lower = {k.casefold(): v for k, v in row.items()}
    for column in columns:
        value = lower.get(column.casefold())
        if value not in (None, ""):
            return value
    return None


def sql_artist(row: dict, art: dict[str, str]) -> Record:
    return normalize_artist(
        {
            "artistid": int(row["idArtist"]),
            "artist": _first_value(row, "strArtist"),
            "description": _first_value(row, "strBiography"),
            "genre": _first_value(row, "strGenres"),
            "musicbrainzartistid": _first_value(row, "strMusicBrainzArtistID"),
            "dateadded": _first_value(row, "dateAdded", "dateNew"),
            "thumbnail": art.get("thumb"),
            "fanart": art.get("fanart"),
        }
    )


def sql_album(row: dict, art: dict[str, str], artist_ids: list[int]) -> Record:
    return normalize_album(
        {
            "albumid": int(row["idAlbum"]),
            "title": _first_value(row, "strAlbum"),
            "artist": _first_value(row, "strArtists", "strArtistDisp", "strArtist"),
            "artistid": artist_ids,
            "displayartist": _first_value(row, "strArtistDisp", "strArtists", "strArtist"),
            "genre": _first_value(row, "strGenres", "strGenre"),
            "year": _sql_year(row),
            "rating": number(_first_value(row, "fRating", "rating")),
            "userrating": number(_first_value(row, "iUserrating", "userrating")),
            "playcount": int(number(_first_value(row, "iTimesPlayed"))),
            "lastplayed": _first_value(row, "lastPlayed", "lastplayed"),
            "dateadded": _first_value(row, "dateAdded", "dateNew"),
            "thumbnail": art.get("thumb"),
            "fanart": art.get("fanart"),
            "musicbrainzalbumid": _first_value(row, "strMusicBrainzAlbumID"),
            "musicbrainzreleasegroupid": _first_value(row, "strReleaseGroupMBID"),
            "label": _first_value(row, "strLabel"),
            "compilation": bool(number(_first_value(row, "bCompilation"))),
            "releasetype": _first_value(row, "strReleaseType"),
            "description": _first_value(row, "strReview"),
        }
    )


def sql_song(row: dict) -> Record:
    raw_track = int(number(_first_value(row, "iTrack")))
    path = str(_first_value(row, "strPath") or "") + str(_first_value(row, "strFileName") or "")
    duration = number(_first_value(row, "iDuration"))
    return normalize_song(
        {
            "songid": int(row["idSong"]),
            "title": _first_value(row, "strTitle"),
            "artist": _first_value(row, "strArtists", "strArtistDisp", "strArtist"),
            "displayartist": _first_value(row, "strArtistDisp", "strArtists", "strArtist"),
            "albumartist": _first_value(row, "strAlbumArtists", "strAlbumArtistDisp"),
            "album": _first_value(row, "strAlbum"),
            "albumid": int(number(_first_value(row, "idAlbum"))),
            # Kodi packs the disc number in the upper 16 bits of iTrack.
            "track": raw_track & 0xFFFF,
            "disc": raw_track >> 16,
            "duration": duration,
            "year": _sql_year(row),
            "genre": _first_value(row, "strGenres", "strGenre"),
            "file": path,
            "rating": number(_first_value(row, "rating", "fRating")),
            "userrating": number(_first_value(row, "userrating", "iUserrating")),
            "playcount": int(number(_first_value(row, "iTimesPlayed"))),
            "lastplayed": _first_value(row, "lastplayed", "lastPlayed"),
            "dateadded": _first_value(row, "dateAdded", "dateNew"),
            "musicbrainztrackid": _first_value(row, "strMusicBrainzTrackID"),
        }
    )


class KodiMusicConnector(KodiConnector):
    """Read-only MyMusicNN snapshot sharing the video connector's MariaDB access."""

    def __init__(self, config: dict[str, Any], progress: Callable | None = None):
        music_config = dict(config)
        music_config["database"] = config.get("music_database") or ""
        super().__init__(music_config, progress)

    @staticmethod
    def _visible_video_databases(cursor: Any) -> list[tuple[int, str]]:
        # Same automatic "highest version" selection, for MyMusic schemas.
        cursor.execute("SHOW DATABASES")
        visible = [str(next(iter(row.values()))) for row in cursor.fetchall()]
        choices = []
        for name in visible:
            if match := re.fullmatch(r"MyMusic(\d+)", name, flags=re.IGNORECASE):
                choices.append((int(match.group(1)), name))
        return sorted(choices, reverse=True)

    def fetch(self) -> list[Record]:
        return self._read(self._music_snapshot)

    def _music_snapshot(self, cursor: Any) -> list[Record]:
        cursor.execute("SHOW FULL TABLES")
        tables = {str(next(iter(r.values()))) for r in cursor.fetchall()}
        missing = {"albumview", "songview"} - tables
        if missing:
            raise ConnectorError(
                "This database is not a Kodi MyMusic schema (albumview/songview missing)."
            )
        self._report("loading")
        art: dict[tuple[str, int], dict[str, str]] = defaultdict(dict)
        if "art" in tables:
            cursor.execute(
                "SELECT media_id, media_type, type, url FROM art "
                "WHERE media_type IN ('artist','album') AND type IN ('thumb','fanart')"
            )
            for row in cursor.fetchall():
                art[(row["media_type"], int(row["media_id"]))][row["type"]] = row["url"]
        album_artists: dict[int, list[tuple[int, int]]] = defaultdict(list)
        if "album_artist" in tables:
            cursor.execute("SELECT idAlbum, idArtist, iOrder FROM album_artist")
            for row in cursor.fetchall():
                album_artists[int(row["idAlbum"])].append(
                    (int(number(row.get("iOrder"))), int(row["idArtist"]))
                )
        artist_view = "artistview" if "artistview" in tables else "artist"
        cursor.execute(f"SELECT * FROM `{artist_view}`")
        album_artist_ids = {i for links in album_artists.values() for _, i in links}
        artists = [
            sql_artist(row, art.get(("artist", int(row["idArtist"])), {}))
            for row in cursor.fetchall()
            if not album_artist_ids or int(row["idArtist"]) in album_artist_ids
        ]
        self._report("music_artists", len(artists), len(artists))
        cursor.execute("SELECT * FROM `albumview`")
        albums = []
        for row in cursor.fetchall():
            album_id = int(row["idAlbum"])
            ids = [i for _, i in sorted(album_artists.get(album_id, []))]
            albums.append(sql_album(row, art.get(("album", album_id), {}), ids))
        self._report("music_albums", len(albums), len(albums))
        cursor.execute("SELECT * FROM `songview`")
        rows = cursor.fetchall()
        songs = []
        for index, row in enumerate(rows, start=1):
            songs.append(sql_song(row))
            if index % 2000 == 0 or index == len(rows):
                self._report("music_songs", index, len(rows))
        records = _finalize(artists, albums, songs)
        for record in records:
            record.metadata["database"] = self.database
        self.snapshot_report = {
            "music_database": self.database,
            "music_artists": len(artists),
            "music_albums": len(albums),
            "music_songs": len(songs),
        }
        return records


def music_counts(records: list[Record]) -> dict[str, int]:
    counts = {"artists": 0, "albums": 0, "songs": 0}
    for record in records:
        counts[{"artist": "artists", "album": "albums", "song": "songs"}[record.kind]] += 1
    return counts


__all__ = [
    "KodiMusicConnector",
    "audio_format",
    "fetch_kodi_music_snapshot",
    "music_counts",
]
