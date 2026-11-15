"""CSV and DVD Profiler imports. Normalize at the same Record boundary as XML."""
import csv
import io
import re
from xml.etree.ElementTree import ParseError

from defusedxml.ElementTree import fromstring
from defusedxml.common import DefusedXmlException

from .base import ConnectorError, Record
from .collection_xml import name, value, values, watched_value

CSV_TYPES = {"icollect", "bluraycom", "moviebuddy", "letterboxd", "imdb", "mymovies", "filmotech"}


def number(raw, maximum=None):
    try:
        result = float(str(raw).replace(",", "."))
        return result if 0 <= result and (maximum is None or result <= maximum) else None
    except (ValueError, TypeError):
        return None


def year(raw):
    match = re.search(r"\b(?:18|19|20)\d{2}\b", raw)
    return int(match[0]) if match else None


def ids(imdb, tmdb):
    result = {}
    match = re.search(r"tt\d{5,12}", imdb, re.I)
    if match:
        result["imdb"] = match[0].lower()
    elif imdb.isdigit() and len(imdb) <= 12:
        result["imdb"] = "tt" + imdb.zfill(7)
    if tmdb.isdigit() and int(tmdb) > 0:
        result["tmdb"] = tmdb
    return result


def split(raw):
    return list(dict.fromkeys(v.strip() for v in re.split(r"[,;|]", raw) if v.strip()))


def media_formats(raw):
    return [
        "4K Ultra HD Blu-ray" if name(v) in {"4k", "4kuhd", "uhd", "ultrahd", "4kbluray", "ultrahdbluray"}
        else "Blu-ray" if name(v) in {"bluray", "bd"}
        else "DVD" if name(v) == "dvd" else v for v in split(raw)
    ]


