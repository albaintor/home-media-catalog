"""Read model of the music library (artists → albums → songs).

Music works share the catalogue tables with video works. These queries keep
music out of the video endpoints and expose compact, paginated views that are
cheap enough for libraries of several hundred thousand songs.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from typing import Any

from .connectors.music import MUSIC_COLLECTION_TYPES, PLAYABLE_MUSIC_TYPES
from .utils import fold

PLAYABLE_SOURCE_TYPES = ("kodi", *PLAYABLE_MUSIC_TYPES)
EXPORT_LIMIT = 2000


def _fts_query(q: str) -> str | None:
    words = re.findall(r"\w+", q or "", flags=re.UNICODE)
    if not words:
        return None
    return " AND ".join('"' + fold(word).replace('"', '""') + '"*' for word in words)


def _artwork(work_id: str, row: sqlite3.Row, aspect: str = "poster") -> str | None:
    return f"/api/v1/media/{work_id}/artwork/{aspect}" if json.loads(row[aspect] or "[]") else None


def _types_clause(column: str, types: tuple[str, ...]) -> str:
    quoted = ",".join("'" + t + "'" for t in types) or "''"
    return f"EXISTS(SELECT 1 FROM json_each({column}) WHERE value IN ({quoted}))"


class MusicQueries:
    """Mixin for :class:`Store`; relies on ``connect`` and ``_sources_for_works``."""

    # --- helpers ------------------------------------------------------------

    @staticmethod
    def _payloads(conn: sqlite3.Connection, work_ids: list[str]) -> dict[str, list[dict]]:
        found: dict[str, list[dict]] = defaultdict(list)
        for start in range(0, len(work_ids), 500):
            chunk = work_ids[start : start + 500]
            if not chunk:
                continue
            slots = ",".join("?" for _ in chunk)
            for row in conn.execute(
                "SELECT r.id,r.work_id,r.source_id,r.payload,s.type,s.name "
                "FROM records r JOIN sources s ON s.id=r.source_id "
                f"WHERE r.work_id IN ({slots}) ORDER BY s.type!='kodi',s.name,r.external_key",
                chunk,
            ):
                payload = json.loads(row["payload"])
                payload["_edition_id"] = row["id"]
                payload["_source_id"] = row["source_id"]
                payload["_source_type"] = row["type"]
                payload["_source_name"] = row["name"]
                found[row["work_id"]].append(payload)
        return found

    @staticmethod
    def _meta(payloads: list[dict], name: str) -> Any:
        return next(
            (
                p["metadata"][name]
                for p in payloads
                if (p.get("metadata") or {}).get(name) not in (None, "", [], {})
            ),
            None,
        )

    @staticmethod
    def _edition_view(payload: dict) -> dict[str, Any]:
        metadata = payload.get("metadata") or {}
        return {
            "id": payload["_edition_id"],
            "source_id": payload["_source_id"],
            "source_type": payload["_source_type"],
            "source_name": payload["_source_name"],
            "external_key": payload["external_key"],
            "physical": bool(metadata.get("physical")),
            "playable": bool(metadata.get("playable")),
            "formats": payload.get("formats") or [],
            "media_format": metadata.get("media_format"),
            "label": metadata.get("label"),
            "catalog_number": metadata.get("catalog_number"),
            "barcode": metadata.get("barcode"),
            "notes": metadata.get("notes"),
            "media_condition": metadata.get("media_condition"),
            "sleeve_condition": metadata.get("sleeve_condition"),
            "discogs_release_id": metadata.get("discogs_release_id"),
            "kodi_id": metadata.get("kodi_id") if payload["_source_type"] == "kodi" else None,
            "file": metadata.get("file"),
            "date_added": payload.get("date_added"),
        }

    def _artist_rows(self, conn, rows) -> list[dict[str, Any]]:
        ids = [row["id"] for row in rows]
        counts: dict[str, int] = {}
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            if chunk:
                slots = ",".join("?" for _ in chunk)
                counts.update(
                    {
                        r[0]: r[1]
                        for r in conn.execute(
                            "SELECT parent_id,COUNT(*) FROM works WHERE kind='album' "
                            f"AND parent_id IN ({slots}) GROUP BY parent_id",
                            chunk,
                        )
                    }
                )
        return [
            {
                "id": row["id"],
                "name": row["title"],
                "sort_name": row["sort_title"],
                "genres": json.loads(row["genres"] or "[]"),
                "source_types": json.loads(row["source_types"] or "[]"),
                "album_count": counts.get(row["id"], 0),
                "date_added": row["date_added"],
                "plot": row["plot"],
                "artwork": {
                    "poster": _artwork(row["id"], row),
                    "fanart": _artwork(row["id"], row, "fanart"),
                },
            }
            for row in rows
        ]

    def _album_rows(self, conn, rows, *, with_editions: bool = False) -> list[dict[str, Any]]:
        ids = [row["id"] for row in rows]
        payloads = self._payloads(conn, ids)
        song_stats: dict[str, tuple[int, float]] = {}
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            if chunk:
                slots = ",".join("?" for _ in chunk)
                song_stats.update(
                    {
                        r[0]: (r[1], r[2] or 0)
                        for r in conn.execute(
                            "SELECT parent_id,COUNT(*),SUM(total_seconds) FROM works "
                            f"WHERE kind='song' AND parent_id IN ({slots}) GROUP BY parent_id",
                            chunk,
                        )
                    }
                )
        sources = self._sources_for_works(conn, ids)
        items = []
        for row in rows:
            entries = payloads.get(row["id"], [])
            types = json.loads(row["source_types"] or "[]")
            count, seconds = song_stats.get(row["id"], (0, 0))
            item = {
                "id": row["id"],
                "title": row["title"],
                "artist": self._meta(entries, "artist") or "",
                "artist_id": row["parent_id"],
                "year": row["year"],
                "genres": json.loads(row["genres"] or "[]"),
                "formats": json.loads(row["formats"] or "[]"),
                "source_types": types,
                "media_sources": sources.get(row["id"], []),
                "physical": any((p.get("metadata") or {}).get("physical") for p in entries),
                "playable": any(t in PLAYABLE_SOURCE_TYPES for t in types) and count > 0,
                "track_count": count or self._meta(entries, "track_count"),
                "duration_seconds": int(seconds) or self._meta(entries, "duration_seconds"),
                "label": self._meta(entries, "label"),
                "compilation": bool(self._meta(entries, "compilation")),
                "rating": row["rating"],
                "user_rating": row["user_rating"],
                "play_count": row["play_count"],
                "date_added": row["date_added"],
                "last_played": row["last_played"],
                "artwork": {
                    "poster": _artwork(row["id"], row),
                    "fanart": _artwork(row["id"], row, "fanart"),
                },
            }
            if with_editions:
                item["editions"] = [self._edition_view(p) for p in entries]
            items.append(item)
        return items

    def _song_rows(self, conn, rows) -> list[dict[str, Any]]:
        payloads = self._payloads(conn, [row["id"] for row in rows])
        items = []
        for row in rows:
            entries = payloads.get(row["id"], [])
            playable = next(
                (p for p in entries if p["_source_type"] in PLAYABLE_SOURCE_TYPES), None
            )
            metadata = (playable or (entries[0] if entries else {})).get("metadata") or {}
            items.append(
                {
                    "id": row["id"],
                    "title": row["title"],
                    "album_id": row["parent_id"],
                    "album": metadata.get("album"),
                    "artist": metadata.get("artist"),
                    "album_artist": metadata.get("album_artist"),
                    "disc": row["season"],
                    "track": row["episode"],
                    "duration_seconds": int(row["total_seconds"] or 0) or None,
                    "year": row["year"],
                    "genres": json.loads(row["genres"] or "[]"),
                    "formats": json.loads(row["formats"] or "[]"),
                    "rating": row["rating"],
                    "user_rating": row["user_rating"],
                    "play_count": row["play_count"],
                    "last_played": row["last_played"],
                    "date_added": row["date_added"],
                    "edition_id": playable["_edition_id"] if playable else None,
                    "source_id": playable["_source_id"] if playable else None,
                    "source_type": playable["_source_type"] if playable else None,
                    "kodi_id": metadata.get("kodi_id") if playable else None,
                    "file": metadata.get("file") if playable else None,
                }
            )
        return items

    @staticmethod
    def _page(items, total, limit, offset) -> dict[str, Any]:
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(items) < total,
        }

    @staticmethod
    def _validate_page(limit: int, offset: int, maximum: int = 200) -> None:
        if not 1 <= limit <= maximum or offset < 0:
            raise ValueError("Invalid pagination option.")

    # --- public queries ------------------------------------------------------

    def music_status(self) -> dict[str, Any]:
        with self.connect() as conn:
            counts = {
                row[0]: row[1]
                for row in conn.execute(
                    "SELECT kind,COUNT(*) FROM works WHERE kind IN ('artist','album','song') "
                    "GROUP BY kind"
                )
            }
            revision = conn.execute(
                "SELECT value FROM catalogue_meta WHERE key='music_revision'"
            ).fetchone()
            physical = conn.execute(
                "SELECT COUNT(*) FROM works w WHERE w.kind='album' AND "
                + _types_clause("w.source_types", MUSIC_COLLECTION_TYPES)
            ).fetchone()[0]
            sources = [
                {"id": row["id"], "name": row["name"], "type": row["type"], "count": row["count"]}
                for row in conn.execute(
                    "SELECT s.id,s.name,s.type,COUNT(r.id) AS count FROM sources s "
                    "JOIN records r ON r.source_id=s.id "
                    "WHERE json_extract(r.payload,'$.kind') IN ('artist','album','song') "
                    "GROUP BY s.id ORDER BY s.name COLLATE NOCASE"
                )
            ]
        return {
            "artists": counts.get("artist", 0),
            "albums": counts.get("album", 0),
            "songs": counts.get("song", 0),
            "physical_albums": physical,
            "revision": revision[0] if revision else None,
            "sources": sources,
            "collection_types": list(MUSIC_COLLECTION_TYPES),
        }

    def music_facets(self) -> dict[str, Any]:
        with self.connect() as conn:
            genres = [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT j.value FROM works w,json_each(w.genres) j "
                    "WHERE w.kind='album' ORDER BY j.value COLLATE NOCASE"
                )
            ]
            formats = [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT j.value FROM works w,json_each(w.formats) j "
                    "WHERE w.kind='album' ORDER BY j.value COLLATE NOCASE"
                )
            ]
            years = [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT year FROM works WHERE kind='album' AND year IS NOT NULL "
                    "ORDER BY year DESC"
                )
            ]
        return {"genres": genres, "formats": formats, "years": years}

    def music_artists(
        self,
        *,
        q: str = "",
        source_id: str | None = None,
        sort: str = "name",
        order: str = "asc",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        self._validate_page(limit, offset)
        clauses, params = ["w.kind='artist'"], []
        if match := _fts_query(q):
            clauses.append(
                "w.id IN (SELECT work_id FROM works_title_fts WHERE works_title_fts MATCH ?)"
            )
            params.append(match)
        if source_id:
            clauses.append("EXISTS(SELECT 1 FROM records r WHERE r.work_id=w.id AND r.source_id=?)")
            params.append(source_id)
        columns = {"name": "w.sort_title", "date_added": "w.date_added"}
        if sort not in columns or order not in ("asc", "desc"):
            raise ValueError("Invalid sort option.")
        where = " WHERE " + " AND ".join(clauses)
        with self.connect() as conn:
            conn.execute("BEGIN")
            total = conn.execute("SELECT COUNT(*) FROM works w" + where, params).fetchone()[0]
            rows = conn.execute(
                f"SELECT w.* FROM works w{where} ORDER BY {columns[sort]} IS NULL,"
                f"{columns[sort]} {order},w.id LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            return self._page(self._artist_rows(conn, rows), total, limit, offset)

    def music_albums(
        self,
        *,
        q: str = "",
        artist_id: str | None = None,
        genre: str | None = None,
        format: str | None = None,
        source_type: str | None = None,
        source_id: str | None = None,
        physical: bool | None = None,
        year: int | None = None,
        ids: list[str] | None = None,
        sort: str = "artist",
        order: str = "asc",
        limit: int = 60,
        offset: int = 0,
    ) -> dict[str, Any]:
        self._validate_page(limit, offset)
        clauses, params = ["w.kind='album'"], []
        if ids is not None:
            if len(ids) > 500:
                raise ValueError("Too many selected album IDs.")
            clauses.append("w.id IN (" + (",".join("?" for _ in ids) or "''") + ")")
            params.extend(ids)
        if match := _fts_query(q):
            clauses.append(
                "w.id IN (SELECT work_id FROM works_title_fts WHERE works_title_fts MATCH ?)"
            )
            params.append(match)
        if artist_id:
            clauses.append("w.parent_id=?")
            params.append(artist_id)
        for column, value in (("genres", genre), ("formats", format), ("source_types", source_type)):
            if value:
                clauses.append(
                    f"EXISTS(SELECT 1 FROM json_each(w.{column}) WHERE value=? COLLATE NOCASE)"
                )
                params.append(value)
        if source_id:
            clauses.append("EXISTS(SELECT 1 FROM records r WHERE r.work_id=w.id AND r.source_id=?)")
            params.append(source_id)
        if physical is not None:
            clause = _types_clause("w.source_types", MUSIC_COLLECTION_TYPES)
            clauses.append(clause if physical else "NOT " + clause)
        if year is not None:
            clauses.append("w.year=?")
            params.append(year)
        columns = {
            "artist": "a.sort_title",
            "title": "w.sort_title",
            "year": "w.year",
            "date_added": "w.date_added",
            "rating": "w.rating",
            "user_rating": "w.user_rating",
            "last_played": "w.last_played",
        }
        if sort not in columns or order not in ("asc", "desc"):
            raise ValueError("Invalid sort option.")
        where = " WHERE " + " AND ".join(clauses)
        secondary = ",w.year,w.sort_title" if sort == "artist" else ",w.sort_title"
        with self.connect() as conn:
            conn.execute("BEGIN")
            total = conn.execute("SELECT COUNT(*) FROM works w" + where, params).fetchone()[0]
            rows = conn.execute(
                "SELECT w.* FROM works w LEFT JOIN works a ON a.id=w.parent_id"
                + where
                + f" ORDER BY {columns[sort]} IS NULL,{columns[sort]} {order}{secondary},w.id"
                " LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            return self._page(self._album_rows(conn, rows), total, limit, offset)

    def music_songs(
        self,
        *,
        q: str = "",
        album_id: str | None = None,
        artist_id: str | None = None,
        sort: str = "album",
        order: str = "asc",
        limit: int = 200,
        offset: int = 0,
    ) -> dict[str, Any]:
        self._validate_page(limit, offset, 500)
        clauses, params = ["w.kind='song'"], []
        if match := _fts_query(q):
            clauses.append(
                "w.id IN (SELECT work_id FROM works_title_fts WHERE works_title_fts MATCH ?)"
            )
            params.append(match)
        if album_id:
            clauses.append("w.parent_id=?")
            params.append(album_id)
        if artist_id:
            clauses.append("w.parent_id IN (SELECT id FROM works WHERE kind='album' AND parent_id=?)")
            params.append(artist_id)
        orderings = {
            "album": f"w.parent_id {order},w.season IS NULL,w.season,w.episode IS NULL,w.episode,w.sort_title",
            "title": f"w.sort_title {order}",
            "date_added": f"w.date_added IS NULL,w.date_added {order}",
            "last_played": f"w.last_played IS NULL,w.last_played {order}",
            "play_count": f"w.play_count IS NULL,w.play_count {order}",
        }
        if sort not in orderings or order not in ("asc", "desc"):
            raise ValueError("Invalid sort option.")
        where = " WHERE " + " AND ".join(clauses)
        with self.connect() as conn:
            conn.execute("BEGIN")
            total = conn.execute("SELECT COUNT(*) FROM works w" + where, params).fetchone()[0]
            rows = conn.execute(
                f"SELECT w.* FROM works w{where} ORDER BY {orderings[sort]},w.id LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            return self._page(self._song_rows(conn, rows), total, limit, offset)

    def music_artist(self, artist_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM works WHERE id=? AND kind='artist'", (artist_id,)
            ).fetchone()
            if row is None:
                return None
            artist = self._artist_rows(conn, [row])[0]
            albums = conn.execute(
                "SELECT * FROM works WHERE kind='album' AND parent_id=? "
                "ORDER BY year IS NULL,year,sort_title,id",
                (artist_id,),
            ).fetchall()
            artist["albums"] = self._album_rows(conn, albums)
            artist["media_sources"] = self._sources_for_works(conn, [artist_id]).get(artist_id, [])
            return artist

    def music_album(self, album_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM works WHERE id=? AND kind='album'", (album_id,)
            ).fetchone()
            if row is None:
                return None
            album = self._album_rows(conn, [row], with_editions=True)[0]
            album["plot"] = row["plot"]
            songs = conn.execute(
                "SELECT * FROM works WHERE kind='song' AND parent_id=? "
                "ORDER BY season IS NULL,season,episode IS NULL,episode,sort_title,id",
                (album_id,),
            ).fetchall()
            album["songs"] = self._song_rows(conn, songs)
            # Physical editions carry their own track list (CLZ Music).
            physical_tracks = []
            for edition in self._payloads(conn, [album_id]).get(album_id, []):
                tracks = (edition.get("metadata") or {}).get("tracks") or []
                if tracks and not physical_tracks:
                    physical_tracks = tracks
            album["physical_tracks"] = physical_tracks
            return album

    def music_export(self, kind: str, *, limit: int = 1000, offset: int = 0) -> dict[str, Any]:
        """Stable compact pages used by the iOS offline replica."""
        if kind not in ("artist", "album", "song"):
            raise ValueError("Unknown music kind.")
        self._validate_page(limit, offset, EXPORT_LIMIT)
        with self.connect() as conn:
            conn.execute("BEGIN")
            total = conn.execute("SELECT COUNT(*) FROM works WHERE kind=?", (kind,)).fetchone()[0]
            rows = conn.execute(
                "SELECT * FROM works WHERE kind=? ORDER BY id LIMIT ? OFFSET ?",
                (kind, limit, offset),
            ).fetchall()
            if kind == "artist":
                items = self._artist_rows(conn, rows)
            elif kind == "album":
                items = self._album_rows(conn, rows, with_editions=True)
            else:
                items = self._song_rows(conn, rows)
            revision = conn.execute(
                "SELECT value FROM catalogue_meta WHERE key='music_revision'"
            ).fetchone()
            page = self._page(items, total, limit, offset)
            page["revision"] = revision[0] if revision else None
            return page

    def music_kodi_target(self, work_id: str) -> dict[str, Any] | None:
        """Kodi IDs needed to play an album or a song on a Kodi instance."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT kind FROM works WHERE id=? AND kind IN ('album','song')", (work_id,)
            ).fetchone()
            if row is None:
                return None
            for record in conn.execute(
                "SELECT r.source_id,r.payload FROM records r JOIN sources s ON s.id=r.source_id "
                "WHERE r.work_id=? AND s.type='kodi'",
                (work_id,),
            ):
                kodi_id = (json.loads(record["payload"]).get("metadata") or {}).get("kodi_id")
                if isinstance(kodi_id, int):
                    return {"kind": row["kind"], "source_id": record["source_id"], "kodi_id": kodi_id}
            return {"kind": row["kind"], "source_id": None, "kodi_id": None}
