"""International GTIN lookup for discs, with independent provider fallbacks.

Works are identified through TMDB separately: a barcode identifies an edition,
not an IMDb or TMDB film. Public providers cannot cover every retail release.
"""
import logging
import os
import re
import time
from xml.etree.ElementTree import ParseError

import httpx
from defusedxml.ElementTree import fromstring

from .enrichment import tmdb_connection

_LOG = logging.getLogger(__name__)
BARCODE_RE = re.compile(r"^(?:[0-9]{8}|[0-9]{12,14})$")
SRU_NS = "{http://www.loc.gov/zing/srw/}"
DC_NS = "{http://purl.org/dc/elements/1.1/}"
_CACHE: dict[str, tuple[float, dict]] = {}


def _valid_gtin(value: str) -> bool:
    if not BARCODE_RE.fullmatch(value):
        return False
    digits = [int(c) for c in value]
    total = sum(d * (3 if i % 2 == 0 else 1)
                for i, d in enumerate(reversed(digits[:-1])))
    return (10 - total % 10) % 10 == digits[-1]


def expand_upce(value: str) -> str | None:
    """Expand the short US UPC-E representation into equivalent UPC-A (GTIN-12)."""
    if len(value) != 8 or not value.isascii() or not value.isdigit() or value[0] not in "01":
        return None
    system, a, b, c, d, e, f, check = value
    if f in "012":
        manufacturer, product = a + b + f + "00", "00" + c + d + e
    elif f == "3":
        manufacturer, product = a + b + c + "00", "000" + d + e
    elif f == "4":
        manufacturer, product = a + b + c + d + "0", "0000" + e
    else:
        manufacturer, product = a + b + c + d + e, "0000" + f
    expanded = system + manufacturer + product + check
    return expanded if _valid_gtin(expanded) else None


def valid_barcode(value: str) -> bool:
    """Accept GS1 GTIN-8/12/13/14 plus checksum-valid US UPC-E symbols."""
    return _valid_gtin(value) or expand_upce(value) is not None


def barcode_variants(value: str) -> tuple[str, ...]:
    """Equivalent representations of the same product for exact-match lookup."""
    aliases = [value]
    upca = expand_upce(value)
    if upca:
        aliases.extend([upca, "0" + upca])
    if len(value) == 12:
        aliases.append("0" + value)
    elif len(value) == 13 and value.startswith("0"):
        aliases.append(value[1:])
    if len(value) == 14 and value.startswith("00"):
        aliases.append(value[2:])
    return tuple(dict.fromkeys(aliases))

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


def _safe_product_image(item: dict) -> str | None:
    images = item.get("images")
    choices = (
        images if isinstance(images, list)
        else [item.get("image_url"), item.get("image")] 
    )
    for image in choices[:5]:
        if isinstance(image, str) and len(image) <= 2048 and image.startswith("https://"):
            return image
    return None


def _result(value: str, source: str, rows: list[dict]) -> dict | None:
    rows = [row for row in rows if isinstance(row.get("title"), str) and row["title"].strip()]
    if not rows:
        return None
    return {
        "barcode": value, "found": True, "title": rows[0]["title"],
        "source": source, "items": rows[:5],
        "image_url": rows[0].get("image_url"),
    }


