"""Per-installation streaming discovery and user state, without an aggregator account.

Discovery is a replaceable provider boundary. TMDB only supplies availability,
not a platform's actual viewing statistics. Official charts are explicitly
identified and otherwise replaced by TMDB popularity (never mislabeled Top 10).
All persistent data belongs to the existing self-hosted catalogue database.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import math
import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .auth import authenticated, writable

TMDB = "https://api.themoviedb.org/3"
NETFLIX_COUNTRIES = "https://www.netflix.com/tudum/top10/data/all-weeks-countries.tsv"
PROVIDERS = {
    "netflix": ("Netflix", (8,)),
    "disney_plus": ("Disney+", (337,)),
    "prime_video": ("Prime Video", (119,)),
    "apple_tv_plus": ("Apple TV+", (350,)),
    "hbo_max": ("HBO Max", (1899, 384)),
    "paramount_plus": ("Paramount+", (531,)),
}
# Maximum retained in-process snapshots. Never share them across HMC installs.
_CACHE: dict[str, tuple[float, Any]] = {}


class StreamingStateRow(BaseModel):
    key: str = Field(pattern=r"^[a-zA-Z0-9:_.-]{1,128}$")
    payload: dict[str, Any] = Field(default_factory=dict)
    modified_at: str = Field(max_length=40)
    deleted: bool = False


class StreamingSyncInput(BaseModel):
    changes: list[StreamingStateRow] = Field(default_factory=list, max_length=1000)


class StreamingLinkImportInput(BaseModel):
    tmdb_id: int = Field(ge=1, le=999999999)
    kind: str = Field(pattern=r"^(movie|tv)$")
    service: str = Field(pattern=r"^[a-z_]{2,40}$")
    url: str = Field(min_length=15, max_length=2000)
    season: int | None = Field(default=None, ge=0, le=10000)
    episode: int | None = Field(default=None, ge=1, le=10000)


_LINK_HOSTS = {
    "netflix": ("netflix.com",),
    "disney_plus": ("disneyplus.com",),
    # Amazon retail-region ASIN links are not portable Prime Video links.
    # Only platform-native HTTPS details are accepted for sync/import.
    "prime_video": ("primevideo.com",),
    "apple_tv_plus": ("tv.apple.com",),
    "hbo_max": ("max.com", "hbomax.com"),
    "paramount_plus": ("paramountplus.com",),
}


def _official_link(url: str, service: str) -> tuple[str, str]:
    """Validate a user-shared official destination without scraping a provider."""
    parsed = urlsplit(url.strip())
    host = (parsed.hostname or "").lower()
    allowed = _LINK_HOSTS.get(service, ())
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port
            or not allowed or not any(host == name or host.endswith("." + name)
                                       for name in allowed)
            or parsed.path in ("", "/") or len(url) > 2000):
        raise HTTPException(422, "Invalid official streaming URL")
    components = [item for item in parsed.path.split("/") if item]
    if "search" in components:
        raise HTTPException(422, "Search URLs are not direct content links")
    provider_id = ""
    if service == "netflix":
        if len(components) < 2 or components[0] != "title" or not components[1].isdigit():
            raise HTTPException(422, "Expected a Netflix title URL")
        provider_id = components[1]
    elif service == "prime_video" and "detail" in components:
        index = components.index("detail") + 1
        provider_id = components[index] if index < len(components) else ""
    elif service == "apple_tv_plus":
        provider_id = next((value for value in components if value.startswith("umc.cmc.")), "")
        if not provider_id:
            raise HTTPException(422, "Expected an Apple TV content URL")
    return parsed.geturl(), provider_id


class StreamingEpisodeImportInput(BaseModel):
    tmdb_id: int = Field(ge=1, le=999999999)
    episodes: list[dict[str, Any]] | None = Field(default=None, max_length=1500)



def _stamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("Time zone is required")
        return parsed.astimezone(UTC)
    except ValueError as error:
        raise HTTPException(422, "Invalid streaming timestamp") from error


def _ensure_schema(store: Any) -> None:
    with store.connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS streaming_state (
            key TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            modified_at TEXT NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0
        )""")


