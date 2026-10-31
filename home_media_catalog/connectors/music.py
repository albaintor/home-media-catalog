"""Music catalogue: shared kinds, matching keys and physical collection imports.

A music library uses the same work/edition model as the video catalogue:

* ``artist`` works are album artists;
* ``album`` works are the unit of ownership (a Kodi album, a CD, a vinyl LP
  of the same album become *editions* of one album work);
* ``song`` works belong to an album through ``parent_id``.

Physical collections (Discogs, CLZ Music, a generic CSV list) only contain
albums and their artists. They are flagged as ``physical`` so that later
playable providers (streaming services) can be added as new source types
without changing the catalogue model.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from typing import Any
from xml.etree.ElementTree import Element, ParseError

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring

from ..utils import date_value, fold, integer, plain_text, positive
from .base import ConnectorError, Record

VIDEO_KINDS = frozenset({"movie", "tvshow", "episode"})
MUSIC_KINDS = frozenset({"artist", "album", "song"})

# Physical-only collections today. A future streaming provider would be added
# to PLAYABLE_MUSIC_TYPES with its own connector, keeping the same records.
MUSIC_COLLECTION_TYPES = ("discogs", "clz_music", "music_csv")
PLAYABLE_MUSIC_TYPES: tuple[str, ...] = ()
MUSIC_IMPORT_CONTENT = {"discogs": "csv", "clz_music": "xml", "music_csv": "csv"}

# Insert parents before children so that parent keys resolve in one snapshot.
KIND_ORDER = {"artist": 0, "movie": 0, "tvshow": 0, "album": 1, "episode": 2, "song": 2}

_EDITION_NOISE = re.compile(
    r"\s*[\(\[][^\)\]]*(?:remaster|deluxe|edition|expanded|anniversary|bonus|"
    r"version|reissue|mono|stereo|réédition|remasteris)[^\)\]]*[\)\]]\s*",
    re.IGNORECASE,
)
_DISCOGS_SUFFIX = re.compile(r"\s+\(\d+\)$")


def words(value: str | None) -> str:
    return " ".join(re.findall(r"\w+", fold(value or "")))


def artist_key(name: str | None) -> str:
    """Stable folded artist identity; "The Beatles" and "Beatles, The" match."""
    value = _DISCOGS_SUFFIX.sub("", (name or "").strip())
    if match := re.fullmatch(r"(.+),\s*(the|les|le|la|die|der)", value, flags=re.IGNORECASE):
        value = f"{match[2]} {match[1]}"
    key = words(value)
    return re.sub(r"^(the|les) ", "", key)


def album_title_key(title: str | None) -> str:
    return words(_EDITION_NOISE.sub(" ", title or ""))


def music_match_keys(kind: str, title: str | None, metadata: dict[str, Any] | None) -> dict:
    """Synthetic identity used only to reconcile providers (never exposed)."""
    metadata = metadata or {}
    if kind == "artist":
        key = artist_key(title)
        return {"music_title": key} if key else {}
    if kind == "album":
        artist = artist_key(metadata.get("album_artist") or metadata.get("artist"))
        album = album_title_key(title)
        return {"music_title": f"{artist}|{album}"} if artist and album else {}
    return {}


def physical_format(label: str | None) -> str | None:
    """Normalize Discogs/CLZ media descriptions to a short, filterable format."""
    value = fold(label or "").strip()
    if not value:
        return None
    tokens = re.split(r"[,;/]+", value)
    first = re.sub(r"^\d+\s*x\s*", "", tokens[0].strip())
    table = (
        (("sacd", "hybrid sacd"), "SACD"),
        (("blu-ray audio", "blu-ray", "bd-audio"), "Blu-ray Audio"),
        (("dvd-audio", "dvd audio"), "DVD-Audio"),
        (("dvd",), "DVD"),
        (("cd", "cdr", "cd-r", "hdcd", "shm-cd", "blu-spec cd", "cd single", "enhanced cd"), "CD"),
        (("vinyl", "lp", "12\"", "10\"", "7\"", "ep", "33 rpm", "45 rpm", "vinyle"), "Vinyl"),
        (("cassette", "k7", "mc"), "Cassette"),
        (("minidisc", "md"), "MiniDisc"),
        (("file", "digital", "download", "flac", "mp3"), "Digital"),
        (("box set", "boxset", "coffret"), "Box Set"),
    )
    for aliases, name in table:
        if first in aliases or any(first.startswith(alias + " ") for alias in aliases):
            return name
    for aliases, name in table:
        if any(alias in value for alias in aliases if len(alias) > 2):
            return name
    return (label or "").strip().split(",")[0].strip()[:40] or None


def _year(value: Any) -> int | None:
    match = re.search(r"(1[89]\d\d|20\d\d)", str(value or ""))
    return int(match[1]) if match else None


def _duration(value: Any) -> int | None:
    """"4:20", "1:02:03" or seconds."""
    text = str(value or "").strip()
    if not text:
        return None
    if ":" in text:
        total = 0
        for part in text.split(":"):
            if not part.strip().isdigit():
                return None
            total = total * 60 + int(part)
        return total or None
    return integer(text)


def artist_record(name: str, *, sort_name: str | None = None, physical: bool = True) -> Record:
    clean = _DISCOGS_SUFFIX.sub("", name.strip()) or name.strip()
    return Record(
        external_key=f"artist:{artist_key(clean) or clean.casefold()}",
        kind="artist",
        title=clean,
        sort_title=sort_name or None,
        metadata={"physical": physical, "sort_name": sort_name},
    )


def album_record(
    *,
    key: str,
    artist: str,
    title: str,
    year: int | None = None,
    media_format: str | None = None,
    genres: list[str] | None = None,
    label: str | None = None,
    catalog_number: str | None = None,
    barcode: str | None = None,
    user_rating: float | None = None,
    date_added: str | None = None,
    notes: str | None = None,
    tracks: list[dict] | None = None,
    extra: dict[str, Any] | None = None,
    poster: list[str] | None = None,
) -> Record:
    clean_artist = _DISCOGS_SUFFIX.sub("", artist.strip()) or "Unknown artist"
    normalized_format = physical_format(media_format)
    metadata: dict[str, Any] = {
        "artist": clean_artist,
        "album_artist": clean_artist,
        "artists": [clean_artist],
        "physical": True,
        "playable": False,
        "media_format": (media_format or "").strip() or None,
        "label": (label or "").strip() or None,
        "catalog_number": (catalog_number or "").strip() or None,
        "barcode": re.sub(r"\D", "", barcode or "") or None,
        "notes": plain_text(notes) or None,
        "tracks": tracks or [],
        "track_count": len(tracks or []) or None,
        "duration_seconds": sum(t.get("duration_seconds") or 0 for t in tracks or []) or None,
    }
    metadata.update(extra or {})
    return Record(
        external_key=key,
        kind="album",
        title=title.strip() or "Untitled",
        year=year,
        genres=list(dict.fromkeys(g.strip() for g in genres or [] if g and g.strip())),
        formats=[normalized_format] if normalized_format else [],
        user_rating=user_rating,
        date_added=date_added,
        parent_key=f"artist:{artist_key(clean_artist) or clean_artist.casefold()}",
        poster=poster or [],
        metadata=metadata,
    )


def _with_artists(albums: list[Record]) -> list[Record]:
    if not albums:
        raise ConnectorError("The export contains no album.")
    artists: dict[str, Record] = {}
    for album in albums:
        name = album.metadata["album_artist"]
        artist = artist_record(name)
        artists.setdefault(artist.external_key, artist)
    seen: set[str] = set()
    unique = []
    for album in albums:
        # Two identical lines (same release twice) stay distinct editions.
        base, suffix = album.external_key, 2
        while album.external_key in seen:
            album.external_key = f"{base}#{suffix}"
            suffix += 1
        seen.add(album.external_key)
        unique.append(album)
    return [*artists.values(), *unique]


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ConnectorError("The file encoding is not supported.")


def _csv_rows(data: bytes) -> list[dict[str, str]]:
    text = _decode(data)
    if not text.strip():
        raise ConnectorError("The CSV export is empty.")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise ConnectorError("The CSV export has no header row.")
    rows = []
    for row in reader:
        rows.append({fold(str(k or "")).strip(): (v or "").strip() for k, v in row.items() if k})
    return rows


def _pick(row: dict[str, str], *names: str) -> str:
    for name in names:
        if value := row.get(name):
            return value
    return ""


def _stable_key(prefix: str, *parts: str) -> str:
    digest = hashlib.sha1("\x1f".join(fold(p) for p in parts).encode()).hexdigest()[:16]
    return f"{prefix}:{digest}"


def parse_discogs_csv(data: bytes) -> list[Record]:
    """Discogs "Export collection" CSV (Catalog#, Artist, Title, Label, Format...)."""
    rows = _csv_rows(data)
    if rows and "artist" not in rows[0] and "title" not in rows[0]:
        raise ConnectorError("This is not a Discogs collection export (Artist/Title columns).")
    albums = []
    for row in rows:
        title, artist = _pick(row, "title"), _pick(row, "artist")
        if not title:
            continue
        release_id = _pick(row, "release_id", "release id")
        rating = positive(_pick(row, "rating"))
        key = (
            f"album:discogs:{release_id}"
            if release_id.isdigit()
            else _stable_key("album", artist, title, _pick(row, "format"))
        )
        albums.append(
            album_record(
                key=key,
                artist=artist or "Unknown artist",
                title=title,
                year=_year(_pick(row, "released")),
                media_format=_pick(row, "format"),
                label=_pick(row, "label"),
                catalog_number=_pick(row, "catalog#", "catalog #", "catno"),
                user_rating=rating * 2 if rating and rating <= 5 else rating,
                date_added=date_value(_pick(row, "date added")),
                notes=_pick(row, "collection notes", "notes"),
                extra={
                    "discogs_release_id": release_id or None,
                    "collection_folder": _pick(row, "collectionfolder", "folder") or None,
                    "media_condition": _pick(row, "collection media condition") or None,
                    "sleeve_condition": _pick(row, "collection sleeve condition") or None,
                },
            )
        )
    return _with_artists(albums)


def parse_music_csv(data: bytes) -> list[Record]:
    """Generic list: artist, album/title, year, format, label, genre, barcode..."""
    rows = _csv_rows(data)
    columns = set(rows[0]) if rows else set()
    if not columns.intersection({"album", "title", "titre", "album title"}):
        raise ConnectorError("The CSV needs at least an Album (or Title) column.")
    albums = []
    for row in rows:
        title = _pick(row, "album", "album title", "title", "titre")
        artist = _pick(row, "artist", "artiste", "album artist", "interprete", "interprète")
        if not title:
            continue
        media_format = _pick(row, "format", "support", "media", "medium")
        barcode = _pick(row, "barcode", "ean", "upc", "code barre", "code-barres")
        rating = positive(_pick(row, "rating", "note"))
        genres = re.split(r"[;,/]+", _pick(row, "genre", "genres", "style"))
        albums.append(
            album_record(
                key=_stable_key("album", artist, title, media_format, barcode),
                artist=artist or "Unknown artist",
                title=title,
                year=_year(_pick(row, "year", "annee", "année", "released", "date")),
                media_format=media_format,
                genres=genres,
                label=_pick(row, "label", "editeur", "éditeur"),
                catalog_number=_pick(row, "catalog#", "catalog number", "catno", "reference"),
                barcode=barcode,
                user_rating=rating * 2 if rating and rating <= 5 else rating,
                notes=_pick(row, "notes", "comment", "commentaire"),
            )
        )
    return _with_artists(albums)


def _local(tag: str) -> str:
    return re.sub(r"[^a-z0-9]", "", fold(tag.rsplit("}", 1)[-1]))


def _child(node: Element, *names: str) -> Element | None:
    wanted = {re.sub(r"[^a-z0-9]", "", n) for n in names}
    return next((c for c in node if _local(c.tag) in wanted), None)


def _text(node: Element | None, *paths: str) -> str:
    """First non-empty text, accepting CLZ ``<x><displayname>`` wrappers."""
    if node is None:
        return ""
    for path in paths:
        current: Element | None = node
        for part in path.split("/"):
            current = _child(current, part) if current is not None else None
        if current is None:
            continue
        display = _child(current, "displayname")
        value = display.text if display is not None and display.text else current.text
        if value and value.strip():
            return value.strip()
    return ""


def _texts(node: Element | None, container: str, item: str) -> list[str]:
    parent = _child(node, container) if node is not None else None
    if parent is None:
        return []
    result = []
    for child in parent:
        if _local(child.tag) == item:
            if value := _text(child, "displayname", "name") or (child.text or "").strip():
                result.append(value)
    return result


def parse_clz_music_xml(data: bytes) -> list[Record]:
    """CLZ Music (Collectorz) XML export: albums, artists, formats and tracks."""
    try:
        root = fromstring(data, forbid_dtd=True)
    except (ParseError, DefusedXmlException) as exc:
        raise ConnectorError("The CLZ Music XML export is invalid.") from exc
    albums = []
    for node in root.iter():
        if _local(node.tag) not in {"music", "album"} or _child(node, "title") is None:
            continue
        title = _text(node, "title")
        artists = _texts(node, "artists", "artist") or [_text(node, "artist")]
        artist = next((a for a in artists if a), "Unknown artist")
        tracks = []
        for disc_index, disc in enumerate(
            [d for d in node.iter() if _local(d.tag) == "disc"] or [node], start=1
        ):
            container = _child(disc, "tracks")
            for position, track in enumerate(
                [t for t in (container if container is not None else []) if _local(t.tag) == "track"],
                start=1,
            ):
                if track_title := _text(track, "title"):
                    tracks.append(
                        {
                            "title": track_title,
                            "disc": disc_index,
                            "track": integer(_text(track, "position", "index")) or position,
                            "duration_seconds": _duration(_text(track, "length", "duration")),
                            "artist": next(iter(_texts(track, "artists", "artist")), None),
                        }
                    )
        identity = _text(node, "id", "bpid", "index", "clzid")
        media_format = _text(node, "format", "media")
        albums.append(
            album_record(
                key=f"album:clz:{identity}" if identity else _stable_key("album", artist, title, media_format),
                artist=artist,
                title=title,
                year=_year(_text(node, "releasedate/year", "releaseyear", "year", "releasedate")),
                media_format=media_format,
                genres=_texts(node, "genres", "genre") or [_text(node, "genre")],
                label=next(iter(_texts(node, "labels", "label")), "") or _text(node, "label"),
                catalog_number=_text(node, "catalognumber", "catalognr", "catno"),
                barcode=_text(node, "upc", "barcode", "ean"),
                user_rating=positive(_text(node, "myrating", "rating")),
                notes=_text(node, "notes", "comments"),
                tracks=tracks,
                extra={"clz_id": identity or None},
            )
        )
    return _with_artists(albums)


def parse_music_collection(data: bytes, source_type: str) -> list[Record]:
    if source_type == "discogs":
        return parse_discogs_csv(data)
    if source_type == "clz_music":
        return parse_clz_music_xml(data)
    if source_type == "music_csv":
        return parse_music_csv(data)
    raise ConnectorError("Unsupported music collection type.")
