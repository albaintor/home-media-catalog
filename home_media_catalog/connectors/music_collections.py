"""Physical music collections: Discogs CSV, CLZ Music XML and a generic list.

These sources describe owned media (CDs, vinyls, cassettes…). They produce
albums ("editions" with a carrier format) and, when the export contains a
track list, non-playable tracks. A future streaming connector would produce
the same ``MusicSnapshot`` with ``playable=True`` tracks.

* ``discogs``   – the CSV from Discogs › Collection › Export.
* ``clz_music`` – the XML export of CLZ Music / Music Collector.
* ``music_csv`` – any CSV/TSV (or JSON array) with columns such as
  artist, album/title, year, format, label, catalog number, barcode, genre,
  notes. French headers (artiste, titre, année, support, référence…) work too.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from typing import Any
from xml.etree.ElementTree import Element, ParseError

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring

from ..utils import fold
from .base import ConnectorError
from .music_base import (
    MusicAlbumRecord,
    MusicArtistRecord,
    MusicSnapshot,
    MusicTrackRecord,
    canonical_music_format,
)

MAX_ROWS = 100_000

# Header aliases (folded, punctuation removed) → canonical field.
ALIASES = {
    "artist": (
        "artist",
        "artists",
        "artiste",
        "artistes",
        "album artist",
        "albumartist",
        "interprete",
        "groupe",
        "band",
        "auteur",
    ),
    "title": ("title", "album", "album title", "titre", "titre album", "release", "nom"),
    "year": (
        "year",
        "released",
        "release year",
        "annee",
        "annee de sortie",
        "date",
        "sortie",
        "release date",
        "date de sortie",
    ),
    "format": ("format", "media", "support", "type de support", "medium", "carrier"),
    "label": ("label", "maison de disques", "editeur", "record label"),
    "catalog_number": (
        "catalog",
        "catalog number",
        "catalog#",
        "catalogue",
        "cat no",
        "catno",
        "reference",
        "ref",
        "numero de catalogue",
        "catalog no",
    ),
    "barcode": ("barcode", "upc", "ean", "code barre", "code barres", "code-barres"),
    "genre": ("genre", "genres", "style", "styles"),
    "country": ("country", "pays"),
    "notes": (
        "notes",
        "note",
        "comment",
        "comments",
        "commentaire",
        "commentaires",
        "collection notes",
    ),
    "rating": ("rating", "note perso", "my rating", "evaluation"),
    "discogs_id": ("release id", "release_id", "discogs", "discogs id"),
    "mbid": ("mbid", "musicbrainz", "musicbrainz id", "release mbid"),
    "media_condition": (
        "collection media condition",
        "media condition",
        "etat disque",
        "etat du disque",
        "condition",
    ),
    "sleeve_condition": (
        "collection sleeve condition",
        "sleeve condition",
        "etat pochette",
        "etat de la pochette",
    ),
    "date_added": ("date added", "added", "date ajout", "ajoute le", "date d ajout"),
    "purchase_date": ("purchase date", "date d achat", "achete le", "acquired"),
    "discs": ("discs", "disc count", "nb disques", "nombre de disques", "qty"),
    "tracks": ("tracks", "tracklist", "pistes", "liste des pistes"),
}


def _header_key(value: str) -> str:
    return re.sub(r"[^a-z0-9#]+", " ", fold(value or "")).strip()


def _field_map(headers: list[str]) -> dict[str, str]:
    mapped: dict[str, str] = {}
    for header in headers:
        key = _header_key(header)
        for field, names in ALIASES.items():
            if field not in mapped and key in {_header_key(n) for n in names}:
                mapped[field] = header
                break
    return mapped


def _decode(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Spreadsheet exports on Windows/macOS are often Windows-1252.
        return data.decode("cp1252", errors="replace")


def _year(value: Any) -> int | None:
    match = re.search(r"(1[89]\d\d|20\d\d)", str(value or ""))
    return int(match.group(1)) if match else None


def _rating(value: Any) -> float | None:
    try:
        rating = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    if rating <= 0:
        return None
    # Discogs and most lists rate on 5; the catalogue uses Kodi's 10 scale.
    return round(rating * 2 if rating <= 5 else min(rating, 10), 2)


def _genres(value: Any) -> list[str]:
    return [g.strip() for g in re.split(r"[;,/|]", str(value or "")) if g.strip()]


def _stable_key(*parts: Any) -> str:
    digest = hashlib.sha1("\x1f".join(str(p or "") for p in parts).encode()).hexdigest()
    return digest[:20]


def _artists(value: str) -> list[str]:
    value = re.sub(r"\s*\(\d+\)$", "", value.strip())  # Discogs disambiguation "(2)"
    return [value] if value else []


def _duration(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        seconds = float(text)
        return seconds / 1000 if seconds > 36000 else seconds
    parts = text.split(":")
    try:
        total = 0.0
        for part in parts:
            total = total * 60 + float(part)
        return total or None
    except ValueError:
        return None


def _snapshot(
    albums: list[MusicAlbumRecord], tracks: list[MusicTrackRecord], kind: str, skipped: int
) -> MusicSnapshot:
    artists: dict[str, MusicArtistRecord] = {}
    for album in albums:
        for name in album.artists:
            key = f"artist:{fold(name)}"
            artists.setdefault(key, MusicArtistRecord(external_key=key, name=name))
            if key not in album.artist_keys:
                album.artist_keys.append(key)
    return MusicSnapshot(
        artists=list(artists.values()),
        albums=albums,
        tracks=tracks,
        report={"music_import_method": kind, "music_rows_skipped": skipped},
    )


def _rows(text: str) -> list[dict[str, str]]:
    stripped = text.lstrip()
    if stripped.startswith("["):
        try:
            values = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ConnectorError("The JSON list is invalid.") from exc
        if not isinstance(values, list) or not all(isinstance(v, dict) for v in values):
            raise ConnectorError("The JSON file must contain a list of objects.")
        return [{str(k): "" if v is None else str(v) for k, v in row.items()} for row in values]
    # The header row decides the delimiter: cells (track lists, notes) may
    # contain other separators.
    header = text.splitlines()[0] if text.strip() else ""
    delimiter = max(",;\t|", key=header.count)
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if not reader.fieldnames:
        raise ConnectorError("The CSV file has no header row.")
    return list(reader)


def parse_tabular(data: bytes, kind: str) -> MusicSnapshot:
    """Discogs CSV or a free-form CSV/TSV/JSON list of physical albums."""
    rows = _rows(_decode(data))
    if len(rows) > MAX_ROWS:
        raise ConnectorError(f"The collection exceeds {MAX_ROWS} rows.")
    if not rows:
        raise ConnectorError("The collection file contains no album.")
    fields = _field_map(list(rows[0].keys()))
    if "artist" not in fields or "title" not in fields:
        raise ConnectorError(
            "The file needs at least an artist and an album/title column "
            "(e.g. Artist, Title — or Artiste, Titre)."
        )

    def get(row, field):
        header = fields.get(field)
        return str(row.get(header) or "").strip() if header else ""

    albums: list[MusicAlbumRecord] = []
    tracks: list[MusicTrackRecord] = []
    seen: dict[str, int] = {}
    skipped = 0
    for row in rows:
        artist, title = get(row, "artist"), get(row, "title")
        if not artist or not title:
            skipped += 1
            continue
        format_text = get(row, "format")
        discogs_id = get(row, "discogs_id") or None
        base_key = (
            f"discogs:{discogs_id}"
            if discogs_id
            else "barcode:" + get(row, "barcode")
            if get(row, "barcode")
            else "row:" + _stable_key(artist, title, format_text, get(row, "catalog_number"))
        )
        # Several copies of the same release are all kept as editions.
        seen[base_key] = seen.get(base_key, 0) + 1
        key = base_key if seen[base_key] == 1 else f"{base_key}#{seen[base_key]}"
        carrier = canonical_music_format(format_text) if format_text else "Other"
        album = MusicAlbumRecord(
            external_key=key,
            title=title,
            artists=_artists(artist),
            display_artist=artist,
            year=_year(get(row, "year")),
            release_date=get(row, "year") or None,
            genres=_genres(get(row, "genre")),
            label=get(row, "label") or None,
            rating=_rating(get(row, "rating")),
            format=carrier,
            format_detail=format_text or None,
            disc_count=int(get(row, "discs")) if get(row, "discs").isdigit() else None,
            catalog_number=get(row, "catalog_number") or None,
            barcode=get(row, "barcode") or None,
            country=get(row, "country") or None,
            discogs_id=discogs_id,
            release_group_mbid=None,
            mbid=get(row, "mbid") or None,
            media_condition=get(row, "media_condition") or None,
            sleeve_condition=get(row, "sleeve_condition") or None,
            notes=get(row, "notes") or None,
            date_added=get(row, "date_added") or None,
            purchase_date=get(row, "purchase_date") or None,
            compilation=fold(artist)
            in ("various", "various artists", "artistes divers", "compilation"),
            metadata={"collection_folder": row.get("CollectionFolder") or None},
        )
        albums.append(album)
        # Optional inline track list: "1. Song; 2. Other" or "A1 Song | B1 Other".
        tracklist = get(row, "tracks")
        if tracklist:
            for index, entry in enumerate(re.split(r"\s*[;|\n]\s*", tracklist), start=1):
                match = re.match(r"^([A-Z]?\d{1,3})[\.\)\s-]+(.+)$", entry.strip())
                position, name = (
                    (match.group(1), match.group(2)) if match else (None, entry.strip())
                )
                if not name:
                    continue
                tracks.append(
                    MusicTrackRecord(
                        external_key=f"{key}:t{index}",
                        album_key=key,
                        title=name,
                        artists=album.artists,
                        display_artist=artist,
                        track=int(position) if position and position.isdigit() else index,
                        position=position,
                        metadata={"album": title},
                    )
                )
    return _snapshot(albums, tracks, kind, skipped)


def _text(node: Element | None, *paths: str) -> str:
    if node is None:
        return ""
    for path in paths:
        found = node.find(path)
        if found is not None:
            value = (found.findtext("displayname") or found.text or "").strip()
            if value:
                return value
    return ""


def parse_clz_music(data: bytes) -> MusicSnapshot:
    """CLZ Music / Music Collector XML export (``<musicinfo><musiclist><music>``)."""
    try:
        root = fromstring(data, forbid_dtd=True)
    except (ParseError, DefusedXmlException) as exc:
        raise ConnectorError("The CLZ Music XML export is invalid.") from exc
    items = root.findall(".//musiclist/music") or root.findall(".//music")
    if not items:
        raise ConnectorError("No <music> entry found: export the collection from CLZ Music as XML.")
    if len(items) > MAX_ROWS:
        raise ConnectorError(f"The collection exceeds {MAX_ROWS} albums.")
    albums: list[MusicAlbumRecord] = []
    tracks: list[MusicTrackRecord] = []
    skipped = 0
    for item in items:
        title = _text(item, "title")
        artist_nodes = item.findall("./artists/artist") or item.findall("./artist")
        artists = [
            (a.findtext("displayname") or a.findtext("name") or a.text or "").strip()
            for a in artist_nodes
        ]
        artists = [a for a in artists if a]
        if not title or not artists:
            skipped += 1
            continue
        identifier = (
            item.findtext("id")
            or item.findtext("index")
            or item.findtext("bpid")
            or _stable_key(artists, title, _text(item, "format"), _text(item, "barcode"))
        )
        key = f"clz:{identifier.strip()}"
        format_text = _text(item, "format", "formats/format")
        year = _year(_text(item, "releasedate/year", "releaseyear", "year", "releasedate/date"))
        genres = [
            (g.findtext("displayname") or g.text or "").strip()
            for g in item.findall("./genres/genre")
        ]
        album = MusicAlbumRecord(
            external_key=key,
            title=title,
            artists=artists,
            display_artist=" / ".join(artists),
            year=year,
            genres=[g for g in genres if g] or _genres(_text(item, "genre")),
            label=_text(item, "labels/label", "label") or None,
            format=canonical_music_format(format_text) if format_text else "CD",
            format_detail=format_text or None,
            catalog_number=_text(item, "labelnumber", "catalognumber", "catalogno") or None,
            barcode=_text(item, "barcode", "upc") or None,
            country=_text(item, "country") or None,
            notes=_text(item, "notes") or None,
            rating=_rating(_text(item, "myrating", "rating")),
            purchase_date=_text(item, "purchasedate/date", "purchasedate") or None,
            date_added=_text(item, "addeddate/date", "dateadded") or None,
            compilation=fold(artists[0]) in ("various", "various artists"),
            metadata={"clz_index": item.findtext("index")},
        )
        discs = item.findall("./discs/disc") or [item]
        album.disc_count = len(discs) if item.find("./discs") is not None else None
        albums.append(album)
        for disc_number, disc in enumerate(discs, start=1):
            for index, track in enumerate(disc.findall("./tracks/track"), start=1):
                name = _text(track, "title")
                if not name:
                    continue
                position = (track.findtext("position") or "").strip() or None
                track_artists = [
                    (a.findtext("displayname") or a.text or "").strip()
                    for a in track.findall("./artists/artist")
                ]
                track_artists = [a for a in track_artists if a] or artists
                tracks.append(
                    MusicTrackRecord(
                        external_key=f"{key}:d{disc_number}t{index}",
                        album_key=key,
                        title=name,
                        artists=track_artists,
                        display_artist=" / ".join(track_artists),
                        disc=disc_number if len(discs) > 1 else None,
                        track=int(position) if position and position.isdigit() else index,
                        position=position,
                        duration_seconds=_duration(
                            track.findtext("lengthsecs") or track.findtext("length")
                        ),
                        metadata={"album": title},
                    )
                )
    return _snapshot(albums, tracks, "clz_music", skipped)


def parse_music_collection(data: bytes, source_type: str) -> MusicSnapshot:
    if source_type == "clz_music":
        return parse_clz_music(data)
    if source_type in ("discogs", "music_csv"):
        return parse_tabular(data, source_type)
    raise ConnectorError("Unsupported music collection type.")