async def _lookup_upcitemdb(value: str, client: httpx.AsyncClient) -> dict | None:
    response = await client.get(
        "https://api.upcitemdb.com/prod/trial/lookup",
        params={"upc": value}, headers={"Accept": "application/json"},
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError("Invalid UPCitemdb response")
    rows = [
        {
            "title": str(item.get("title") or "")[:300],
            "brand": str(item.get("brand") or "")[:120],
            "category": str(item.get("category") or "")[:120],
            "image_url": _safe_product_image(item),
        }
        for item in body.get("items", [])[:5] if isinstance(item, dict)
    ]
    return _result(value, "upcitemdb", rows)


async def _lookup_bnf(value: str, client: httpx.AsyncClient) -> dict | None:
    """BnF supports EANs for video discs; it is one regional fallback, not a gate."""
    if len(value) != 13:
        return None
    response = await client.get(
        "https://catalogue.bnf.fr/api/SRU",
        params={
            "version": "1.2",
            "operation": "searchRetrieve",
            "query": f'bib.ean any "{value}"',
            "recordSchema": "dublincore",
            "maximumRecords": "5",
        },
        headers={"Accept": "application/xml"},
    )
    response.raise_for_status()
    if len(response.content) > 512_000:
        raise ValueError("BnF response too large")
    root = fromstring(response.content)
    rows = []
    for entry in root.iter(SRU_NS + "recordData"):
        titles = [(node.text or "").strip() for node in entry.iter(DC_NS + "title")]
        descriptions = [(node.text or "").strip() for node in entry.iter(DC_NS + "description")]
        # The identifier is a product identifier, not a title keyword.
        ean_descriptions = [desc for desc in descriptions if "code à barres commercial" in desc.casefold()]
        if ean_descriptions and not any(value in desc for desc in ean_descriptions):
            continue
        if titles and titles[0]:
            rows.append({"title": titles[0][:300], "brand": "BnF", "category": "Disc"})
    return _result(value, "bnf", rows)


async def _lookup_barcodelookup(value: str, client: httpx.AsyncClient, key: str) -> dict | None:
    """Optional globally indexed provider; requires a private server-side key."""
    response = await client.get(
        "https://api.barcodelookup.com/v3/products",
        params={"barcode": value, "formatted": "y", "key": key},
        headers={"Accept": "application/json"},
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError("Invalid Barcode Lookup response")
    rows = [
        {
            "title": str(item.get("title") or "")[:300],
            "brand": str(item.get("brand") or "")[:120],
            "category": str(item.get("category") or "")[:120],
            "image_url": _safe_product_image(item),
        }
        for item in body.get("products", [])[:5] if isinstance(item, dict)
    ]
    return _result(value, "barcodelookup", rows)


async def lookup_disc_barcode(value: str) -> dict:
    if not valid_barcode(value):
        raise ValueError("Invalid EAN/UPC/GTIN barcode or check digit.")
    now_time = time.monotonic()
    cached = _CACHE.get(value)
    if cached and cached[0] > now_time:
        return cached[1]

    attempted: list[str] = []
    errors: list[str] = []
    answer = None
    variants = barcode_variants(value)
    async with httpx.AsyncClient(timeout=9, trust_env=False, follow_redirects=False) as client:
        # World-wide product index first, including US UPC and non-French EAN.
        attempted.append("upcitemdb")
        try:
            answer = await _lookup_upcitemdb(value, client)
            # UPC-E is often stored using its expanded UPC-A equivalent.
            if answer is None and expand_upce(value):
                answer = await _lookup_upcitemdb(expand_upce(value), client)
        except (httpx.HTTPError, ValueError) as exc:
            errors.append("upcitemdb")
            _LOG.info("UPCitemdb barcode lookup unavailable: %s", type(exc).__name__)

        # French national bibliography is additive: non-French discs continue
        # to work via the global provider. Normalize UPC-A -> EAN-13 here.
        if answer is None:
            ean13 = next((v for v in variants if len(v) == 13), None)
            if ean13:
                attempted.append("bnf")
                try:
                    answer = await _lookup_bnf(ean13, client)
                except (httpx.HTTPError, ParseError, ValueError) as exc:
                    errors.append("bnf")
                    _LOG.info("BnF barcode lookup unavailable: %s", type(exc).__name__)

        # Optional paid provider expands coverage without shipping keys in iOS.
        key = os.getenv("BARCODELOOKUP_API_KEY", "").strip()
        if answer is None and key:
            attempted.append("barcodelookup")
            try:
                answer = await _lookup_barcodelookup(value, client, key)
            except (httpx.HTTPError, ValueError) as exc:
                errors.append("barcodelookup")
                _LOG.info("Barcode Lookup unavailable: %s", type(exc).__name__)

    if answer is None:
        answer = {
            "barcode": value, "found": False, "title": None,
            "source": None, "items": [],
        }
    answer["providers_checked"] = attempted
    answer["providers_unavailable"] = errors
    # Do not conflate "not indexed" with "service down"; the UI can show both.
    answer["status"] = (
        "found" if answer["found"] else
        "unavailable" if errors and len(errors) == len(attempted) else
        "not_found"
    )
    # A negative cache protects free provider quotas; transient outages expire quickly.
    if len(_CACHE) >= 500:
        _CACHE.clear()
    _CACHE[value] = (now_time + (3600 if answer["found"] else 300), answer)
    return answer


def is_multi_season_boxset(title: str) -> bool:
    """Ambiguous complete-series boxes must not silently become season 1."""
    return bool(re.search(
        r"(?i)\b(?:complete\s+series|complete\s+seasons?|int[eé]grale|"
        r"seasons?\s*\d{1,3}\s*[-–/&+]\s*\d{1,3}|"
        r"saisons?\s*\d{1,3}\s*[-–/&+]\s*\d{1,3})\b",
        title or "",
    ))


# Only explicit season labels are considered. A barcode identifies a release,
# never a season on its own; box sets such as "Seasons 1-3" remain unassigned.
_SEASON_LABEL = re.compile(
    r"(?i)(?:^|[\s:–—-]+)(?:season|saison|staffel|temporada|s)\s*[:.]?\s*0*(\d{1,3})"
    r"\b(?!\s*[-–]\s*\d)"
)
_DISC_SUFFIX = re.compile(
    r"(?i)\b(?:blu[- ]?ray(?:\s*3d)?|ultra\s*hd|dvd|4k|uhd|"
    r"complete\s+edition|édition\s+complète|collector(?:'s)?\s+edition)\b"
)


def physical_season_in_title(title: str) -> tuple[int | None, str]:
    """Extract explicit season and optional series title from a disc label."""
    if not title:
        return None, ""
    # Do not convert a multi-season box set to a single owned season.
    if re.search(
        r"(?i)\b(?:seasons|saisons|season|saison)\s+\d{1,3}\s*[-–/&+]\s*\d{1,3}",
        title,
    ):
        return None, ""
    matches = list(_SEASON_LABEL.finditer(title))
    if len(matches) != 1:
        return None, ""
    season = int(matches[0].group(1))
    base = title[:matches[0].start()].strip(" :-–—,()[]")
    base = _DISC_SUFFIX.sub("", base).strip(" :-–—,()[]")
    return season, base


async def tmdb_series_season_profile(tmdb_id: str, token: str) -> dict:
    """Return season cardinality for safe single-season disc inference.

    A bare series title or barcode is not evidence that a physical disc covers
    season 1. Infer it only when TMDB confirms exactly one regular season.
    """
    if not re.fullmatch(r"[0-9]{1,24}", tmdb_id):
        raise ValueError("Invalid TMDB TV-series identifier.")
    async with tmdb_connection(token) as get:
        data = await get(f"tv/{tmdb_id}", language="fr-FR")
    seasons = data.get("seasons")
    regular = [
        row for row in seasons
        if isinstance(row, dict) and isinstance(row.get("season_number"), int)
        and row["season_number"] > 0
    ] if isinstance(seasons, list) else []
    count = data.get("number_of_seasons")
    # Never infer if TMDB returned contradictory or incomplete season data.
    single = count == 1 and len(regular) == 1 and regular[0]["season_number"] == 1
    entry = regular[0] if single else {}
    return {
        "number_of_seasons": count if isinstance(count, int) else None,
        "single_season": single,
        "episode_count": entry.get("episode_count") if isinstance(entry.get("episode_count"), int) else None,
        "poster_path": entry.get("poster_path") if isinstance(entry.get("poster_path"), str)
                       and re.fullmatch(r"/[A-Za-z0-9_.-]{1,150}", entry["poster_path"]) else None,
    }


async def tmdb_physical_season(tmdb_id: str, season: int, token: str) -> dict:
    """Fetch provider-backed season art and episode count, never a show poster."""
    if not re.fullmatch(r"[0-9]{1,24}", tmdb_id) or not 0 <= season <= 10000:
        raise ValueError("Invalid TMDB season identifier.")
    async with tmdb_connection(token) as get:
        data = await get(f"tv/{tmdb_id}/season/{season}", language="fr-FR")
    path = data.get("poster_path")
    if not isinstance(path, str) or not re.fullmatch(r"/[A-Za-z0-9_.-]{1,150}", path):
        path = None
    episodes = data.get("episodes")
    return {
        "season": season,
        "poster_path": path,
        "episode_count": len(episodes) if isinstance(episodes, list) else None,
    }
