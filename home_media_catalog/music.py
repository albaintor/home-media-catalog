"""Music catalogue: artists, albums (with per-source editions) and tracks.

Music lives beside the video ``works`` tables in the same SQLite database but
never shares rows with them. Volumes differ by orders of magnitude (50k–500k
tracks), so publication uses set-based statements, integer row ids for FTS
and keyset pagination for native-client synchronisation.

Matching rules
--------------
* An **artist** is identified by its MusicBrainz ID, otherwise by its folded
  name. Artists are global: Kodi and a vinyl list naming "Radiohead" share it.
* An **album** gathers source items ("editions"): the FLAC rip from Kodi, the
  CD and the vinyl from a physical collection. They merge on MusicBrainz
  release-group ID, otherwise on folded album-artist + title with edition
  qualifiers ("Remastered 2011", "Deluxe Edition") removed.
* **Tracks** are never merged: each source keeps its own track list. A track
  is playable when its source can deliver it (a Kodi file today, a streaming
  provider later).

Album and artist IDs are stable across re-synchronisations as long as one
source still contains them, so client lists and caches stay valid.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from .connectors.music_base import (
    MUSIC_FORMATS,
    MusicAlbumRecord,
    MusicArtistRecord,
    MusicSnapshot,
    MusicTrackRecord,
)
from .utils import fold, now

MUSIC_SCHEMA = """
CREATE TABLE IF NOT EXISTS music_artists (
 rid INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE,
 name TEXT NOT NULL, sort_name TEXT NOT NULL DEFAULT '', name_key TEXT NOT NULL,
 mbid TEXT, genres TEXT NOT NULL DEFAULT '[]', biography TEXT,
 thumb TEXT NOT NULL DEFAULT '[]', fanart TEXT NOT NULL DEFAULT '[]',
 album_count INTEGER NOT NULL DEFAULT 0, track_count INTEGER NOT NULL DEFAULT 0,
 updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS music_artists_name_key ON music_artists(name_key);
CREATE INDEX IF NOT EXISTS music_artists_mbid ON music_artists(mbid);
CREATE INDEX IF NOT EXISTS music_artists_sort ON music_artists(sort_name,id);
CREATE TABLE IF NOT EXISTS music_artist_sources (
 artist_id TEXT NOT NULL REFERENCES music_artists(id) ON DELETE CASCADE,
 source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
 external_key TEXT NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(source_id,external_key)
);
CREATE INDEX IF NOT EXISTS music_artist_sources_artist ON music_artist_sources(artist_id);
CREATE TABLE IF NOT EXISTS music_albums (
 rid INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE,
 match_key TEXT NOT NULL, release_group_mbid TEXT,
 title TEXT NOT NULL, sort_title TEXT NOT NULL DEFAULT '',
 artist_id TEXT REFERENCES music_artists(id) ON DELETE SET NULL,
 artist_name TEXT NOT NULL DEFAULT '', artist_sort TEXT NOT NULL DEFAULT '',
 year INTEGER, release_type TEXT, compilation INTEGER NOT NULL DEFAULT 0,
 label TEXT, genres TEXT NOT NULL DEFAULT '[]',
 rating REAL, user_rating REAL, play_count INTEGER, last_played TEXT, date_added TEXT,
 thumb TEXT NOT NULL DEFAULT '[]', fanart TEXT NOT NULL DEFAULT '[]',
 formats TEXT NOT NULL DEFAULT '[]', source_types TEXT NOT NULL DEFAULT '[]',
 track_count INTEGER NOT NULL DEFAULT 0, duration_seconds REAL NOT NULL DEFAULT 0,
 playable INTEGER NOT NULL DEFAULT 0, owned_physical INTEGER NOT NULL DEFAULT 0,
 updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS music_albums_match ON music_albums(match_key);
CREATE INDEX IF NOT EXISTS music_albums_rg ON music_albums(release_group_mbid);
CREATE INDEX IF NOT EXISTS music_albums_artist ON music_albums(artist_id,year,sort_title);
CREATE INDEX IF NOT EXISTS music_albums_sort ON music_albums(artist_sort,year,sort_title,id);
CREATE INDEX IF NOT EXISTS music_albums_title ON music_albums(sort_title,id);
CREATE INDEX IF NOT EXISTS music_albums_added ON music_albums(date_added,id);
CREATE TABLE IF NOT EXISTS music_album_artists (
 album_id TEXT NOT NULL REFERENCES music_albums(id) ON DELETE CASCADE,
 artist_id TEXT NOT NULL REFERENCES music_artists(id) ON DELETE CASCADE,
 position INTEGER NOT NULL, PRIMARY KEY(album_id,artist_id)
);
CREATE INDEX IF NOT EXISTS music_album_artists_artist ON music_album_artists(artist_id,album_id);
CREATE TABLE IF NOT EXISTS music_album_editions (
 id TEXT PRIMARY KEY,
 album_id TEXT NOT NULL REFERENCES music_albums(id) ON DELETE CASCADE,
 source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
 external_key TEXT NOT NULL, format TEXT NOT NULL, payload TEXT NOT NULL,
 UNIQUE(source_id,external_key)
);
CREATE INDEX IF NOT EXISTS music_album_editions_album ON music_album_editions(album_id);
CREATE TABLE IF NOT EXISTS music_tracks (
 rid INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE,
 source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
 external_key TEXT NOT NULL,
 album_id TEXT REFERENCES music_albums(id) ON DELETE SET NULL,
 edition_id TEXT REFERENCES music_album_editions(id) ON DELETE SET NULL,
 artist_id TEXT REFERENCES music_artists(id) ON DELETE SET NULL,
 title TEXT NOT NULL, sort_title TEXT NOT NULL DEFAULT '',
 artist_name TEXT NOT NULL DEFAULT '', album_title TEXT NOT NULL DEFAULT '',
 disc INTEGER, track INTEGER, position TEXT, duration_seconds REAL,
 year INTEGER, genres TEXT NOT NULL DEFAULT '[]', file TEXT,
 playable INTEGER NOT NULL DEFAULT 0, format TEXT NOT NULL DEFAULT 'Digital',
 mbid TEXT, rating REAL, user_rating REAL, play_count INTEGER,
 last_played TEXT, date_added TEXT, audio TEXT NOT NULL DEFAULT '{}',
 metadata TEXT NOT NULL DEFAULT '{}',
 UNIQUE(source_id,external_key)
);
CREATE INDEX IF NOT EXISTS music_tracks_album ON music_tracks(album_id,edition_id,disc,track,position);
CREATE INDEX IF NOT EXISTS music_tracks_artist ON music_tracks(artist_id);
CREATE INDEX IF NOT EXISTS music_tracks_sort ON music_tracks(sort_title,rid);
CREATE INDEX IF NOT EXISTS music_tracks_added ON music_tracks(date_added,rid);
CREATE INDEX IF NOT EXISTS music_tracks_played ON music_tracks(last_played,rid);
CREATE VIRTUAL TABLE IF NOT EXISTS music_artists_fts USING fts5(text, tokenize='unicode61 remove_diacritics 2');
CREATE VIRTUAL TABLE IF NOT EXISTS music_albums_fts USING fts5(text, tokenize='unicode61 remove_diacritics 2');
CREATE VIRTUAL TABLE IF NOT EXISTS music_tracks_fts USING fts5(text, tokenize='unicode61 remove_diacritics 2');
"""

# Video and music sources share the ``sources`` table.
KODI_SOURCE = "kodi"
MUSIC_COLLECTION_TYPES = ("music_csv", "discogs", "clz_music")
MUSIC_SOURCE_TYPES = (KODI_SOURCE, *MUSIC_COLLECTION_TYPES)
PHYSICAL_FORMATS = {
    "CD",
    "SACD",
    "Vinyl",
    "Cassette",
    "MiniDisc",
    "DVD-Audio",
    "Blu-ray Audio",
    "Reel-to-reel",
    "Other",
}
# Preferred metadata provider when several sources describe the same album.
SOURCE_PRIORITY = {"kodi": 0, "clz_music": 1, "discogs": 2, "music_csv": 3}
MAX_PAGE = 500
MAX_EXPORT_PAGE = 5000

_EDITION_QUALIFIER = re.compile(
    r"\s*[\(\[][^\)\]]*(remaster|deluxe|edition|expanded|anniversary|bonus|"
    r"mono|stereo|reissue|version|édition|réédition|special|collector)[^\)\]]*[\)\]]",
    re.IGNORECASE,
)
_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)
_ARTICLE = re.compile(r"^(the|les|le|la|l')\s+")


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def name_key(name: str | None) -> str:
    """Fold an artist name: case, accents, '&' vs 'and', leading 'The'."""
    value = fold(str(name or "").strip()).replace("&", " and ").replace("+", " and ")
    value = _ARTICLE.sub("", value)
    return _NON_WORD.sub(" ", value).strip()


def title_key(title: str | None) -> str:
    value = _EDITION_QUALIFIER.sub("", str(title or ""))
    return _NON_WORD.sub(" ", fold(value)).strip()


def sort_name(name: str | None, explicit: str | None = None) -> str:
    if explicit and explicit.strip():
        return fold(explicit.strip())
    value = fold(str(name or "").strip())
    match = re.match(r"^(the|les|le|la)\s+(.+)$", value)
    return f"{match.group(2)}, {match.group(1)}" if match else value


def album_match_key(artist: str | None, title: str | None, compilation: bool = False) -> str:
    artist_part = "various artists" if compilation else name_key(artist)
    return f"{artist_part}|{title_key(title)}"


def _first(values: list[str] | None) -> str | None:
    return next((v for v in values or [] if v), None)


def _int(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _uniq(values) -> list:
    return list(dict.fromkeys(v for v in values if v not in (None, "")))


class MusicCatalogue:
    """Music operations on the shared catalogue database."""

    def __init__(self, store):
        self.store = store

    # ------------------------------------------------------------------ setup
    @staticmethod
    def initialize(conn: sqlite3.Connection) -> None:
        conn.executescript(MUSIC_SCHEMA)

    def revision(self) -> int:
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT value FROM catalogue_meta WHERE key='music_revision'"
            ).fetchone()
            return int(row[0]) if row else 0

    @staticmethod
    def _bump_revision(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT value FROM catalogue_meta WHERE key='music_revision'").fetchone()
        value = (int(row[0]) if row else 0) + 1
        conn.execute(
            "INSERT INTO catalogue_meta(key,value) VALUES('music_revision',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(value),),
        )
        return value

    # ------------------------------------------------------------ publication
    def apply_snapshot(self, source_id: str, snapshot: MusicSnapshot) -> dict[str, Any]:
        """Atomically replace one source's music contribution."""
        with self.store.connect() as conn:
            source = conn.execute("SELECT type FROM sources WHERE id=?", (source_id,)).fetchone()
            if source is None:
                raise ValueError("Unknown source.")
            source_type = source["type"]
            before = conn.execute(
                "SELECT COUNT(*) FROM music_album_editions WHERE source_id=?", (source_id,)
            ).fetchone()[0]
            before_tracks = conn.execute(
                "SELECT COUNT(*) FROM music_tracks WHERE source_id=?", (source_id,)
            ).fetchone()[0]
            touched_albums = {
                row[0]
                for row in conn.execute(
                    "SELECT album_id FROM music_album_editions WHERE source_id=?", (source_id,)
                )
            }
            touched_artists = {
                row[0]
                for row in conn.execute(
                    "SELECT artist_id FROM music_artist_sources WHERE source_id=? "
                    "UNION SELECT artist_id FROM music_tracks WHERE source_id=? "
                    "AND artist_id IS NOT NULL",
                    (source_id, source_id),
                )
            }
            touched_artists |= {
                row[0]
                for row in conn.execute(
                    "SELECT artist_id FROM music_album_artists WHERE album_id IN "
                    "(SELECT album_id FROM music_album_editions WHERE source_id=?)",
                    (source_id,),
                )
            }
            # Remove the previous contribution. Albums and artists survive
            # until aggregation decides whether another source still owns them.
            conn.execute(
                "DELETE FROM music_tracks_fts WHERE rowid IN "
                "(SELECT rid FROM music_tracks WHERE source_id=?)",
                (source_id,),
            )
            conn.execute("DELETE FROM music_tracks WHERE source_id=?", (source_id,))
            conn.execute("DELETE FROM music_album_editions WHERE source_id=?", (source_id,))
            conn.execute("DELETE FROM music_artist_sources WHERE source_id=?", (source_id,))

            resolver = _ArtistResolver(conn)
            artist_by_key: dict[str, str] = {}
            for artist in snapshot.artists:
                artist_id = resolver.resolve(artist.name, artist.mbid, artist)
                if artist_id is None:
                    continue
                artist_by_key[artist.external_key] = artist_id
                touched_artists.add(artist_id)
                conn.execute(
                    "INSERT OR REPLACE INTO music_artist_sources"
                    "(artist_id,source_id,external_key,payload) VALUES(?,?,?,?)",
                    (artist_id, source_id, artist.external_key, encode(_artist_payload(artist))),
                )

            album_by_key: dict[str, tuple[str, str, str]] = {}
            for album in snapshot.albums:
                names = album.artists or ([album.display_artist] if album.display_artist else [])
                artist_ids = [
                    artist_by_key[key] for key in album.artist_keys if key in artist_by_key
                ] or [resolver.resolve(name, None) for name in names]
                artist_ids = _uniq(a for a in artist_ids if a)
                display = album.display_artist or " / ".join(names)
                album_id = self._resolve_album(conn, album, display)
                touched_albums.add(album_id)
                for position, artist_id in enumerate(artist_ids):
                    touched_artists.add(artist_id)
                    conn.execute(
                        "INSERT OR IGNORE INTO music_album_artists(album_id,artist_id,position)"
                        " VALUES(?,?,?)",
                        (album_id, artist_id, position),
                    )
                edition_id = str(
                    uuid5(NAMESPACE_URL, f"music-edition:{source_id}:{album.external_key}")
                )
                conn.execute(
                    "INSERT OR REPLACE INTO music_album_editions"
                    "(id,album_id,source_id,external_key,format,payload) VALUES(?,?,?,?,?,?)",
                    (
                        edition_id,
                        album_id,
                        source_id,
                        album.external_key,
                        album.format or "Other",
                        encode(_album_payload(album, display, source_type)),
                    ),
                )
                album_by_key[album.external_key] = (album_id, edition_id, album.format or "Other")

            stamp = now()
            rows = []
            fts_text: dict[str, str] = {}
            for track in snapshot.tracks:
                album_id, edition_id, carrier = album_by_key.get(
                    track.album_key or "", (None, None, "Digital")
                )
                artist_name = track.display_artist or " / ".join(track.artists)
                artist_id = resolver.resolve(_first(track.artists) or artist_name, None)
                if artist_id:
                    touched_artists.add(artist_id)
                track_id = str(
                    uuid5(NAMESPACE_URL, f"music-track:{source_id}:{track.external_key}")
                )
                album_title = track.metadata.get("album") or ""
                audio = {
                    key: value
                    for key, value in (
                        ("codec", track.codec),
                        ("bitrate", track.bitrate),
                        ("sample_rate", track.sample_rate),
                        ("channels", track.channels),
                        ("bits_per_sample", track.bits_per_sample),
                        ("replay_gain", track.replay_gain),
                    )
                    if value not in (None, "", {})
                }
                rows.append(
                    (
                        track_id,
                        source_id,
                        track.external_key,
                        album_id,
                        edition_id,
                        artist_id,
                        track.title or "Untitled",
                        fold(track.title or ""),
                        artist_name,
                        album_title,
                        track.disc,
                        track.track,
                        track.position,
                        track.duration_seconds,
                        track.year,
                        encode(track.genres),
                        track.file,
                        1 if track.playable else 0,
                        carrier if not track.playable else ("Digital" if track.file else carrier),
                        track.mbid,
                        track.rating,
                        track.user_rating,
                        track.play_count,
                        track.last_played,
                        track.date_added,
                        encode(audio),
                        encode(track.metadata),
                    )
                )
                fts_text[track_id] = fold(
                    " ".join(filter(None, (track.title, artist_name, album_title)))
                )
            conn.executemany(
                "INSERT OR REPLACE INTO music_tracks(id,source_id,external_key,album_id,edition_id,"
                "artist_id,title,sort_title,artist_name,album_title,disc,track,position,"
                "duration_seconds,year,genres,file,playable,format,mbid,rating,user_rating,"
                "play_count,last_played,date_added,audio,metadata) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            conn.executemany(
                "INSERT INTO music_tracks_fts(rowid,text) "
                "SELECT rid,? FROM music_tracks WHERE id=?",
                ((text, track_id) for track_id, text in fts_text.items()),
            )
            # Tracks without album title in their payload inherit it now.
            conn.execute(
                "UPDATE music_tracks SET album_title=(SELECT title FROM music_albums a "
                "WHERE a.id=music_tracks.album_id) WHERE source_id=? AND album_title='' "
                "AND album_id IS NOT NULL",
                (source_id,),
            )

            for album_id in touched_albums:
                self._rebuild_album(conn, album_id, stamp)
            removed_albums = self._prune_albums(conn)
            touched_artists |= (
                {
                    row[0]
                    for row in conn.execute(
                        "SELECT DISTINCT artist_id FROM music_album_artists WHERE album_id IN "
                        f"({','.join('?' for _ in touched_albums)})",
                        tuple(touched_albums),
                    )
                }
                if touched_albums
                else set()
            )
            for artist_id in touched_artists:
                self._rebuild_artist(conn, artist_id, stamp)
            removed_artists = self._prune_artists(conn)
            revision = self._bump_revision(conn)
            after = len(snapshot.albums)
            return {
                "music_albums": after,
                "music_tracks": len(snapshot.tracks),
                "music_artists": len(snapshot.artists),
                "music_albums_previous": before,
                "music_tracks_previous": before_tracks,
                "music_albums_removed": removed_albums,
                "music_artists_removed": removed_artists,
                "music_revision": revision,
                **snapshot.report,
            }

    def remove_source(self, conn: sqlite3.Connection, source_id: str) -> None:
        """Called inside Store.delete_source before the source row is deleted."""
        albums = [
            r[0]
            for r in conn.execute(
                "SELECT album_id FROM music_album_editions WHERE source_id=?", (source_id,)
            )
        ]
        artists = [
            r[0]
            for r in conn.execute(
                "SELECT artist_id FROM music_artist_sources WHERE source_id=? UNION "
                "SELECT artist_id FROM music_tracks WHERE source_id=? AND artist_id IS NOT NULL",
                (source_id, source_id),
            )
        ]
        if not albums and not artists:
            return
        conn.execute(
            "DELETE FROM music_tracks_fts WHERE rowid IN "
            "(SELECT rid FROM music_tracks WHERE source_id=?)",
            (source_id,),
        )
        conn.execute("DELETE FROM music_tracks WHERE source_id=?", (source_id,))
        conn.execute("DELETE FROM music_album_editions WHERE source_id=?", (source_id,))
        conn.execute("DELETE FROM music_artist_sources WHERE source_id=?", (source_id,))
        stamp = now()
        for album_id in set(albums):
            artists.extend(
                r[0]
                for r in conn.execute(
                    "SELECT artist_id FROM music_album_artists WHERE album_id=?", (album_id,)
                )
            )
            self._rebuild_album(conn, album_id, stamp)
        self._prune_albums(conn)
        for artist_id in set(artists):
            self._rebuild_artist(conn, artist_id, stamp)
        self._prune_artists(conn)
        self._bump_revision(conn)

    @staticmethod
    def _resolve_album(conn: sqlite3.Connection, album: MusicAlbumRecord, display: str) -> str:
        primary = _first(album.artists) or display
        key = album_match_key(primary, album.title, album.compilation)
        row = None
        if album.release_group_mbid:
            row = conn.execute(
                "SELECT id FROM music_albums WHERE release_group_mbid=? LIMIT 1",
                (album.release_group_mbid,),
            ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT id FROM music_albums WHERE match_key=? ORDER BY rid LIMIT 1", (key,)
            ).fetchone()
            # Display-artist strings ("A feat. B") may differ from the first
            # credited artist between sources; try the full display too.
            if row is None and display and display != primary:
                row = conn.execute(
                    "SELECT id FROM music_albums WHERE match_key=? ORDER BY rid LIMIT 1",
                    (album_match_key(display, album.title, album.compilation),),
                ).fetchone()
        if row is not None:
            if album.release_group_mbid:
                conn.execute(
                    "UPDATE music_albums SET release_group_mbid=COALESCE(release_group_mbid,?) "
                    "WHERE id=?",
                    (album.release_group_mbid, row["id"]),
                )
            return row["id"]
        album_id = str(uuid4())
        conn.execute(
            "INSERT INTO music_albums(id,match_key,release_group_mbid,title,sort_title,"
            "artist_name,artist_sort,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                album_id,
                key,
                album.release_group_mbid,
                album.title or "Untitled",
                fold(album.title or ""),
                display,
                sort_name(primary),
                now(),
            ),
        )
        return album_id

    @staticmethod
    def _rebuild_album(conn: sqlite3.Connection, album_id: str, stamp: str) -> None:
        editions = conn.execute(
            "SELECT e.id,e.format,e.payload,s.type FROM music_album_editions e "
            "JOIN sources s ON s.id=e.source_id WHERE e.album_id=?",
            (album_id,),
        ).fetchall()
        if not editions:
            return
        payloads = sorted(
            ((json.loads(row["payload"]), row["type"], row["format"]) for row in editions),
            key=lambda item: (SOURCE_PRIORITY.get(item[1], 9), item[0].get("title") or ""),
        )

        def choose(field):
            for payload, _source, _format in payloads:
                value = payload.get(field)
                if value not in (None, "", []):
                    return value
            return None

        def combined(field):
            values = []
            for payload, _source, _format in payloads:
                values.extend(payload.get(field) or [])
            return _uniq(values)

        stats = conn.execute(
            "SELECT COUNT(*),COALESCE(SUM(duration_seconds),0),MAX(playable),"
            "MAX(play_count),MAX(last_played) FROM music_tracks "
            "WHERE album_id=? AND edition_id=("
            " SELECT edition_id FROM music_tracks WHERE album_id=? AND edition_id IS NOT NULL"
            " GROUP BY edition_id ORDER BY MAX(playable) DESC, COUNT(*) DESC LIMIT 1)",
            (album_id, album_id),
        ).fetchone()
        any_playable = conn.execute(
            "SELECT 1 FROM music_tracks WHERE album_id=? AND playable=1 LIMIT 1", (album_id,)
        ).fetchone()
        formats = _uniq(fmt for _payload, _source, fmt in payloads)
        source_types = _uniq(source for _payload, source, _fmt in payloads)
        dates = [p.get("date_added") for p, _s, _f in payloads if p.get("date_added")]
        played = [p.get("last_played") for p, _s, _f in payloads if p.get("last_played")]
        if stats[4]:
            played.append(stats[4])
        play_counts = [p.get("play_count") for p, _s, _f in payloads if p.get("play_count")]
        if stats[3]:
            play_counts.append(stats[3])
        primary = conn.execute(
            "SELECT a.id,a.sort_name FROM music_album_artists aa JOIN music_artists a "
            "ON a.id=aa.artist_id WHERE aa.album_id=? ORDER BY aa.position LIMIT 1",
            (album_id,),
        ).fetchone()
        title = choose("title") or "Untitled"
        artist_name = choose("display_artist") or ""
        conn.execute(
            "UPDATE music_albums SET title=?,sort_title=?,artist_id=?,artist_name=?,artist_sort=?,"
            "year=?,release_type=?,compilation=?,label=?,genres=?,rating=?,user_rating=?,"
            "play_count=?,last_played=?,date_added=?,thumb=?,fanart=?,formats=?,source_types=?,"
            "track_count=?,duration_seconds=?,playable=?,owned_physical=?,updated_at=? WHERE id=?",
            (
                title,
                fold(title),
                primary["id"] if primary else None,
                artist_name,
                primary["sort_name"] if primary else sort_name(artist_name),
                choose("year"),
                choose("release_type"),
                1 if any(p.get("compilation") for p, _s, _f in payloads) else 0,
                choose("label"),
                encode(combined("genres")),
                choose("rating"),
                choose("user_rating"),
                max(play_counts) if play_counts else None,
                max(played) if played else None,
                min(dates) if dates else None,
                encode(combined("thumb")),
                encode(combined("fanart")),
                encode(formats),
                encode(source_types),
                stats[0] or max((p.get("track_count") or 0 for p, _s, _f in payloads), default=0),
                stats[1] or 0,
                1 if any_playable else 0,
                1 if any(fmt in PHYSICAL_FORMATS for fmt in formats) else 0,
                stamp,
                album_id,
            ),
        )
        conn.execute(
            "DELETE FROM music_albums_fts WHERE rowid=(SELECT rid FROM music_albums WHERE id=?)",
            (album_id,),
        )
        labels = " ".join(
            filter(
                None,
                [title, artist_name, choose("label") or ""]
                + [p.get("catalog_number") or "" for p, _s, _f in payloads]
                + [p.get("barcode") or "" for p, _s, _f in payloads]
                + combined("genres"),
            )
        )
        conn.execute(
            "INSERT INTO music_albums_fts(rowid,text) SELECT rid,? FROM music_albums WHERE id=?",
            (fold(labels), album_id),
        )

    @staticmethod
    def _prune_albums(conn: sqlite3.Connection) -> int:
        orphans = [
            (row[0], row[1])
            for row in conn.execute(
                "SELECT rid,id FROM music_albums a WHERE NOT EXISTS "
                "(SELECT 1 FROM music_album_editions e WHERE e.album_id=a.id)"
            )
        ]
        conn.executemany("DELETE FROM music_albums_fts WHERE rowid=?", ((r,) for r, _ in orphans))
        conn.executemany("DELETE FROM music_albums WHERE id=?", ((i,) for _, i in orphans))
        return len(orphans)

    @staticmethod
    def _rebuild_artist(conn: sqlite3.Connection, artist_id: str, stamp: str) -> None:
        sources = conn.execute(
            "SELECT p.payload,s.type FROM music_artist_sources p JOIN sources s "
            "ON s.id=p.source_id WHERE p.artist_id=?",
            (artist_id,),
        ).fetchall()
        payloads = sorted(
            ((json.loads(row["payload"]), row["type"]) for row in sources),
            key=lambda item: SOURCE_PRIORITY.get(item[1], 9),
        )
        current = conn.execute(
            "SELECT rid,name FROM music_artists WHERE id=?", (artist_id,)
        ).fetchone()
        if current is None:
            return
        album_count = conn.execute(
            "SELECT COUNT(*) FROM music_album_artists WHERE artist_id=?", (artist_id,)
        ).fetchone()[0]
        track_count = conn.execute(
            "SELECT COUNT(*) FROM music_tracks WHERE artist_id=?", (artist_id,)
        ).fetchone()[0]
        update: dict[str, Any] = {"album_count": album_count, "track_count": track_count}
        if payloads:
            first = payloads[0][0]
            for field in ("biography", "mbid"):
                value = next((p.get(field) for p, _ in payloads if p.get(field)), None)
                update[field] = value
            update["genres"] = encode(_uniq(g for p, _ in payloads for g in p.get("genres") or []))
            update["thumb"] = encode(_uniq(t for p, _ in payloads for t in p.get("thumb") or []))
            update["fanart"] = encode(_uniq(t for p, _ in payloads for t in p.get("fanart") or []))
            update["sort_name"] = sort_name(first.get("name"), first.get("sort_name"))
        update["updated_at"] = stamp
        assignments = ",".join(f"{key}=?" for key in update)
        conn.execute(
            f"UPDATE music_artists SET {assignments} WHERE id=?",
            (*update.values(), artist_id),
        )
        conn.execute("DELETE FROM music_artists_fts WHERE rowid=?", (current["rid"],))
        conn.execute(
            "INSERT INTO music_artists_fts(rowid,text) VALUES(?,?)",
            (current["rid"], fold(current["name"])),
        )

    @staticmethod
    def _prune_artists(conn: sqlite3.Connection) -> int:
        orphans = [
            (row[0], row[1])
            for row in conn.execute(
                "SELECT rid,id FROM music_artists a WHERE "
                "NOT EXISTS (SELECT 1 FROM music_artist_sources s WHERE s.artist_id=a.id) AND "
                "NOT EXISTS (SELECT 1 FROM music_album_artists x WHERE x.artist_id=a.id) AND "
                "NOT EXISTS (SELECT 1 FROM music_tracks t WHERE t.artist_id=a.id)"
            )
        ]
        conn.executemany("DELETE FROM music_artists_fts WHERE rowid=?", ((r,) for r, _ in orphans))
        conn.executemany("DELETE FROM music_artists WHERE id=?", ((i,) for _, i in orphans))
        return len(orphans)

    # ----------------------------------------------------------------- queries
    def status(self) -> dict[str, Any]:
        with self.store.connect() as conn:
            counts = {
                "artists": conn.execute("SELECT COUNT(*) FROM music_artists").fetchone()[0],
                "albums": conn.execute("SELECT COUNT(*) FROM music_albums").fetchone()[0],
                "tracks": conn.execute("SELECT COUNT(*) FROM music_tracks").fetchone()[0],
                "playable_tracks": conn.execute(
                    "SELECT COUNT(*) FROM music_tracks WHERE playable=1"
                ).fetchone()[0],
                "physical_albums": conn.execute(
                    "SELECT COUNT(*) FROM music_albums WHERE owned_physical=1"
                ).fetchone()[0],
            }
            formats: dict[str, int] = {}
            for row in conn.execute("SELECT format,COUNT(*) FROM music_album_editions GROUP BY 1"):
                formats[row[0]] = row[1]
            sources = [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "type": row["type"],
                    "albums": row["albums"],
                    "tracks": row["tracks"],
                }
                for row in conn.execute(
                    "SELECT s.id,s.name,s.type,"
                    "(SELECT COUNT(*) FROM music_album_editions e WHERE e.source_id=s.id) AS albums,"
                    "(SELECT COUNT(*) FROM music_tracks t WHERE t.source_id=s.id) AS tracks "
                    "FROM sources s WHERE s.type IN ({}) ORDER BY s.name".format(
                        ",".join("?" for _ in MUSIC_SOURCE_TYPES)
                    ),
                    MUSIC_SOURCE_TYPES,
                )
            ]
            revision = conn.execute(
                "SELECT value FROM catalogue_meta WHERE key='music_revision'"
            ).fetchone()
        return {
            **counts,
            "formats": formats,
            "sources": sources,
            "revision": int(revision[0]) if revision else 0,
        }

    def facets(self) -> dict[str, Any]:
        with self.store.connect() as conn:
            genres: set[str] = set()
            for row in conn.execute("SELECT DISTINCT genres FROM music_albums"):
                genres.update(json.loads(row[0]))
            formats = [
                r[0] for r in conn.execute("SELECT DISTINCT format FROM music_album_editions")
            ]
            years = [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT year FROM music_albums WHERE year IS NOT NULL ORDER BY 1 DESC"
                )
            ]
        return {
            "genres": sorted(genres, key=fold),
            "formats": sorted(
                formats, key=lambda f: MUSIC_FORMATS.index(f) if f in MUSIC_FORMATS else 99
            ),
            "years": years,
        }

    @staticmethod
    def _fts_query(text: str) -> str | None:
        words = [w for w in _NON_WORD.split(fold(text)) if w]
        if not words:
            return None
        return " ".join(f'"{w}"*' for w in words[:12])

    @staticmethod
    def _artist_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "sort_name": row["sort_name"],
            "mbid": row["mbid"],
            "genres": json.loads(row["genres"]),
            "album_count": row["album_count"],
            "track_count": row["track_count"],
            "thumb": _first(json.loads(row["thumb"])),
            "fanart": _first(json.loads(row["fanart"])),
        }

    @staticmethod
    def _album_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "title": row["title"],
            "artist": row["artist_name"],
            "artist_id": row["artist_id"],
            "year": row["year"],
            "release_type": row["release_type"],
            "compilation": bool(row["compilation"]),
            "label": row["label"],
            "genres": json.loads(row["genres"]),
            "rating": row["rating"],
            "user_rating": row["user_rating"],
            "play_count": row["play_count"],
            "last_played": row["last_played"],
            "date_added": row["date_added"],
            "formats": json.loads(row["formats"]),
            "source_types": json.loads(row["source_types"]),
            "track_count": row["track_count"],
            "duration_seconds": row["duration_seconds"],
            "playable": bool(row["playable"]),
            "owned_physical": bool(row["owned_physical"]),
            "thumb": _first(json.loads(row["thumb"])),
            "fanart": _first(json.loads(row["fanart"])),
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _track_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "title": row["title"],
            "artist": row["artist_name"],
            "artist_id": row["artist_id"],
            "album": row["album_title"],
            "album_id": row["album_id"],
            "edition_id": row["edition_id"],
            "source_id": row["source_id"],
            "disc": row["disc"],
            "track": row["track"],
            "position": row["position"],
            "duration_seconds": row["duration_seconds"],
            "year": row["year"],
            "genres": json.loads(row["genres"]),
            "playable": bool(row["playable"]),
            "format": row["format"],
            "file": row["file"],
            "rating": row["rating"],
            "user_rating": row["user_rating"],
            "play_count": row["play_count"],
            "last_played": row["last_played"],
            "date_added": row["date_added"],
            "audio": json.loads(row["audio"]),
        }

    def artists(
        self, *, query: str = "", genre: str = "", offset: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        limit = max(1, min(MAX_PAGE, limit))
        where, params = ["1=1"], []
        if query and (fts := self._fts_query(query)):
            where.append(
                "rid IN (SELECT rowid FROM music_artists_fts WHERE music_artists_fts MATCH ?)"
            )
            params.append(fts)
        if genre:
            where.append("EXISTS (SELECT 1 FROM json_each(genres) WHERE value=?)")
            params.append(genre)
        clause = " AND ".join(where)
        with self.store.connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM music_artists WHERE {clause}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM music_artists WHERE {clause} ORDER BY sort_name,id LIMIT ? OFFSET ?",
                (*params, limit, max(0, offset)),
            ).fetchall()
        return {
            "items": [self._artist_view(r) for r in rows],
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    def artist(self, artist_id: str) -> dict[str, Any] | None:
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM music_artists WHERE id=?", (artist_id,)).fetchone()
            if row is None:
                return None
            value = self._artist_view(row)
            value["biography"] = row["biography"]
            value["albums"] = [
                self._album_view(a)
                for a in conn.execute(
                    "SELECT a.* FROM music_albums a JOIN music_album_artists x ON x.album_id=a.id "
                    "WHERE x.artist_id=? ORDER BY a.year IS NULL,a.year,a.sort_title",
                    (artist_id,),
                )
            ]
            # Appearances on albums credited to someone else (features, compilations).
            value["appears_on"] = [
                self._album_view(a)
                for a in conn.execute(
                    "SELECT DISTINCT a.* FROM music_tracks t JOIN music_albums a ON a.id=t.album_id "
                    "WHERE t.artist_id=? AND NOT EXISTS (SELECT 1 FROM music_album_artists x "
                    "WHERE x.album_id=a.id AND x.artist_id=?) ORDER BY a.year,a.sort_title LIMIT 200",
                    (artist_id, artist_id),
                )
            ]
            value["sources"] = [
                {"source_id": s["source_id"], "external_key": s["external_key"]}
                for s in conn.execute(
                    "SELECT source_id,external_key FROM music_artist_sources WHERE artist_id=?",
                    (artist_id,),
                )
            ]
        return value

    _ALBUM_SORTS = {
        "artist": "artist_sort {o},year,sort_title,id",
        "title": "sort_title {o},id",
        "year": "year IS NULL,year {o},artist_sort,sort_title,id",
        "date_added": "date_added IS NULL,date_added {o},id",
        "last_played": "last_played IS NULL,last_played {o},id",
        "rating": "COALESCE(user_rating,rating) IS NULL,COALESCE(user_rating,rating) {o},id",
    }

    def albums(
        self,
        *,
        query: str = "",
        artist_id: str = "",
        genre: str = "",
        fmt: str = "",
        source_type: str = "",
        playable: bool | None = None,
        physical: bool | None = None,
        year: int | None = None,
        sort: str = "artist",
        order: str = "asc",
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        limit = max(1, min(MAX_PAGE, limit))
        where, params = ["1=1"], []
        if query and (fts := self._fts_query(query)):
            where.append(
                "(rid IN (SELECT rowid FROM music_albums_fts WHERE music_albums_fts MATCH ?) OR "
                "id IN (SELECT x.album_id FROM music_album_artists x JOIN music_artists ar "
                "ON ar.id=x.artist_id WHERE ar.rid IN (SELECT rowid FROM music_artists_fts "
                "WHERE music_artists_fts MATCH ?)))"
            )
            params += [fts, fts]
        if artist_id:
            where.append("id IN (SELECT album_id FROM music_album_artists WHERE artist_id=?)")
            params.append(artist_id)
        if genre:
            where.append("EXISTS (SELECT 1 FROM json_each(genres) WHERE value=?)")
            params.append(genre)
        if fmt:
            where.append("EXISTS (SELECT 1 FROM json_each(formats) WHERE value=?)")
            params.append(fmt)
        if source_type:
            where.append("EXISTS (SELECT 1 FROM json_each(source_types) WHERE value=?)")
            params.append(source_type)
        if playable is not None:
            where.append("playable=?")
            params.append(1 if playable else 0)
        if physical is not None:
            where.append("owned_physical=?")
            params.append(1 if physical else 0)
        if year:
            where.append("year=?")
            params.append(year)
        direction = "DESC" if order == "desc" else "ASC"
        sort_sql = self._ALBUM_SORTS.get(sort, self._ALBUM_SORTS["artist"]).format(o=direction)
        clause = " AND ".join(where)
        with self.store.connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM music_albums WHERE {clause}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM music_albums WHERE {clause} ORDER BY {sort_sql} LIMIT ? OFFSET ?",
                (*params, limit, max(0, offset)),
            ).fetchall()
        return {
            "items": [self._album_view(r) for r in rows],
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    def album(self, album_id: str) -> dict[str, Any] | None:
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM music_albums WHERE id=?", (album_id,)).fetchone()
            if row is None:
                return None
            value = self._album_view(row)
            value["artists"] = [
                {"id": a["id"], "name": a["name"]}
                for a in conn.execute(
                    "SELECT a.id,a.name FROM music_album_artists x JOIN music_artists a "
                    "ON a.id=x.artist_id WHERE x.album_id=? ORDER BY x.position",
                    (album_id,),
                )
            ]
            value["editions"] = []
            for edition in conn.execute(
                "SELECT e.*,s.name AS source_name,s.type AS source_type FROM music_album_editions e "
                "JOIN sources s ON s.id=e.source_id WHERE e.album_id=? "
                "ORDER BY e.format='Digital' DESC,s.name",
                (album_id,),
            ):
                payload = json.loads(edition["payload"])
                value["editions"].append(
                    {
                        "id": edition["id"],
                        "source_id": edition["source_id"],
                        "source_name": edition["source_name"],
                        "source_type": edition["source_type"],
                        "format": edition["format"],
                        **{
                            key: payload.get(key)
                            for key in (
                                "format_detail",
                                "label",
                                "catalog_number",
                                "barcode",
                                "country",
                                "discogs_id",
                                "mbid",
                                "release_group_mbid",
                                "release_date",
                                "year",
                                "disc_count",
                                "media_condition",
                                "sleeve_condition",
                                "notes",
                                "purchase_date",
                                "kodi_id",
                            )
                        },
                    }
                )
            value["tracks"] = [
                self._track_view(t)
                for t in conn.execute(
                    "SELECT * FROM music_tracks WHERE album_id=? "
                    "ORDER BY playable DESC,edition_id,COALESCE(disc,1),track IS NULL,track,position,rid",
                    (album_id,),
                )
            ]
        return value

    _TRACK_SORTS = {
        "title": "sort_title {o},rid",
        "artist": "artist_name {o},album_title,disc,track,rid",
        "album": "album_title {o},disc,track,rid",
        "date_added": "date_added IS NULL,date_added {o},rid",
        "last_played": "last_played IS NULL,last_played {o},rid",
        "play_count": "COALESCE(play_count,0) {o},rid",
    }

    def tracks(
        self,
        *,
        query: str = "",
        album_id: str = "",
        artist_id: str = "",
        genre: str = "",
        playable: bool | None = None,
        sort: str = "title",
        order: str = "asc",
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        limit = max(1, min(MAX_PAGE, limit))
        where, params = ["1=1"], []
        if query and (fts := self._fts_query(query)):
            where.append(
                "rid IN (SELECT rowid FROM music_tracks_fts WHERE music_tracks_fts MATCH ?)"
            )
            params.append(fts)
        if album_id:
            where.append("album_id=?")
            params.append(album_id)
        if artist_id:
            where.append("artist_id=?")
            params.append(artist_id)
        if genre:
            where.append("EXISTS (SELECT 1 FROM json_each(genres) WHERE value=?)")
            params.append(genre)
        if playable is not None:
            where.append("playable=?")
            params.append(1 if playable else 0)
        direction = "DESC" if order == "desc" else "ASC"
        sort_sql = self._TRACK_SORTS.get(sort, self._TRACK_SORTS["title"]).format(o=direction)
        clause = " AND ".join(where)
        with self.store.connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM music_tracks WHERE {clause}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM music_tracks WHERE {clause} ORDER BY {sort_sql} LIMIT ? OFFSET ?",
                (*params, limit, max(0, offset)),
            ).fetchall()
        return {
            "items": [self._track_view(r) for r in rows],
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    def track(self, track_id: str) -> dict[str, Any] | None:
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM music_tracks WHERE id=?", (track_id,)).fetchone()
            if row is None:
                return None
            value = self._track_view(row)
            value["metadata"] = json.loads(row["metadata"])
            value["external_key"] = row["external_key"]
        return value

    def export(self, collection: str, *, after: int = 0, limit: int = 2000) -> dict[str, Any]:
        """Keyset pages for complete offline mirrors (iOS). ``after`` is an opaque cursor."""
        limit = max(1, min(MAX_EXPORT_PAGE, limit))
        table, view = {
            "artists": ("music_artists", self._artist_view),
            "albums": ("music_albums", self._album_view),
            "tracks": ("music_tracks", self._track_view),
        }[collection]
        with self.store.connect() as conn:
            conn.execute("BEGIN")
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE rid>? ORDER BY rid LIMIT ?", (after, limit)
            ).fetchall()
            total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            revision = conn.execute(
                "SELECT value FROM catalogue_meta WHERE key='music_revision'"
            ).fetchone()
            items = [view(r) for r in rows]
            if collection == "albums":
                artists_by_album: dict[str, list[str]] = {}
                ids = [r["id"] for r in rows]
                for start in range(0, len(ids), 500):
                    chunk = ids[start : start + 500]
                    for link in conn.execute(
                        "SELECT album_id,artist_id FROM music_album_artists WHERE album_id IN ({}) "
                        "ORDER BY position".format(",".join("?" for _ in chunk)),
                        chunk,
                    ):
                        artists_by_album.setdefault(link[0], []).append(link[1])
                for item in items:
                    item["artist_ids"] = artists_by_album.get(item["id"], [])
        return {
            "items": items,
            "next": rows[-1]["rid"] if len(rows) == limit else None,
            "total": total,
            "revision": int(revision[0]) if revision else 0,
        }

    def search(self, text: str, limit: int = 20) -> dict[str, Any]:
        return {
            "artists": self.artists(query=text, limit=limit)["items"],
            "albums": self.albums(query=text, limit=limit)["items"],
            "tracks": self.tracks(query=text, limit=limit)["items"],
        }

    def artwork(self, entity: str, entity_id: str, aspect: str) -> list[str]:
        table = {"album": "music_albums", "artist": "music_artists"}.get(entity)
        if table is None or aspect not in ("thumb", "fanart"):
            return []
        with self.store.connect() as conn:
            row = conn.execute(f"SELECT {aspect} FROM {table} WHERE id=?", (entity_id,)).fetchone()
        return json.loads(row[0]) if row else []


class _ArtistResolver:
    """Find or create global artists with a per-snapshot cache."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.cache: dict[str, str] = {}

    def resolve(
        self, name: str | None, mbid: str | None, record: MusicArtistRecord | None = None
    ) -> str | None:
        name = str(name or "").strip()
        key = name_key(name)
        if not key and not mbid:
            return None
        cache_key = f"mbid:{mbid}" if mbid else f"name:{key}"
        if cache_key in self.cache:
            return self.cache[cache_key]
        row = None
        if mbid:
            row = self.conn.execute(
                "SELECT id FROM music_artists WHERE mbid=? LIMIT 1", (mbid,)
            ).fetchone()
        if row is None and key:
            # Prefer an exact folded-name match without a conflicting MBID.
            row = self.conn.execute(
                "SELECT id FROM music_artists WHERE name_key=? AND (mbid IS NULL OR ? IS NULL "
                "OR mbid=?) ORDER BY rid LIMIT 1",
                (key, mbid, mbid),
            ).fetchone()
        if row is not None:
            artist_id = row["id"]
            if mbid:
                self.conn.execute(
                    "UPDATE music_artists SET mbid=COALESCE(mbid,?) WHERE id=?", (mbid, artist_id)
                )
        else:
            artist_id = str(uuid4())
            self.conn.execute(
                "INSERT INTO music_artists(id,name,sort_name,name_key,mbid,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    artist_id,
                    name or "Unknown artist",
                    sort_name(name, record.sort_name if record else None),
                    key,
                    mbid,
                    now(),
                ),
            )
        self.cache[cache_key] = artist_id
        if key:
            self.cache.setdefault(f"name:{key}", artist_id)
        return artist_id


def _artist_payload(artist: MusicArtistRecord) -> dict[str, Any]:
    return {
        "name": artist.name,
        "sort_name": artist.sort_name,
        "mbid": artist.mbid,
        "genres": artist.genres,
        "biography": artist.biography,
        "thumb": artist.thumb,
        "fanart": artist.fanart,
        "metadata": artist.metadata,
    }


def _album_payload(album: MusicAlbumRecord, display: str, source_type: str) -> dict[str, Any]:
    payload = {
        key: getattr(album, key)
        for key in (
            "title",
            "year",
            "release_date",
            "genres",
            "mbid",
            "release_group_mbid",
            "release_type",
            "compilation",
            "label",
            "rating",
            "user_rating",
            "play_count",
            "last_played",
            "date_added",
            "thumb",
            "fanart",
            "format_detail",
            "disc_count",
            "catalog_number",
            "barcode",
            "country",
            "discogs_id",
            "media_condition",
            "sleeve_condition",
            "notes",
            "purchase_date",
        )
    }
    payload["display_artist"] = display
    payload["artists"] = album.artists
    payload["source_type"] = source_type
    payload["track_count"] = album.metadata.get("track_count")
    payload["kodi_id"] = album.metadata.get("kodi_id")
    payload["metadata"] = album.metadata
    return payload


__all__ = [
    "MUSIC_COLLECTION_TYPES",
    "MUSIC_SOURCE_TYPES",
    "MusicCatalogue",
    "album_match_key",
    "name_key",
    "title_key",
    "MusicAlbumRecord",
    "MusicArtistRecord",
    "MusicTrackRecord",
    "MusicSnapshot",
]
