"""Filmotech and My Movies collection exports, normalized at the connector boundary."""

import hashlib
import re
from xml.etree.ElementTree import ParseError

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring

from ..media_formats import technical_formats
from ..utils import date_value, fold, formats, identifiers, integer, plain_text, positive
from .base import ConnectorError, Record

COLLECTION_TYPES = ("clz", "mymovies", "filmotech")
SOURCE_TYPES = ("kodi", "internal", *COLLECTION_TYPES)


def name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", fold(value.rsplit("}", 1)[-1]))


def value(node, *paths: str) -> str:
    """Case/namespace independent paths, without searching unrelated descendants."""
    for path in paths:
        nodes = [node]
        for part in path.split("/"):
            nodes = [child for parent in nodes for child in parent if name(child.tag) == name(part)]
        for child in nodes:
            if child.text and child.text.strip():
                return child.text.strip()
    return ""


def values(node, *paths: str) -> list[str]:
    result = []
    for path in paths:
        nodes = [node]
        for part in path.split("/"):
            nodes = [child for parent in nodes for child in parent if name(child.tag) == name(part)]
        for child in nodes:
            if child.text and child.text.strip():
                result.extend(v.strip() for v in re.split(r"[\r\n;,/]+", child.text) if v.strip())
    return list(dict.fromkeys(result))


def watched_value(raw: str) -> bool | None:
    normalized = fold(raw.strip())
    if normalized in {"true", "yes", "oui", "1", "vu", "watched"}:
        return True
    if normalized in {"false", "no", "non", "0", "non vu", "unwatched"}:
        return False
    return None


def people(node, provider: str) -> list[dict]:
    result = []
    if provider == "mymovies":
        for person in node.iter():
            if name(person.tag) != "person":
                continue
            person_name = value(person, "Name") or " ".join(
                filter(None, (value(person, "FirstName"), value(person, "LastName")))
            )
            if not person_name:
                continue
            kind = person.get("Type", person.get("type", "")) or value(person, "Type")
            result.append(
                {
                    "name": person_name,
                    "role": value(person, "Role") or None,
                    "category": "director" if kind in {"2", "Director"} else "cast",
                    "order": len(result),
                }
            )
    else:
        for field, category in (("Acteurs", "cast"), ("Realisateurs", "director")):
            for person_name in values(node, field):
                result.append({"name": person_name, "category": category, "order": len(result)})
    return result


def parse_collection(data: bytes, provider: str) -> list[Record]:
    if provider not in {"filmotech", "mymovies"}:
        raise ConnectorError("Unsupported collection export.")
    try:
        root = fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except (DefusedXmlException, ParseError, ValueError) as exc:
        raise ConnectorError(f"Invalid or unsafe {provider} XML export.") from exc
    root_name = name(root.tag)
    if provider == "mymovies":
        if root_name not in {"titles", "mymovies"}:
            raise ConnectorError("Expected a My Movies collection XML export (Titles).")
        entries = [child for child in root if name(child.tag) == "title"]
        if root_name == "mymovies":
            entries += [
                child
                for group in root
                if name(group.tag) == "titles"
                for child in group
                if name(child.tag) == "title"
            ]
    else:
        if root_name not in {"filmotech", "films", "listefilms", "filmotechxml"}:
            raise ConnectorError("Expected a Filmotech XML export containing Film entries.")
        entries = [child for child in root.iter() if name(child.tag) == "film"]
    # Do not turn an unrecognized/truncated document into a destructive empty snapshot.
    if not entries:
        raise ConnectorError("No movie entries found. The previous collection was retained.")
    records, seen = [], set()
    for node in entries:
        movie_title = value(node, "LocalTitle", "TitreVF", "Titre", "OriginalTitle", "TitreVO")
        if not movie_title:
            raise ConnectorError(
                "A collection entry has no title; the previous collection was retained."
            )
        year = integer(value(node, "ProductionYear", "Annee", "Year"))
        barcode = value(node, "Barcode", "CodeBarre", "CodeBarres", "EAN")
        key = value(node, "ID", "TitleID", "CollectionNumber") or node.get("ID") or node.get("id")
        if not key:
            identity = "|".join(
                (
                    movie_title,
                    str(year or ""),
                    barcode,
                    value(node, "Type", "Support"),
                    value(node, "Edition"),
                )
            )
            key = hashlib.sha256(identity.encode()).hexdigest()
        if key in seen:
            raise ConnectorError(
                "Duplicate collection entry identifier; the previous collection was retained."
            )
        seen.add(key)
        imdb = value(node, "IMDB", "IMDBID", "IdentifiantIMDB")
        imdb_match = re.search(r"tt\d+", imdb)
        raw_formats = values(node, "Type", "MediaType", "Support", "Formats/Format")
        raw_audio = values(node, "Audio", "AudioTracks/AudioTrack/Type")
        watched = watched_value(value(node, "Watched", "PersonalData/Watched", "Vu", "DejaVu"))
        records.append(
            Record(
                external_key=key,
                kind="movie",
                title=movie_title,
                original_title=value(node, "OriginalTitle", "TitreVO") or None,
                sort_title=value(node, "SortTitle", "TitreTri") or movie_title,
                year=year,
                plot=plain_text(value(node, "Description", "Synopsis")),
                runtime_minutes=integer(value(node, "RunningTime", "Duree", "Runtime")),
                rating=positive(value(node, "Rating", "IMDBRating")),
                user_rating=positive(value(node, "PersonalData/Rating", "PersonalRating", "Note")),
                identifiers=identifiers(
                    {
                        "imdb": imdb_match[0] if imdb_match else imdb,
                        "tmdb": value(node, "TMDB", "TMDBID"),
                    }
                ),
                genres=values(node, "Genres/Genre", "Genre"),
                formats=sorted(
                    set(formats(raw_formats)) | set(technical_formats(raw_formats + raw_audio))
                ),
                collections=values(node, "Collection", "BoxSet/Title", "Coffret"),
                date_added=date_value(
                    value(node, "Added", "PersonalData/Added", "DateEntree", "DateAjout")
                ),
                last_played=date_value(
                    value(node, "LastWatched", "PersonalData/LastWatched", "DateVu")
                ),
                watched=watched,
                play_count=(1 if watched else 0) if watched is not None else None,
                poster=[
                    v
                    for v in [
                        value(
                            node,
                            "Covers/FrontMedium",
                            "Covers/Front",
                            "Cover/Front",
                            "FrontCover",
                            "Affiche",
                            "Poster",
                        )
                    ]
                    if v
                ],
                metadata={
                    "people": people(node, provider),
                    "barcode": barcode or None,
                    "edition": value(node, "Edition", "EditionType") or None,
                    "disc_count": integer(value(node, "Discs", "NombreSupports", "NbSupport")),
                    "audio_tracks": raw_audio,
                    "subtitles": values(node, "Sous-Titres", "Subtitles/Subtitle"),
                    "country": value(node, "Country", "Pays") or None,
                    "collection_provider": provider,
                    "mymovies_remote_title_id": (
                        value(node, "ID", "TitleID") or node.get("ID") or node.get("id")
                    )
                    if provider == "mymovies"
                    else None,
                    "mymovies_title": movie_title if provider == "mymovies" else None,
                },
            )
        )
    return records