def parse_csv(data, provider, filename):
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    if "\0" in text:
        raise ConnectorError("Unsupported CSV encoding.")
    title_keys = ["name", "title"] if provider == "letterboxd" else [
        "title", "movietitle", "name", "titrevf", "titre", "localtitle", "titlelocal", "titrevo"
    ]
    rows = None
    for delimiter in (",", ";", "\t"):
        try:
            candidate = [r for r in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
                         if any(v.strip() for v in r)]
        except csv.Error:
            continue
        if not candidate or len(candidate[0]) < 2:
            continue
        headers = [name(v) for v in candidate[0]]
        keys = set(headers)
        if not keys.intersection(title_keys) or any(len(r) != len(headers) for r in candidate[1:]):
            continue
        if provider == "imdb" and "const" not in keys:
            continue
        if provider == "letterboxd" and not keys.intersection({"letterboxduri", "imdbid", "tmdbid"}):
            continue
        if provider not in {"imdb", "letterboxd"} and not keys.intersection({
            "year", "releaseyear", "productionyear", "releasedate", "annee", "barcode", "barcodenumber",
            "upc", "ean", "codebarre", "format", "mediatype", "mediaformat", "support", "id",
            "itemid", "collectionid", "contenttype", "movieid", "imdbid", "collectionnumber", "type"
        }):
            continue
        rows = candidate[1:]
        break
    if not rows:
        raise ConnectorError("No supported CSV movie records. The previous collection was retained.")
    records, seen = [], set()
    tracking = provider in {"imdb", "letterboxd"}
    for row in rows:
        fields = {}
        for key, val in zip(headers, row):
            if key and val.strip():
                fields.setdefault(key, val.strip())
        def get(*keys):
            return next((fields[k] for k in keys if k in fields), "")
        title = get(*title_keys)
        if not title:
            raise ConnectorError("A CSV record has no title.")
        raw_type = name(get("contenttype", "titletype", "movietype", "kind", "type"))
        if "episode" in raw_type or (provider == "imdb" and raw_type in {"videogame", "podcastseries", "musicvideo"}):
            continue
        kind = "tvshow" if raw_type in {"tvshow", "tvseries", "tvminiseries", "television", "series", "miniseries"} else "movie"
        released = year(get("year", "releaseyear", "productionyear", "annee", "datepublished", "releasedate"))
        barcode = get("barcode", "barcodenumber", "upc", "ean", "codebarre", "upcean", "upcbarcode")
        imdb = get("const", "imdbid", "imdb", "imdburl", "imdbidentifier")
        tmdb = get("tmdbid", "tmdbidentifier")
        fmt = get("format", "mediaformat", "mediatype", "support", "discformat", "media", "formats")
        edition = get("edition", "editiontype", "packaging")
        key = imdb if provider == "imdb" else get("letterboxduri", "imdbid", "tmdbid") if provider == "letterboxd" else get(
            "id", "itemid", "icollectid", "collectionid", "movieid", "uniqueid", "collectionnumber", "uuid")
        key = key or "|".join((title, str(released or ""), barcode, fmt, edition))
        if key in seen:
            if tracking:
                continue
            raise ConnectorError("Duplicate physical edition. Previous collection retained.")
        seen.add(key)
        if provider == "mymovies" and not fmt:
            fmt = get("type")
        if provider == "bluraycom" and not fmt:
            detected = []
            for fields_, label in [
                (("4kuhd", "4k", "4kdiscs", "uhddiscs", "4kbluray"), "4K Ultra HD Blu-ray"),
                (("bluray3d", "3d", "3ddiscs"), "Blu-ray 3D"),
                (("bluray", "bluraydiscs", "bldiscs", "bd"), "Blu-ray"),
                (("dvd", "dvddiscs"), "DVD"),
            ]:
                if (number(get(*fields_)) or 0) > 0:
                    detected.append(label)
            if not detected and re.search(r"\b4K(?: UHD)?$", title, re.I):
                detected.append("4K Ultra HD Blu-ray")
            fmt = "|".join(detected)
        watched = watched_value(get("watched", "seen", "viewed", "vu", "filmvu"))
        if provider == "letterboxd" and (filename.lower() in {"watched.csv", "ratings.csv"} or get("watcheddate")):
            watched = True
        rating = number(get("rating10", "rating") if provider == "letterboxd" else
                        get("yourrating", "yourated", "myrating", "personalrating", "userrating"), 10)
        if provider == "letterboxd" and not get("rating10") and rating is not None:
            rating = rating * 2 if rating <= 5 else None
        runtime = number(get("runtimemins", "runtime", "runningtime", "duration", "duree"))
        formats = [] if tracking else media_formats(fmt)
        metadata = {} if tracking else {
            "barcode": barcode, "edition": edition, "formats": formats,
            "country": get("country", "countryoforigin", "pays"),
        }
        records.append(Record(
            external_key=key, kind=kind, title=title, original_title=get("originaltitle", "titrevo") or None,
            year=released, identifiers=ids(imdb, tmdb), plot=get("description", "synopsis", "plot", "overview", "resume"),
            runtime_minutes=int(runtime) if runtime is not None else None,
            rating=number(get("imdbrating"), 10), user_rating=rating, watched=watched,
            genres=split(get("genres", "genre")), formats=formats,
            collections=[] if tracking else split(get("collection", "collectionname")), metadata=metadata,
        ))
    if not records:
        raise ConnectorError("No supported movie or series entries.")
    return records


def parse_dvdprofiler(data):
    try:
        root = fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except (DefusedXmlException, ParseError, ValueError) as exc:
        raise ConnectorError("Invalid or unsafe DVD Profiler XML.") from exc
    if name(root.tag) != "collection":
        raise ConnectorError("Expected DVD Profiler Collection XML.")
    records, seen = [], set()
    for node in root:
        if name(node.tag) != "dvd":
            continue
        title = value(node, "Title")
        released = year(value(node, "ProductionYear", "Year"))
        barcode = re.sub(r"\D", "", value(node, "UPC"))
        key = value(node, "ID", "CollectionNumber") or "|".join((title, str(released or 0), value(node, "UPC")))
        if not title or key in seen:
            raise ConnectorError("Invalid or duplicate DVD Profiler entry.")
        seen.add(key)
        formats = [label for field, label in (("UltraHD", "4K Ultra HD Blu-ray"), ("BluRay", "Blu-ray"), ("DVD", "DVD"), ("HDDVD", "HD DVD"))
                   if watched_value(value(node, "MediaTypes/" + field)) is True]
        records.append(Record(external_key=key, kind="movie", title=title, year=released,
                              original_title=value(node,"OriginalTitle") or None, plot=value(node,"Overview"),
                              identifiers=ids(value(node,"IMDB","IMDBID"), ""), formats=formats,
                              genres=values(node,"Genres/Genre"),
                              metadata={"barcode": barcode, "edition":value(node,"DistTrait"),"formats":formats}))
    if not records:
        raise ConnectorError("No DVD Profiler entries found.")
    return records
