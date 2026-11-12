"""Collectorz/CLZ Movies XML export, including the datasetversion 15 shape."""

import hashlib
import re
from datetime import UTC, date, datetime
from xml.etree.ElementTree import Element, ParseError

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring

from ..media_formats import technical_formats
from ..people import category, youtube_url
from ..utils import formats, identifiers, integer, plain_text, positive
from .base import ConnectorError, Record


def text(node: Element, path: str) -> str:
    return (node.findtext(path) or "").strip()


def display_list(node: Element, path: str) -> list[str]:
    return [str(x.text).strip() for x in node.findall(path) if x.text and x.text.strip()]


def timestamp(node: Element, path: str) -> str | None:
    try:
        return datetime.fromtimestamp(int(text(node, path)), UTC).isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        return None


def credits(node: Element) -> list[dict]:
    result = []
    for group in ("cast", "crew"):
        container = node.find(group)
        if container is None:
            continue
        for order, member in enumerate(container):
            name = next(
                (
                    text(member, p)
                    for p in (
                        "person/displayname",
                        "person/name",
                        "actor/displayname",
                        "displayname",
                        "name",
                    )
                    if text(member, p)
                ),
                "",
            )
            if not name:
                name = " ".join(filter(None, (text(member, "firstname"), text(member, "lastname"))))
            if not name:
                continue
            role = next(
                (
                    text(member, p)
                    for p in (
                        "role/displayname",
                        "role",
                        "character",
                        "job/displayname",
                        "job",
                        "crewwork/displayname",
                        "function/displayname",
                    )
                    if text(member, p)
                ),
                "",
            )
            imdb = next(
                (
                    text(member, p)
                    for p in ("imdbnr", "imdbid", "person/imdbnr", "imdburl")
                    if text(member, p)
                ),
                "",
            )
            imdb_match = re.search(r"nm\d+", imdb)
            photos = [
                text(member, p)
                for p in (
                    "thumbnail",
                    "image",
                    "picture",
                    "thumb",
                    "photo",
                    "person/thumbnail",
                    "person/image",
                )
                if text(member, p)
            ]
            result.append(
                {
                    "name": name,
                    "role": role or None,
                    "order": order,
                    "category": "cast" if group == "cast" else category(role or "crew"),
                    "imdb_id": imdb_match[0] if imdb_match else None,
                    "photos": photos,
                }
            )
    return result


