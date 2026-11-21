"""Per-installation streaming discovery and user state, without an aggregator account.

Discovery is a replaceable provider boundary. TMDB only supplies availability,
not a platform's actual viewing statistics. Official charts are explicitly
identified and otherwise replaced by TMDB popularity (never mislabeled Top 10).
All persistent data belongs to the existing self-hosted catalogue database.
"""
from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import time
from datetime import UTC, datetime
from typing import Any

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


def _provider_ids(key: str) -> str:
    if key not in PROVIDERS:
        raise HTTPException(422, "Unsupported streaming provider")
    return "|".join(str(v) for v in PROVIDERS[key][1])


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


def build_streaming_router(store: Any, settings: Any) -> APIRouter:
    router = APIRouter(prefix="/api/v1/streaming", tags=["Streaming"])

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

    @router.get("/top", dependencies=[Depends(authenticated)])
    async def top(service: str, kind: str = Query("movie", pattern=r"^(movie|tv)$"),
                  region: str = Query("FR", pattern=r"^[A-Z]{2}$")):
        _provider_ids(service)
        if service == "netflix":
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

    @router.get("/state", dependencies=[Depends(authenticated)])
    def read_state():
        return {"rows": _merge(store, [])}

    @router.post("/state/sync", dependencies=[Depends(writable)])
    def sync_state(body: StreamingSyncInput):
        return {"rows": _merge(store, body.changes)}

    return router