def _merge(store: Any, incoming: list[StreamingStateRow]) -> list[dict[str, Any]]:
    # Per-key LWW reconciliation, deterministic at equal timestamps, tombstones
    # retained. Missing keys never mean deletion, so offline clients can merge.
    _ensure_schema(store)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for entry in incoming:
            if len(json.dumps(entry.payload, ensure_ascii=False)) > 16384:
                raise HTTPException(413, "Streaming state record too large")
            timestamp = _stamp(entry.modified_at).isoformat(timespec="microseconds")
            payload = json.dumps(entry.payload, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"))
            current = db.execute(
                "SELECT payload,modified_at,deleted FROM streaming_state WHERE key=?",
                (entry.key,),
            ).fetchone()
            # At identical timestamps, all devices must pick the same record.
            candidate = (timestamp, int(entry.deleted), payload)
            if current is not None:
                existing = (current["modified_at"], int(current["deleted"]), current["payload"])
                if candidate <= existing:
                    continue
            db.execute("""INSERT INTO streaming_state(key,payload,modified_at,deleted)
                VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                payload=excluded.payload,modified_at=excluded.modified_at,
                deleted=excluded.deleted""",
                (entry.key, payload, timestamp, int(entry.deleted)))
        rows = db.execute(
            "SELECT key,payload,modified_at,deleted FROM streaming_state ORDER BY key"
        ).fetchall()
    return [{"key": row["key"], "payload": json.loads(row["payload"]),
             "modified_at": row["modified_at"], "deleted": bool(row["deleted"])}
            for row in rows]


def _title_metadata(raw: dict[str, Any], kind: str) -> dict[str, Any]:
    """Project TMDB film/TV details to a bounded, shared web/native contract.

    These are public reference metadata, not watched state or service ownership.
    Missing translations are left empty rather than fabricated.
    """
    date = raw.get("first_air_date" if kind == "tv" else "release_date") or ""
    credits = raw.get("credits") or {}
    if not isinstance(credits, dict):
        credits = {}

    def people(entries: Any, role_key: str, limit: int) -> list[dict[str, Any]]:
        if not isinstance(entries, list):
            return []
        result = []
        for person in entries:
            if not isinstance(person, dict):
                continue
            identifier, name = person.get("id"), person.get("name")
            if not isinstance(identifier, int) or not isinstance(name, str):
                continue
            profile = person.get("profile_path")
            result.append({
                "id": identifier, "name": name[:160],
                "role": str(person.get(role_key) or "")[:160],
                "profile_path": profile if isinstance(profile, str)
                and re.fullmatch(r"/[A-Za-z0-9_.-]{1,150}", profile) else None,
            })
            if len(result) >= limit:
                break
        return result

    cast = people(credits.get("cast"), "character", 30)
    crew = people(credits.get("crew"), "job", 80)
    # A useful concise list: directors and principal creative roles first.
    priority = {"Director": 0, "Creator": 1, "Writer": 2, "Screenplay": 3,
                "Executive Producer": 4, "Producer": 5}
    crew.sort(key=lambda row: priority.get(row["role"], 20))
    genres = [
        row["name"][:100] for row in (raw.get("genres") or [])[:30]
        if isinstance(row, dict) and isinstance(row.get("name"), str)
    ]
    seasons = [
        {
            "number": season["season_number"],
            "title": str(season.get("name") or "")[:200],
            "episode_count": int(season.get("episode_count") or 0),
            "air_date": str(season.get("air_date") or "")[:30],
            "poster_path": season.get("poster_path"),
        }
        for season in (raw.get("seasons") or [])[:60]
        if isinstance(season, dict) and isinstance(season.get("season_number"), int)
        and isinstance(season.get("episode_count"), int)
        and 0 <= season["season_number"] <= 10000
    ]
    runtimes = raw.get("episode_run_time" if kind == "tv" else "runtime")
    if isinstance(runtimes, list):
        runtimes = next((v for v in runtimes if isinstance(v, int) and v > 0), None)
    if not isinstance(runtimes, int) or not 0 < runtimes <= 10000:
        runtimes = None
    return {
        "id": raw.get("id"),
        "kind": kind,
        "title": raw.get("name" if kind == "tv" else "title") or "",
        "original_title": raw.get("original_name" if kind == "tv"
                                  else "original_title") or "",
        "overview": raw.get("overview") or "",
        "year": int(date[:4]) if re.match(r"^\d{4}", date) else None,
        "poster_path": raw.get("poster_path"),
        "backdrop_path": raw.get("backdrop_path"),
        "rating": raw.get("vote_average") or 0,
        "genres": genres,
        "cast": cast,
        "crew": crew[:16],
        "seasons": seasons,
        "runtime_minutes": runtimes,
        "status": raw.get("status") or "",
    }


def _streaming_edition_uuid(key: str) -> str:
    """Use the same deterministic edition UUID as iOS' editionUUID(identity)."""
    digest = bytearray(hashlib.sha256(("hmc-streaming:" + key).encode()).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x40
    digest[8] = (digest[8] & 0x3F) | 0x80
    value = digest.hex()
    return "-".join((value[:8], value[8:12], value[12:16],
                     value[16:20], value[20:]))


def _reconcile_streaming_catalogue(store: Any, rows: list[dict[str, Any]]) -> int:
    """Materialize committed Streaming heart entries into the server catalogue.

    A device using local-first storage can synchronize its Streaming state
    before its asynchronous catalogue upload completes. This idempotent
    reconciliation ensures web /media sees those titles in the meantime.
    The deterministic edition key is identical to iOS, so the later regular
    catalogue journal upload does not create a second internal edition.
    Never remove catalogue entries just because a heart row was tombstoned.
    """
    wanted = []
    for row in rows:
        if row["deleted"] or not re.fullmatch(r"item:(movie|tv):[0-9]{1,24}", row["key"]):
            continue
        data = row["payload"]
        if data.get("status") not in {"saved", "pending"}:
            continue
        kind = row["key"].split(":")[1]
        tmdb_id = row["key"].split(":")[2]
        title = data.get("title")
        if (not isinstance(title, str) or not title.strip() or len(title.strip()) > 300
                or data.get("tmdb_id") != tmdb_id or data.get("kind") != kind):
            continue
        service_names = data.get("services", "")
        if not isinstance(service_names, str):
            continue
        services = [key for key in dict.fromkeys(service_names.split(","))
                    if key in PROVIDERS]
        year = data.get("year")
        if not isinstance(year, str) or not year.isdecimal() or not 1870 <= int(year) <= 2200:
            year = None
        poster = data.get("poster_path")
        if not isinstance(poster, str) or not re.fullmatch(r"/[A-Za-z0-9_.-]{1,150}", poster):
            poster = None
        backdrop = data.get("backdrop_path")
        if not isinstance(backdrop, str) or not re.fullmatch(r"/[A-Za-z0-9_.-]{1,150}", backdrop):
            backdrop = None
        original = data.get("original_title")
        if not isinstance(original, str) or not 1 <= len(original.strip()) <= 300:
            original = title
        overview = data.get("overview")
        if not isinstance(overview, str) or len(overview) > 5000:
            overview = None
        try:
            rating = float(data.get("rating", ""))
            if not math.isfinite(rating) or not 0 <= rating <= 10:
                rating = None
        except (ValueError, TypeError):
            rating = None
        wanted.append({
            "id": _streaming_edition_uuid(row["key"]),
            "kind": "tvshow" if kind == "tv" else "movie",
            "tmdb_id": tmdb_id, "title": title.strip(),
            "original_title": original.strip(),
            "plot": overview, "rating": rating,
            "year": int(year) if year is not None else None,
            "poster_path": poster, "backdrop_path": backdrop,
            "format": "", "streaming_services": services,
        })
    if not wanted:
        return 0
    with store.connect() as db:
        known = {row["external_key"] for row in db.execute(
            "SELECT external_key FROM records WHERE source_id='internal'"
        )}
    created = 0
    for data in wanted:
        if data["id"] in known:
            continue
        store.save_internal_media(data)
        created += 1
    return created


def _provider_keys(selection: str) -> list[str]:
    """Parse selected subscriptions; reject unknown and empty scopes."""
    keys = [part.strip() for part in selection.split(",")]
    if not keys or any(key not in PROVIDERS for key in keys) or len(keys) > len(PROVIDERS):
        raise HTTPException(422, "Unsupported streaming provider selection")
    return list(dict.fromkeys(keys))


def _provider_ids(selection: str) -> str:
    keys = _provider_keys(selection)
    return "|".join(str(provider_id) for key in keys for provider_id in PROVIDERS[key][1])


def _availability_keys(results: dict[str, Any], selection: str, region: str) -> list[str]:
    """Only paid-subscription (flatrate) offers qualify; exclude rentals/buy offers."""
    scoped = results.get("results", {}).get(region, {})
    available = {row.get("provider_id") for row in scoped.get("flatrate", [])}
    return [key for key in _provider_keys(selection)
            if any(ident in available for ident in PROVIDERS[key][1])]


async def _availability(token: str, kind: str, tmdb_id: int, selection: str,
                        region: str) -> list[str]:
    response = await _tmdb(token, f"/{kind}/{tmdb_id}/watch/providers", {})
    return _availability_keys(response, selection, region)


async def _tmdb(token: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
    if not token:
        raise HTTPException(503, "Configure a TMDB API token on this server")
    params = {**params, "api_key": token} if not token.startswith("eyJ") else params
    headers = {"Authorization": "Bearer " + token} if token.startswith("eyJ") else {}
    async with httpx.AsyncClient(timeout=14, follow_redirects=False) as client:
        try:
            response = await client.get(TMDB + path, params=params, headers=headers)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise HTTPException(502, "TMDB streaming discovery unavailable") from error


async def _discover(token: str, service: str, kind: str, genre: int | None,
                    page: int, region: str, sort: str) -> dict[str, Any]:
    params: dict[str, Any] = {
        "watch_region": region, "with_watch_providers": _provider_ids(service),
        "with_watch_monetization_types": "flatrate",
        "language": "fr-FR" if region == "FR" else "en-US",
        "sort_by": "vote_average.desc" if sort == "rating" else "popularity.desc",
        "page": page,
    }
    if sort == "rating":
        params["vote_count.gte"] = 100
    elif sort == "recent":
        # Recently released titles currently available, NOT newly added to the service.
        params["sort_by"] = "first_air_date.desc" if kind == "tv" else "primary_release_date.desc"
        params["first_air_date.lte" if kind == "tv" else "primary_release_date.lte"] = datetime.now(UTC).date().isoformat()
    if genre is not None:
        params["with_genres"] = genre
    result = await _tmdb(token, "/discover/" + ("tv" if kind == "tv" else "movie"), params)
    return {
        "items": [_item(row, kind) for row in result.get("results", []) if row.get("id")],
        "page": result.get("page", page),
        "total_pages": min(result.get("total_pages", 1), 500),
        "source": "tmdb",
        "ranking_type": "tmdb_recent" if sort == "recent" else (
            "tmdb_rating" if sort == "rating" else "tmdb_popularity"),
    }


def _item(row: dict[str, Any], kind: str) -> dict[str, Any]:
    date = row.get("first_air_date" if kind == "tv" else "release_date") or ""
    return {
        "id": int(row["id"]), "kind": kind,
        "title": row.get("name" if kind == "tv" else "title") or "",
        "original_title": row.get("original_name" if kind == "tv" else "original_title") or "",
        "overview": row.get("overview") or "",
        "year": int(date[:4]) if re.match(r"^\d{4}", date) else None,
        "poster_path": row.get("poster_path"),
        "backdrop_path": row.get("backdrop_path"),
        "rating": row.get("vote_average") or 0,
        "genre_ids": row.get("genre_ids") or [],
    }


async def _official_netflix(token: str, region: str, kind: str) -> list[dict[str, Any]]:
    # Netflix publishes historical weekly ranks by country as TSV, not an API.
    # Download at most once per installation and 12h; never treat it as daily.
    cache_key = "netflix-countries-" + region
    cached = _CACHE.get(cache_key)
    if cached is not None and time.monotonic() - cached[0] < 12 * 3600:
        rows = cached[1]
    else:
        async with httpx.AsyncClient(timeout=45, follow_redirects=False) as client:
            response = await client.get(NETFLIX_COUNTRIES)
            response.raise_for_status()
            if len(response.content) > 75_000_000:
                raise ValueError("Netflix chart file exceeds safety limit")
            source = csv.DictReader(io.StringIO(response.content.decode("utf-8-sig")),
                                    delimiter="\t")
            rows = []
            newest: str | None = None
            for row in source:
                if row.get("country_iso2", "").upper() != region:
                    continue
                week = row.get("week") or ""
                if newest is None or week > newest:
                    newest = week
                    rows = [row]
                elif week == newest:
                    rows.append(row)
            _CACHE[cache_key] = (time.monotonic(), rows)
    category = "TV" if kind == "tv" else "Films"
    selected = sorted(
        (row for row in rows if row.get("category", "").casefold() == category.casefold()),
        key=lambda row: int(row.get("weekly_rank") or 99),
    )[:10]
    enriched: list[dict[str, Any]] = []
    for row in selected:
        title = (row.get("show_title") or "").strip()
        if not title:
            continue
        try:
            result = await _tmdb(token, "/search/" + kind, {
                "query": title, "language": "fr-FR", "page": 1,
            })
            candidate = (result.get("results") or [None])[0]
            if candidate:
                record = _item(candidate, kind)
                record["rank"] = int(row["weekly_rank"])
                enriched.append(record)
        except (HTTPException, ValueError, KeyError):
            continue
    return enriched


async def _search(token: str, selection: str, phrase: str, kind: str,
                  region: str) -> dict[str, Any]:
    """Search film/series titles, then validate regional SVOD availability.

    TMDB /search does not accept provider filters. Validation through each
    title's /watch/providers avoids showing rentals or unavailable titles.
    Bound the candidate pool and the concurrent lookups for mobile typing.
    """
    _provider_keys(selection)
    media_kinds = ("movie", "tv") if kind == "both" else (kind,)
    requests = await asyncio.gather(*(
        _tmdb(token, "/search/" + media_kind, {
            "query": phrase, "page": 1, "language": "fr-FR",
            "include_adult": "false",
        }) for media_kind in media_kinds
    ))
    candidates: list[tuple[str, dict[str, Any]]] = []
    for media_kind, response in zip(media_kinds, requests, strict=True):
        candidates.extend((media_kind, row) for row in
                          response.get("results", [])[:15] if row.get("id"))
    limit = asyncio.Semaphore(6)

    async def check(media_kind: str, row: dict[str, Any]) -> dict[str, Any] | None:
        async with limit:
            try:
                available = await _availability(
                    token, media_kind, int(row["id"]), selection, region
                )
            except (HTTPException, ValueError, KeyError):
                return None
        if not available:
            return None
        record = _item(row, media_kind)
        record["service_keys"] = available
        return record

    checked = await asyncio.gather(*(check(k, row) for k, row in candidates))
    matches = [record for record in checked if record is not None]
    # Preserve TMDB search relevance order; vote averages are not query scores.
    return {"items": matches, "source": "tmdb", "ranking_type": "search",
            "page": 1, "total_pages": 1, "region": region}


def build_streaming_router(store: Any, settings: Any) -> APIRouter:
    router = APIRouter(prefix="/api/v1/streaming", tags=["Streaming"])

    @router.get("/titles/{kind}/{tmdb_id}/details",
                dependencies=[Depends(authenticated)])
    async def title_details(
        kind: str, tmdb_id: int,
        language: str = Query("fr-FR", pattern=r"^[a-z]{2}-[A-Z]{2}$"),
    ):
        """Fetch a full streaming title record, including credits and seasons.

        Uses the same TMDB identity as the iOS heart journal. Credentials stay
        on the NAS; the browser never contacts TMDB's authenticated API.
        """
        if kind not in {"movie", "tv"} or tmdb_id < 1 or tmdb_id > 999999999:
            raise HTTPException(422, "Invalid TMDB title identity")
        cache_key = f"tmdb-title-details:{language}:{kind}:{tmdb_id}"
        cached = _CACHE.get(cache_key)
        if cached is not None and time.monotonic() - cached[0] < 6 * 3600:
            return cached[1]
        raw = await _tmdb(
            settings.tmdb_api_token.strip(), f"/{kind}/{tmdb_id}",
            {"language": language, "append_to_response": "credits"},
        )
        result = _title_metadata(raw, kind)
        if len(_CACHE) >= 160:
            # Bound global, per-installation metadata cache.
            oldest = next(iter(_CACHE))
            _CACHE.pop(oldest, None)
        _CACHE[cache_key] = (time.monotonic(), result)
        return result

    @router.get("/providers", dependencies=[Depends(authenticated)])
    async def providers(region: str = Query("FR", pattern=r"^[A-Z]{2}$")):
        token = settings.tmdb_api_token.strip()
        logos: dict[int, str] = {}
        if token:
            for kind in ("movie", "tv"):
                try:
                    response = await _tmdb(token, "/watch/providers/" + kind, {
                        "watch_region": region, "language": "fr-FR",
                    })
                    logos.update({int(row["provider_id"]): row.get("logo_path")
                                  for row in response.get("results", []) if row.get("logo_path")})
                except HTTPException:
                    pass
        return {"region": region, "providers": [
            {"key": key, "name": details[0],
             "tmdb_ids": list(details[1]),
             "logo_path": next((logos.get(ident) for ident in details[1]
                                if logos.get(ident)), None)}
            for key, details in PROVIDERS.items()
        ]}

    @router.get("/genres", dependencies=[Depends(authenticated)])
    async def genres(kind: str = Query("movie", pattern=r"^(movie|tv)$")):
        response = await _tmdb(settings.tmdb_api_token.strip(), "/genre/" + kind + "/list",
                               {"language": "fr-FR"})
        return {"genres": response.get("genres", [])}

    @router.get("/discover", dependencies=[Depends(authenticated)])
    async def discover(
        service: str, kind: str = Query("movie", pattern=r"^(movie|tv)$"),
        genre: int | None = Query(None, ge=1), page: int = Query(1, ge=1, le=500),
        region: str = Query("FR", pattern=r"^[A-Z]{2}$"),
        sort: str = Query("popularity", pattern=r"^(popularity|rating|recent)$"),
    ):
        return await _discover(settings.tmdb_api_token.strip(), service, kind, genre, page,
                               region, sort)

    @router.get("/search", dependencies=[Depends(authenticated)])
    async def search(
        q: str = Query(min_length=2, max_length=120),
        service: str = Query(min_length=1, max_length=150),
        kind: str = Query("both", pattern=r"^(movie|tv|both)$"),
        region: str = Query("FR", pattern=r"^[A-Z]{2}$"),
    ):
        return await _search(settings.tmdb_api_token.strip(),
                             service, q.strip(), kind, region)

    @router.get("/availability/{kind}/{tmdb_id}",
                dependencies=[Depends(authenticated)])
    async def availability(
        kind: str, tmdb_id: int, service: str,
        region: str = Query("FR", pattern=r"^[A-Z]{2}$"),
    ):
        if kind not in {"movie", "tv"} or tmdb_id < 1:
            raise HTTPException(422, "Invalid TMDB title identity")
        return {"service_keys": await _availability(
            settings.tmdb_api_token.strip(), kind, tmdb_id, service, region
        )}

    @router.get("/top", dependencies=[Depends(authenticated)])
    async def top(service: str, kind: str = Query("movie", pattern=r"^(movie|tv)$"),
                  region: str = Query("FR", pattern=r"^[A-Z]{2}$")):
        _provider_ids(service)
        if _provider_keys(service) == ["netflix"]:
            try:
                official = await _official_netflix(settings.tmdb_api_token.strip(), region, kind)
                if len(official) >= 5:
                    return {"items": official, "ranking_type": "official_weekly",
                            "source": "netflix", "region": region}
            except (httpx.HTTPError, ValueError, KeyError, HTTPException):
                pass
        result = await _discover(settings.tmdb_api_token.strip(), service, kind, None,
                                 1, region, "popularity")
        return {"items": result["items"][:10], "ranking_type": "tmdb_popularity",
                "source": "tmdb", "region": region}

    @router.post("/series/{media_id}/episodes/import", dependencies=[Depends(writable)])
    async def import_series_episodes(media_id: str, body: StreamingEpisodeImportInput):
        """Materialize TMDB reference episodes under a canonical catalogue TV show.

        Episodes carry no playable file; personal watched overrides use the
        existing /media/{id}/personal and /episodes/watch endpoints.
        """
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", media_id):
            raise HTTPException(422, "Invalid media ID")
        detail = await asyncio.to_thread(store.detail, media_id)
        if detail is None or detail.get("kind") != "tvshow":
            raise HTTPException(404, "Unknown TV series")
        known = detail.get("identifiers", {}).get("tmdb") or []
        if known and str(body.tmdb_id) not in [str(value) for value in known]:
            raise HTTPException(409, "TMDB identity mismatch")
        if body.episodes is not None:
            try:
                return await asyncio.to_thread(
                    store.import_streaming_episodes, media_id, body.tmdb_id, body.episodes
                )
            except ValueError as error:
                raise HTTPException(422, str(error)) from error
        token = settings.tmdb_api_token.strip()
        series = await _tmdb(token, f"/tv/{body.tmdb_id}", {"language": "fr-FR"})
        numbers = [
            season["season_number"]
            for season in series.get("seasons", [])
            if isinstance(season, dict)
            and isinstance(season.get("season_number"), int)
            and 0 <= season["season_number"] <= 10000
        ][:60]
        limit = asyncio.Semaphore(5)

        async def get_season(number: int) -> list[dict[str, Any]]:
            async with limit:
                data = await _tmdb(token, f"/tv/{body.tmdb_id}/season/{number}",
                                   {"language": "fr-FR"})
            return [
                {"season": number, "episode": item.get("episode_number"),
                 "tmdb_id": item.get("id"),
                 "title": item.get("name") or f"Episode {item.get('episode_number')}",
                 "overview": item.get("overview") or "",
                 "air_date": item.get("air_date") or "",
                 "still_path": item.get("still_path")}
                for item in data.get("episodes", []) if isinstance(item, dict)
            ]

        results = await asyncio.gather(*(get_season(n) for n in numbers))
        episodes = [entry for season in results for entry in season][:1500]
        try:
            return await asyncio.to_thread(
                store.import_streaming_episodes, media_id, body.tmdb_id, episodes
            )
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @router.post("/links/import", dependencies=[Depends(writable)])
    def import_provider_link(body: StreamingLinkImportInput):
        """Resolve an official shared URL locally on the NAS and sync to iOS.

        This deliberately does not infer Netflix/Disney/etc IDs from TMDB IDs.
        Episodes have optional independent links; series links remain fallback.
        """
        if (body.season is None) != (body.episode is None):
            raise HTTPException(422, "Season and episode must be supplied together")
        if body.season is not None and body.kind != "tv":
            raise HTTPException(422, "Episode links require a TV series")
        if body.service not in PROVIDERS:
            raise HTTPException(422, "Unsupported streaming provider")
        url, provider_id = _official_link(body.url, body.service)
        suffix = (f":s{body.season}e{body.episode}"
                  if body.season is not None else "")
        key = f"link:{body.kind}:{body.tmdb_id}:{body.service}{suffix}"
        timestamp = datetime.now(UTC).isoformat(timespec="microseconds")
        row = StreamingStateRow(
            key=key, modified_at=timestamp,
            payload={"url": url, "provider_id": provider_id,
                     "source": "official_share_link"})
        _merge(store, [row])
        return {"key": key, "url": url, "provider_id": provider_id}

    @router.get("/state", dependencies=[Depends(authenticated)])
    def read_state():
        return {"rows": _merge(store, [])}

    @router.post("/state/sync", dependencies=[Depends(writable)])
    def sync_state(body: StreamingSyncInput):
        merged = _merge(store, body.changes)
        _reconcile_streaming_catalogue(store, merged)
        return {"rows": merged}

    return router
