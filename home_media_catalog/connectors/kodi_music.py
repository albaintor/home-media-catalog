"""Kodi music library: AudioLibrary JSON-RPC with a MyMusic MariaDB fallback.

The AudioLibrary namespace exists since Kodi 12. Some properties were added
later (``sortname``, ``releasetype``, ``totaldiscs``, ``bitrate``…); when a
Kodi version rejects them, the request is retried with a conservative list
instead of failing the whole snapshot.

Kodi keeps music in a separate database (``MyMusicNN``), next to the
``MyVideosNN`` schema used for movies. Its ``artistview``, ``albumview`` and
``songview`` columns vary between versions, so rows are read with ``SELECT *``
and normalised defensively.

Libraries are large (often 50k–500k songs): pages are bigger than for video
and progress is reported per collection.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from ..utils import date_value, plain_text, positive
from .base import ConnectorError
from .kodi import KodiConnector, artwork_candidates
from .music_base import (
    MusicAlbumRecord,
    MusicArtistRecord,
    MusicSnapshot,
    MusicTrackRecord,
)

ARTIST_PAGE = 1000
ALBUM_PAGE = 1000
SONG_PAGE = 2000

ARTIST_PROPERTIES = [
    "genre",
    "description",
    "musicbrainzartistid",
    "thumbnail",
    "fanart",
    "art",
    "sortname",
]
ARTIST_FALLBACK = ["genre", "description", "musicbrainzartistid", "thumbnail", "fanart"]
ALBUM_PROPERTIES = [
    "title",
    "artist",
    "artistid",
    "displayartist",
    "year",
    "genre",
    "musicbrainzalbumid",
    "musicbrainzreleasegroupid",
    "rating",
    "userrating",
    "playcount",
    "lastplayed",
    "dateadded",
    "thumbnail",
    "fanart",
    "art",
    "label",
    "releasetype",
    "compilation",
    "totaldiscs",
    "releasedate",
]
ALBUM_FALLBACK = [
    "title",
    "artist",
    "artistid",
    "displayartist",
    "year",
    "genre",
    "musicbrainzalbumid",
    "rating",
    "playcount",
    "thumbnail",
    "fanart",
    "label",
]
SONG_PROPERTIES = [
    "title",
    "artist",
    "artistid",
    "displayartist",
    "albumid",
    "album",
    "albumartist",
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
    "bitrate",
    "samplerate",
    "channels",
]
SONG_FALLBACK = [
    "title",
    "artist",
    "artistid",
    "displayartist",
    "albumid",
    "album",
    "track",
    "duration",
    "year",
    "genre",
    "file",
    "rating",
    "playcount",
]

AUDIO_EXTENSIONS = {
    "mp3",
    "m4a",
    "aac",
    "flac",
    "alac",
    "ogg",
    "oga",
    "opus",
    "wav",
    "aiff",
    "aif",
    "aifc",
    "caf",
    "wma",
    "ape",
    "wv",
    "dsf",
    "dff",
    "mka",
    "mp2",
    "ac3",
    "eac3",
    "dts",
    "amr",
    "mpc",
    "tta",
    "tak",
}


def _art(row: dict[str, Any], kind: str) -> list[str]:
    art = row.get("art") if isinstance(row.get("art"), dict) else {}
    value = art.get(kind) or row.get("thumbnail" if kind == "thumb" else "fanart")
    return artwork_candidates(value)


def _names(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v or "").strip()]
    if isinstance(value, str) and value.strip():
        return [part.strip() for part in value.split(" / ") if part.strip()]
    return []


def _year(*values: Any) -> int | None:
    for value in values:
        if type(value) is int and 0 < value < 10000:
            return value
        match = re.match(r"^(\d{4})", str(value or ""))
        if match and int(match.group(1)) > 0:
            return int(match.group(1))
    return None


def _codec(path: str | None) -> str | None:
    if not path:
        return None
    match = re.search(r"\.([A-Za-z0-9]{2,5})$", path.split("?", 1)[0])
    if not match:
        return None
    extension = match.group(1).lower()
    return extension.upper() if extension in AUDIO_EXTENSIONS else None


def _rating(value: Any) -> float | None:
    rating = positive(value)
    return round(rating, 2) if rating is not None else None


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


# ----------------------------------------------------------------- JSON-RPC


def normalize_artist(row: dict[str, Any]) -> MusicArtistRecord:
    artist_id = row.get("artistid")
    if type(artist_id) is not int or artist_id < 0:
        raise ConnectorError("Kodi returned an artist without a valid artistid.")
    mbids = row.get("musicbrainzartistid")
    mbid = (mbids[0] if isinstance(mbids, list) and mbids else mbids) or None
    return MusicArtistRecord(
        external_key=f"artist:{artist_id}",
        name=str(row.get("artist") or row.get("label") or "Unknown artist"),
        sort_name=row.get("sortname") or None,
        mbid=str(mbid) if mbid else None,
        genres=_names(row.get("genre")),
        biography=plain_text(row.get("description")) or None,
        thumb=_art(row, "thumb"),
        fanart=_art(row, "fanart"),
        metadata={"kodi_id": artist_id},
    )


def normalize_album(row: dict[str, Any]) -> MusicAlbumRecord:
    album_id = row.get("albumid")
    if type(album_id) is not int or album_id < 0:
        raise ConnectorError("Kodi returned an album without a valid albumid.")
    artist_ids = row.get("artistid") if isinstance(row.get("artistid"), list) else []
    release_type = str(row.get("releasetype") or "").strip().lower() or None
    return MusicAlbumRecord(
        external_key=f"album:{album_id}",
        title=str(row.get("title") or row.get("label") or "Untitled"),
        artists=_names(row.get("artist")),
        artist_keys=[f"artist:{a}" for a in artist_ids if type(a) is int],
        display_artist=row.get("displayartist") or None,
        year=_year(row.get("year"), row.get("releasedate")),
        release_date=row.get("releasedate") or None,
        genres=_names(row.get("genre")),
        mbid=row.get("musicbrainzalbumid") or None,
        release_group_mbid=row.get("musicbrainzreleasegroupid") or None,
        release_type=release_type,
        compilation=bool(row.get("compilation")),
        label=row.get("label") or None,
        rating=_rating(row.get("rating")),
        user_rating=_rating(row.get("userrating")),
        play_count=_count(row.get("playcount")),
        last_played=date_value(row.get("lastplayed")),
        date_added=date_value(row.get("dateadded")),
        thumb=_art(row, "thumb"),
        fanart=_art(row, "fanart"),
        format="Digital",
        disc_count=row.get("totaldiscs") if type(row.get("totaldiscs")) is int else None,
        metadata={"kodi_id": album_id},
    )


def normalize_song(row: dict[str, Any]) -> MusicTrackRecord:
    song_id = row.get("songid")
    if type(song_id) is not int or song_id < 0:
        raise ConnectorError("Kodi returned a song without a valid songid.")
    album_id = row.get("albumid")
    file = row.get("file") or None
    duration = row.get("duration")
    disc = row.get("disc") if type(row.get("disc")) is int and row["disc"] > 0 else None
    track = row.get("track") if type(row.get("track")) is int and row["track"] > 0 else None
    bitrate = row.get("bitrate") if type(row.get("bitrate")) is int and row["bitrate"] > 0 else None
    sample_rate = (
        row.get("samplerate")
        if type(row.get("samplerate")) is int and row["samplerate"] > 0
        else None
    )
    channels = (
        row.get("channels") if type(row.get("channels")) is int and row["channels"] > 0 else None
    )
    return MusicTrackRecord(
        external_key=f"song:{song_id}",
        title=str(row.get("title") or row.get("label") or "Untitled"),
        album_key=f"album:{album_id}" if type(album_id) is int and album_id > 0 else None,
        artists=_names(row.get("artist")),
        display_artist=row.get("displayartist") or None,
        track=track,
        disc=disc,
        duration_seconds=float(duration)
        if isinstance(duration, (int, float)) and duration > 0
        else None,
        year=_year(row.get("year")),
        genres=_names(row.get("genre")),
        file=file,
        playable=bool(file),
        mbid=row.get("musicbrainztrackid") or None,
        rating=_rating(row.get("rating")),
        user_rating=_rating(row.get("userrating")),
        play_count=_count(row.get("playcount")),
        last_played=date_value(row.get("lastplayed")),
        date_added=date_value(row.get("dateadded")),
        codec=_codec(file),
        bitrate=bitrate,
        sample_rate=sample_rate,
        channels=channels,
        metadata={"kodi_id": song_id, "album": row.get("album") or ""},
    )


Progress = Callable[[str, int, int], None]
RPC = Callable[[str, dict[str, Any]], Awaitable[Any]]


async def _paged(
    rpc: RPC,
    method: str,
    key: str,
    properties: list[str],
    fallback: list[str],
    page: int,
    phase: str,
    progress: Progress | None,
    extra: dict | None = None,
):
    rows: list[dict[str, Any]] = []
    start = 0
    props = properties
    while True:
        params = {"properties": props, "limits": {"start": start, "end": start + page}}
        params.update(extra or {})
        try:
            payload = await rpc(method, params)
        except ConnectorError as exc:
            # Older Kodi versions reject unknown properties (-32602); restart
            # this collection once with the conservative property list.
            if props is fallback or start or "-32602" not in str(exc):
                raise
            props = fallback
            continue
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
        rows.extend(item for item in items if isinstance(item, dict))
        start += len(items)
        if progress:
            progress(phase, min(start, total), total)
        if start >= total:
            return rows


async def fetch_kodi_music_snapshot(
    player_config: dict, progress: Progress | None = None, *, connection_factory=None
) -> MusicSnapshot:
    """Complete AudioLibrary snapshot; any page failure aborts before publication."""
    if connection_factory is None:
        from ..players import connection as connection_factory
    async with connection_factory(player_config, timeout=60.0) as rpc:
        artists = await _paged(
            rpc,
            "AudioLibrary.GetArtists",
            "artists",
            ARTIST_PROPERTIES,
            ARTIST_FALLBACK,
            ARTIST_PAGE,
            "music_artists",
            progress,
            {"albumartistsonly": False},
        )
        albums = await _paged(
            rpc,
            "AudioLibrary.GetAlbums",
            "albums",
            ALBUM_PROPERTIES,
            ALBUM_FALLBACK,
            ALBUM_PAGE,
            "music_albums",
            progress,
        )
        songs = await _paged(
            rpc,
            "AudioLibrary.GetSongs",
            "songs",
            SONG_PROPERTIES,
            SONG_FALLBACK,
            SONG_PAGE,
            "music_songs",
            progress,
        )
    return MusicSnapshot(
        artists=[normalize_artist(row) for row in artists],
        albums=[normalize_album(row) for row in albums],
        tracks=[normalize_song(row) for row in songs],
        report={"music_import_method": "jsonrpc"},
    )


# ------------------------------------------------------------------ MariaDB


def _col(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return None


def normalize_sql_artist(row: dict[str, Any], art: dict[str, str]) -> MusicArtistRecord:
    artist_id = int(row["idArtist"])
    return MusicArtistRecord(
        external_key=f"artist:{artist_id}",
        name=str(_col(row, "strArtist") or "Unknown artist"),
        sort_name=_col(row, "strSortName"),
        mbid=_col(row, "strMusicBrainzArtistID"),
        genres=_names(_col(row, "strGenres")),
        biography=plain_text(_col(row, "strBiography")) or None,
        thumb=artwork_candidates(art.get("thumb") or _col(row, "strImage")),
        fanart=artwork_candidates(art.get("fanart") or _col(row, "strFanart")),
        metadata={"kodi_id": artist_id},
    )


def normalize_sql_album(
    row: dict[str, Any], art: dict[str, str], artist_ids: list[int]
) -> MusicAlbumRecord:
    album_id = int(row["idAlbum"])
    display = _col(row, "strArtistDisp", "strArtists")
    return MusicAlbumRecord(
        external_key=f"album:{album_id}",
        title=str(_col(row, "strAlbum") or "Untitled"),
        artists=_names(display),
        artist_keys=[f"artist:{a}" for a in artist_ids],
        display_artist=display,
        year=_year(_col(row, "iYear"), _col(row, "strReleaseDate", "strOrigReleaseDate")),
        release_date=_col(row, "strReleaseDate"),
        genres=_names(_col(row, "strGenres")),
        mbid=_col(row, "strMusicBrainzAlbumID"),
        release_group_mbid=_col(row, "strReleaseGroupMBID"),
        release_type=str(_col(row, "strReleaseType") or "").lower() or None,
        compilation=bool(_col(row, "bCompilation")),
        label=_col(row, "strLabel"),
        rating=_rating(_col(row, "fRating", "rating")),
        user_rating=_rating(_col(row, "iUserrating", "userrating")),
        play_count=int(_col(row, "iTimesPlayed") or 0) or None,
        last_played=date_value(_col(row, "lastPlayed", "lastplayed")),
        date_added=date_value(_col(row, "dateAdded", "dateNew")),
        thumb=artwork_candidates(art.get("thumb") or _col(row, "strImage")),
        fanart=artwork_candidates(art.get("fanart")),
        format="Digital",
        disc_count=int(_col(row, "iDiscTotal") or 0) or None,
        metadata={"kodi_id": album_id},
    )


def normalize_sql_song(row: dict[str, Any]) -> MusicTrackRecord:
    song_id = int(row["idSong"])
    encoded = int(_col(row, "iTrack") or 0)
    disc, track = (encoded >> 16) or None, (encoded & 0xFFFF) or None
    path = str(_col(row, "strPath") or "") + str(_col(row, "strFileName") or "")
    duration = _col(row, "iDuration")
    album_id = _col(row, "idAlbum")
    display = _col(row, "strArtistDisp", "strArtists")
    bitrate = int(_col(row, "iBitRate") or 0) or None
    return MusicTrackRecord(
        external_key=f"song:{song_id}",
        title=str(_col(row, "strTitle") or "Untitled"),
        album_key=f"album:{int(album_id)}" if album_id else None,
        artists=_names(display),
        display_artist=display,
        track=track,
        disc=disc,
        duration_seconds=float(duration) if duration else None,
        year=_year(_col(row, "iYear"), _col(row, "strReleaseDate")),
        genres=_names(_col(row, "strGenres")),
        file=path or None,
        playable=bool(path),
        mbid=_col(row, "strMusicBrainzTrackID"),
        rating=_rating(_col(row, "rating")),
        user_rating=_rating(_col(row, "userrating")),
        play_count=int(_col(row, "iTimesPlayed") or 0) or None,
        last_played=date_value(_col(row, "lastplayed", "lastPlayed")),
        date_added=date_value(_col(row, "dateAdded", "dateNew")),
        codec=_codec(path),
        # Kodi stores bit/s in MyMusic; JSON-RPC reports kbit/s.
        bitrate=(bitrate // 1000 if bitrate and bitrate > 10000 else bitrate),
        sample_rate=int(_col(row, "iSampleRate") or 0) or None,
        channels=int(_col(row, "iChannels") or 0) or None,
        metadata={"kodi_id": song_id, "album": _col(row, "strAlbum") or ""},
    )


class KodiMusicConnector(KodiConnector):
    """Read-only snapshot of Kodi's MyMusic schema (same credentials as MyVideos)."""

    def __init__(self, config: dict[str, Any], progress: Progress | None = None):
        music_config = dict(config)
        music_config["database"] = config.get("music_database") or ""
        super().__init__(music_config, progress)

    @staticmethod
    def _visible_video_databases(cursor: Any) -> list[tuple[int, str]]:
        cursor.execute("SHOW DATABASES")
        visible = [str(next(iter(row.values()))) for row in cursor.fetchall()]
        choices = []
        for name in visible:
            match = re.fullmatch(r"MyMusic(\d+)", name, flags=re.IGNORECASE)
            if match:
                choices.append((int(match.group(1)), name))
        return sorted(choices, key=lambda item: (item[0], item[1]), reverse=True)

    def fetch_music(self) -> MusicSnapshot:
        try:
            return self._read(self._music_snapshot)
        except ConnectorError as exc:
            if "MyVideos" in str(exc):
                raise ConnectorError(
                    "No accessible MyMusic database. Specify its name or grant SELECT permission."
                ) from exc
            raise

    def _rows(self, cursor: Any, sql: str, phase: str, total: int) -> list[dict[str, Any]]:
        cursor.execute(sql)
        rows: list[dict[str, Any]] = []
        while batch := cursor.fetchmany(5000):
            rows.extend(batch)
            self._report(phase, len(rows), total)
        return rows

    def _music_snapshot(self, cursor: Any) -> MusicSnapshot:
        cursor.execute("SHOW FULL TABLES")
        tables = {str(next(iter(row.values()))).lower() for row in cursor.fetchall()}
        missing = {"artistview", "albumview", "songview"} - tables
        if missing:
            raise ConnectorError(
                "The MyMusic database must expose artistview, albumview and songview "
                f"(missing: {', '.join(sorted(missing))})."
            )

        def count(table: str) -> int:
            cursor.execute(f"SELECT COUNT(*) AS n FROM {table}")
            return int(cursor.fetchone()["n"])

        art: dict[tuple[str, int], dict[str, str]] = defaultdict(dict)
        if "art" in tables:
            cursor.execute(
                "SELECT media_id,media_type,type,url FROM art "
                "WHERE media_type IN ('artist','album') AND type IN ('thumb','fanart')"
            )
            for row in cursor.fetchall():
                art[(row["media_type"], int(row["media_id"]))][row["type"]] = row["url"]
        album_artists: dict[int, list[int]] = defaultdict(list)
        if "album_artist" in tables:
            cursor.execute("SELECT idAlbum,idArtist FROM album_artist ORDER BY idAlbum,iOrder")
            for row in cursor.fetchall():
                album_artists[int(row["idAlbum"])].append(int(row["idArtist"]))

        artists_total = count("artistview")
        artists = [
            normalize_sql_artist(row, art[("artist", int(row["idArtist"]))])
            for row in self._rows(
                cursor, "SELECT * FROM artistview", "music_artists", artists_total
            )
        ]
        albums_total = count("albumview")
        albums = [
            normalize_sql_album(
                row, art[("album", int(row["idAlbum"]))], album_artists.get(int(row["idAlbum"]), [])
            )
            for row in self._rows(cursor, "SELECT * FROM albumview", "music_albums", albums_total)
        ]
        songs_total = count("songview")
        tracks = [
            normalize_sql_song(row)
            for row in self._rows(cursor, "SELECT * FROM songview", "music_songs", songs_total)
        ]
        self.snapshot_report = {
            "music_database": self.database,
            "music_import_method": "mariadb",
        }
        return MusicSnapshot(
            artists=artists, albums=albums, tracks=tracks, report=dict(self.snapshot_report)
        )
