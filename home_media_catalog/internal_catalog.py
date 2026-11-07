"""Search film metadata and physical editions without assuming that an EAN is a TMDB ID."""
import re

import httpx

from .connectors.base import ConnectorError
from .enrichment import tmdb_connection

BARCODE_RE = re.compile(r"^[0-9]{8}$|^[0-9]{12,14}$")


def valid_barcode(value: str) -> bool:
    if not BARCODE_RE.fullmatch(value):
        return False
    # GTIN-8 / UPC-A / EAN-13 / GTIN-14 check digit, right-to-left.
    digits = [int(c) for c in value]
    total = sum(d * (3 if i % 2 else 1)
                for i, d in enumerate(reversed(digits[:-1])))
    return (10 - total % 10) % 10 == digits[-1]


async def find_tmdb(query: str, token: str) -> list[dict]:
    if len(query.strip()) < 2:
        return []
    async with tmdb_connection(token) as get:
        data = await get("search/multi", query=query.strip(), language="fr-FR",
                         include_adult="false", page=1)
    results = []
    for item in data.get("results", []):
        category = item.get("media_type")
        if category not in {"movie", "tv"} or not isinstance(item.get("id"), int):
            continue
        poster = item.get("poster_path")
        if not isinstance(poster, str) or not re.fullmatch(r"/[A-Za-z0-9_.-]{1,150}", poster):
            poster = None
        release = item.get("release_date") or item.get("first_air_date") or ""
        results.append({
            "kind": "movie" if category == "movie" else "tvshow",
            "title": str(item.get("title") or item.get("name") or "")[:300],
            "original_title": str(item.get("original_title") or item.get("original_name") or "")[:300],
            "year": int(release[:4]) if re.match(r"^[0-9]{4}", release) else None,
            "tmdb_id": str(item["id"]),
            "plot": str(item.get("overview") or "")[:5000],
            "rating": item.get("vote_average"),
            "poster_path": poster,
        })
        if len(results) == 12:
            break
    return results


async def lookup_disc_barcode(value: str) -> dict:
    if not valid_barcode(value):
        raise ValueError("Invalid EAN/UPC/GTIN barcode or check digit.")
    try:
        async with httpx.AsyncClient(timeout=8, trust_env=False) as client:
            response = await client.get(
                "https://api.upcitemdb.com/prod/trial/lookup",
                params={"upc": value}, headers={"Accept": "application/json"}
            )
        if response.status_code == 404:
            return {"barcode": value, "found": False, "title": None, "items": []}
        if response.status_code == 429:
            raise ConnectorError("Barcode service rate limit reached. Enter the title manually.")
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ConnectorError("Barcode lookup service is unavailable. Search TMDB by title.") from exc
    items = [
        {"title": str(item.get("title") or "")[:300],
         "brand": str(item.get("brand") or "")[:120],
         "category": str(item.get("category") or "")[:120]}
        for item in body.get("items", [])[:5] if isinstance(item, dict)
    ]
    return {"barcode": value, "found": bool(items),
            "title": items[0]["title"] if items else None, "items": items}