def parse_clz(data: bytes) -> list[Record]:
    try:
        root = fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except (DefusedXmlException, ParseError, ValueError) as exc:
        # XML parsing errors are deliberately kept out of client output.
        raise ConnectorError("Invalid or unsafe CLZ XML export.") from exc
    movies = root.findall("./data/movieinfo/movielist/movie")
    if root.tag != "collectorz" or root.find("./data/movieinfo/movielist") is None:
        raise ConnectorError("Expected a CLZ Movies/Collectorz export with a movielist.")
    result = []
    seen = set()
    for node in movies:
        title = text(node, "title")
        if not title:
            raise ConnectorError("A CLZ entry has no title; the previous catalogue was retained.")
        key = text(node, "hash")
        if not key:
            identity = "|".join(text(node, p) for p in ("bpmediaid", "bpmovieid", "upc", "index"))
            key = hashlib.sha256((identity + "|" + title).encode()).hexdigest()
        if key in seen:
            raise ConnectorError(
                "Duplicate CLZ entry identifier; the previous catalogue was retained."
            )
        seen.add(key)
        tv = node.find("istvseries")
        is_tv = tv is not None and (
            tv.get("boolvalue") == "1" or (tv.text or "").casefold() == "yes"
        )
        history = node.find("seenhistory")
        viewings = [] if history is None else history.findall("seendate")
        seen_dates = []
        for viewing in viewings:
            try:
                seen_dates.append(
                    date(
                        int(text(viewing, "date/year")),
                        int(text(viewing, "date/month")),
                        int(text(viewing, "date/day")),
                    ).isoformat()
                )
            except ValueError:
                # A viewing without a valid date still counts as watched.
                continue
        # CLZ season discs are separate physical editions of a single series.
        # Never use the shared series IMDb ID to infer a season number.
        explicit_series = text(node, "series/displayname")
        season_match = re.search(
            r"(?:^|[\s:—-])(?:season|saison)\s*0*(\d{1,3})\b",
            title, flags=re.IGNORECASE,
        )
        physical_season = int(season_match.group(1)) if is_tv and season_match else None
        series_title = explicit_series or (
            title[:season_match.start()].rstrip(" :-—").strip()
            if physical_season is not None else ""
        )
        if not series_title:
            physical_season = None
        record = Record(
            external_key=key,
            kind="tvshow" if is_tv else "movie",
            title=series_title if physical_season is not None else title,
            original_title=text(node, "originaltitle") or None,
            sort_title=series_title if physical_season is not None else (text(node, "sorttitle") or title),
            year=integer(text(node, "releasedate/year/displayname")),
            plot=plain_text(text(node, "plot")),
            runtime_minutes=integer(text(node, "runtime")),
            rating=positive(text(node, "imdbrating")),
            user_rating=positive(text(node, "myrating")),
            identifiers=identifiers({"imdb": text(node, "imdbnr")}),
            genres=display_list(node, "genres/genre/displayname"),
            formats=sorted(
                set(formats(display_list(node, "formats/format/displayname")))
                | set(
                    technical_formats(
                        display_list(node, "formats/format/displayname")
                        + display_list(node, "audiotracks/audiotrack/displayname")
                        + display_list(node, "tags/tag/displayname")
                    )
                )
            ),
            collections=[
                v
                for v in (text(node, "collection/displayname"), text(node, "boxset/displayname"))
                if v
            ],
            date_added=timestamp(node, "addeddate/timestamp"),
            # An absent history is unknown; an explicitly empty history is unwatched.
            watched=bool(viewings) if history is not None else None,
            play_count=len(viewings) if history is not None else None,
            last_played=max(seen_dates, default=None),
            poster=[
                v
                for v in (
                    text(node, "coverfrontlarge"),
                    text(node, "coverfront"),
                    text(node, "coverfrontdefault"),
                )
                if v
            ],
            metadata={
                "people": credits(node),
                "trailers": list(
                    dict.fromkeys(
                        url
                        for link in node.findall("links/link")
                        if (url := youtube_url(text(link, "url")))
                    )
                ),
                "seen_dates": sorted(seen_dates),
                "barcode": text(node, "upc") or None,
                "edition": text(node, "edition/displayname") or text(node, "edition") or None,
                "disc_count": integer(text(node, "nrdiscs")),
                "quantity": integer(text(node, "quantity")),
                "collection_status": text(node, "collectionstatus"),
                "boxset": text(node, "boxset/displayname") or None,
                "regions": display_list(node, "regions/region/displayname"),
                "audio_tracks": display_list(node, "audiotracks/audiotrack/displayname"),
                "subtitles": display_list(node, "subtitles/subtitle/displayname"),
                "studios": display_list(node, "studios/studio/displayname"),
                "tags": display_list(node, "tags/tag/displayname"),
                "country": text(node, "country/displayname") or None,
                "language": text(node, "language/displayname") or None,
                "purchase_date": text(node, "purchasedate/displaydate") or None,
                "series_title": series_title or None,
                "physical_season": physical_season,
                "physical_title": title if physical_season is not None else None,
                "physical_episode_count": len(node.findall("episodes/episode")) if physical_season is not None else 0,
                "physical_year": (
                    integer(text(node, "releasedate/year/displayname"))
                    if physical_season is not None else None
                ),
                "clz_movie_id": text(node, "bpmovieid"),
                "clz_media_id": text(node, "bpmediaid"),
            },
        )
        result.append(record)
    return result
