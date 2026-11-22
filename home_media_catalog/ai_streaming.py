"""Personalized streaming recommendations using the existing TMDB discovery boundary.

The server owns the watched/rating profile and the AI provider. TMDB supplies
regional SVOD candidates; no complete TMDB corpus or provider credentials are
persisted. A model can refine relevance, but never assert streaming availability.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from collections import defaultdict
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

from .ai_rerank import rerank_candidates
from .streaming import _availability, _merge, _provider_ids, _provider_keys, _tmdb, _item
from .watch_progress import effective_series_status

# Public TMDB metadata only; personal viewing histories are not cached.
_TMDB_CACHE: dict[str, tuple[float, dict]] = {}
_AVAILABILITY_CACHE: dict[str, tuple[float, list[str]]] = {}


class StreamingAIInput(BaseModel):
    prompt: str = Field(min_length=3, max_length=500)
    kind: Literal["tv", "movie", "both"] = "tv"
    services: list[str] = Field(min_length=1, max_length=9)
    region: str = Field(default="FR", pattern=r"^[A-Z]{2}$")
    # Locally pending changes must not appear as recommendations before sync.
    excluded_ids: list[str] = Field(default_factory=list, max_length=2000)
    previous_prompt: str | None = Field(default=None, max_length=500)


def _number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) and 0 <= result <= 10 else None
    except (ValueError, TypeError):
        return None


def _identity(kind: str, tmdb_id: Any) -> str | None:
    raw = str(tmdb_id)
    if kind not in ("tv", "movie") or not raw.isdecimal() or not 0 < int(raw) < 1000000000:
        return None
    return f"{kind}:{int(raw)}"


def _build_profile(catalogue_rows: list[dict], journal: list[dict]) -> dict[str, Any]:
    """Merge canonical catalogue and independent streaming preferences by TMDB ID.

    A streaming personal override wins over the catalogue. Never equate an
    unknown watched state with a definite unseen state. Any recorded episode
    progress marks a series as started, not entirely unseen.
    """
    works: dict[str, dict] = {}
    for row in catalogue_rows:
        identity = _identity("tv" if row["kind"] == "tvshow" else row["kind"], row["tmdb_id"])
        if identity is None:
            continue
        entry = works.setdefault(identity, {
            "id": identity, "rating": None, "seen": False, "title": "", "genres": [],
        })
        rating = _number(row.get("user_rating"))
        if rating is not None:
            entry["rating"] = rating
        entry["seen"] |= row.get("watch_status") in ("watched", "in_progress")
        entry["title"] = entry["title"] or (row.get("title") or "")
        try:
            genres = json.loads(row.get("genres") or "[]")
            if isinstance(genres, list):
                entry["genres"] = [str(g)[:60] for g in genres[:20]]
        except (ValueError, TypeError):
            pass

    states = {row["key"]: row["payload"] for row in journal
              if not row["deleted"] and isinstance(row["payload"], dict)}
    for key, payload in states.items():
        if not key.startswith("personal:"):
            continue
        identity = key[len("personal:"):]
        match = re.fullmatch(r"(tv|movie):(\d{1,9})", identity)
        if not match or int(match[2]) == 0:
            continue
        entry = works.setdefault(identity, {
            "id": identity, "rating": None, "seen": False, "title": "", "genres": [],
        })
        if "rating" in payload or payload.get("rating_cleared") == "true":
            entry["rating"] = _number(payload.get("rating"))
        if payload.get("watched") in ("true", "false"):
            entry["seen"] = payload["watched"] == "true"
        if match[1] == "tv":
            inventory = states.get("inventory:" + identity)
            status = effective_series_status(payload, inventory)
            if status in ("watched", "in_progress"):
                entry["seen"] = True
            if payload.get("watched_episode_snapshot"):
                entry["seen"] = True
            try:
                overrides = json.loads(payload.get("episode_overrides") or "{}")
                if isinstance(overrides, dict) and any(v is True for v in overrides.values()):
                    entry["seen"] = True
            except (ValueError, TypeError):
                pass

    for key, payload in states.items():
        if key.startswith("item:") and payload.get("title"):
            identity = key[len("item:"):]
            if identity in works and not works[identity]["title"]:
                works[identity]["title"] = str(payload["title"])[:300]
    for entry in works.values():
        # Ratings represent consumption, even in older catalogues whose
        # watched flag was never synchronized.
        if entry["rating"] is not None:
            entry["seen"] = True
    positive = sorted(
        (w for w in works.values() if w["rating"] is not None and w["rating"] >= 7),
        key=lambda w: -w["rating"],
    )
    negative = [w for w in works.values()
                if w["rating"] is not None and w["rating"] <= 5]
    negative.sort(key=lambda w: w["rating"])
    unrated = [w for w in works.values() if w["seen"] and w["rating"] is None]
    return {
        "works": works, "seen": {key for key, w in works.items() if w["seen"]},
        "positive": positive, "negative": negative,
        "seeds": (positive[:6] or unrated[:5]),
        "rated_count": sum(w["rating"] is not None for w in works.values()),
    }


def _catalogue_profile(store: Any) -> dict[str, Any]:
    with store.connect() as db:
        rows = db.execute(
            "SELECT i.value AS tmdb_id, w.kind, w.title, w.genres, "
            "w.user_rating, w.watch_status "
            "FROM identifiers i JOIN works w ON w.id=i.work_id "
            "WHERE i.namespace='tmdb' AND w.kind IN ('movie','tvshow')"
        ).fetchall()
    return _build_profile([dict(row) for row in rows], _merge(store, []))


def _language_hint(text: str) -> str | None:
    query = text.casefold()
    for pattern, code in (
        (r"\b(coréen\w*|korean\w*|k.drama\w*)\b", "ko"),
        (r"\b(japonais\w*|japanese\w*)\b", "ja"),
        (r"\b(espagnol\w*|spanish\w*)\b", "es"),
        (r"\b(français\w*|french\w*)\b", "fr"),
        (r"\b(anglais\w*|english\w*|britannique\w*)\b", "en"),
    ):
        if re.search(pattern, query):
            return code
    return None


def _score(row: dict, affinity: dict[int, float], phrase: str) -> float:
    genres = row.get("genre_ids") or []
    interest = sum(affinity.get(genre, 0.0) for genre in genres)
    rating = max(0.0, min(10.0, float(row.get("vote_average") or 0)))
    votes = max(0, int(row.get("vote_count") or 0))
    # Public rating is a weak prior, not a substitute for personal taste.
    quality = (rating / 10) * min(1.0, math.log1p(votes) / 7)
    tokens = [word for word in re.findall(r"\w+", phrase.casefold())
              if len(word) >= 5 and word not in {"séries", "films", "series", "movies", "watched"}]
    text = ((row.get("overview") or "") + " " + (row.get("name") or row.get("title") or "")).casefold()
    thematic = sum(word in text for word in set(tokens[:12]))
    return interest + quality * 1.2 + thematic * 0.25


async def recommend_streaming(
    store: Any, token: str, request: StreamingAIInput, plan: Any, *,
    model: str, api_key: str, provider: str,
) -> dict:
    """Retrieve candidates under a strict subscription scope, then rank once."""
    selection = ",".join(_provider_keys(",".join(request.services)))
    if not token:
        raise HTTPException(503, "Configure TMDB on the server for streaming AI search.")
    profile = await asyncio.to_thread(_catalogue_profile, store)
    if not profile["seeds"]:
        return {
            "items": [], "total": 0, "relevance_reasons": {},
            "profile_count": 0, "note": "Mark or rate some titles as watched to personalize recommendations.",
            "assistant_message": "No viewing history is available yet.",
        }

    kind = request.kind
    if plan.kind == "movie":
        kind = "movie"
    elif plan.kind in ("tvshow", "episode"):
        kind = "tv"
    kinds = ("movie", "tv") if kind == "both" else (kind,)
    locale = "fr-FR" if request.region == "FR" else "en-US"
    semaphore = asyncio.Semaphore(5)
    credential_scope = hashlib.sha256(token.encode()).hexdigest()[:12]

    async def fetch(path: str, params: dict) -> dict:
        cache_key = credential_scope + ":" + path + ":" + json.dumps(
            params, sort_keys=True, separators=(",", ":")
        )
        cached = _TMDB_CACHE.get(cache_key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        async with semaphore:
            result = await _tmdb(token, path, params)
        if len(_TMDB_CACHE) > 400:
            _TMDB_CACHE.pop(next(iter(_TMDB_CACHE)))
        _TMDB_CACHE[cache_key] = time.monotonic() + 900, result
        return result

    seeds = profile["seeds"][:6]
    dislikes = profile["negative"][:3]
    async def seed_details(work: dict) -> tuple[dict, dict]:
        category, number = work["id"].split(":")
        try:
            detail = await fetch(f"/{category}/{number}", {"language": locale})
        except HTTPException:
            detail = {}
        return work, detail

    seed_pairs = await asyncio.gather(*(seed_details(work) for work in seeds + dislikes))
    affinity: dict[int, float] = defaultdict(float)
    for work, detail in seed_pairs:
        score = work["rating"]
        weight = 0.35 if score is None else (score - 6.0) / 3.0
        for genre in detail.get("genres", []):
            genre_id = genre.get("id") if isinstance(genre, dict) else None
            if isinstance(genre_id, int):
                affinity[genre_id] += weight

    full_prompt = (
        ("Previous search: " + request.previous_prompt + "\n")
        if request.previous_prompt else ""
    ) + request.prompt
    language = _language_hint(full_prompt)
    # A single discover response is already filtered to paid subscriptions in
    # the selected country; seed-based recommendations must be verified later.
    good_genres = [genre for genre, value in
                   sorted(affinity.items(), key=lambda pair: -pair[1])
                   if value > 0][:3]
    genre_labels = {
        item["id"]: item["name"]
        for _, detail in seed_pairs
        for item in detail.get("genres", [])
        if isinstance(item, dict) and isinstance(item.get("id"), int)
        and isinstance(item.get("name"), str)
    }
    for category in kinds:
        try:
            genre_data = await fetch("/genre/" + category + "/list", {"language": locale})
            genre_labels.update({
                item["id"]: item["name"] for item in genre_data.get("genres", [])
                if isinstance(item, dict) and isinstance(item.get("id"), int)
                and isinstance(item.get("name"), str)
            })
        except HTTPException:
            pass

    async def discover(category: str, genre: int | None, page: int) -> tuple[str, list]:
        args = {
            "watch_region": request.region,
            "with_watch_providers": _provider_ids(selection),
            "with_watch_monetization_types": "flatrate",
            "language": locale,
            "sort_by": "popularity.desc",
            "page": page,
        }
        if genre is not None:
            args["with_genres"] = genre
        if language:
            args["with_original_language"] = language
        try:
            result = await fetch("/discover/" + category, args)
            return category, result.get("results") or []
        except HTTPException:
            return category, []

    async def similar_seed(seed: dict) -> tuple[str, list]:
        category, number = seed["id"].split(":")
        if category not in kinds:
            return category, []
        try:
            data = await fetch(
                f"/{category}/{number}/recommendations",
                {"language": locale, "page": 1},
            )
            return category, data.get("results") or []
        except HTTPException:
            return category, []

    jobs = [discover(cat, genre, 1) for cat in kinds for genre in good_genres[:3]]
    jobs.extend(discover(cat, None, page) for cat in kinds for page in (1, 2))
    jobs.extend(similar_seed(seed) for seed in seeds[:4])
    found = await asyncio.gather(*jobs)

    blocked = set(profile["seen"])
    blocked.update(key for key in request.excluded_ids if re.fullmatch(r"(movie|tv):[1-9]\d{0,8}", key))
    candidates: dict[str, tuple[float, dict]] = {}
    query_text = " ".join([plan.semantic_query or "", *plan.retrieval_terms[:6]])
    for category, rows in found:
        for row in rows:
            if not isinstance(row, dict):
                continue
            identity = _identity(category, row.get("id"))
            if not identity or identity in blocked:
                continue
            if language and row.get("original_language") != language:
                continue
            date = row.get("first_air_date" if category == "tv" else "release_date") or ""
            year = int(date[:4]) if re.match(r"^\d{4}", date) else None
            if (plan.year_min and (year is None or year < plan.year_min) or
                    plan.year_max and (year is None or year > plan.year_max) or
                    plan.rating_min is not None and float(row.get("vote_average") or 0) < plan.rating_min):
                continue
            score = _score(row, affinity, query_text)
            if identity not in candidates or score > candidates[identity][0]:
                candidates[identity] = score, {**_item(row, category), "genre_ids": row.get("genre_ids") or []}

    ordered = sorted(candidates.items(), key=lambda entry: -entry[1][0])[:32]

    async def verify(identity: str, item: dict) -> tuple[str, dict, list[str]]:
        category, number = identity.split(":")
        key = credential_scope + ":" + identity + ":" + selection + ":" + request.region
        cached = _AVAILABILITY_CACHE.get(key)
        if cached and cached[0] > time.monotonic():
            return identity, item, cached[1]
        try:
            async with semaphore:
                services = await _availability(
                    token, category, int(number), selection, request.region
                )
        except HTTPException:
            services = []
        if len(_AVAILABILITY_CACHE) > 600:
            _AVAILABILITY_CACHE.pop(next(iter(_AVAILABILITY_CACHE)))
        _AVAILABILITY_CACHE[key] = time.monotonic() + 900, services
        return identity, item, services

    verified = await asyncio.gather(*(verify(identity, data[1]) for identity, data in ordered))
    available = [(identity, {**item, "service_keys": keys})
                 for identity, item, keys in verified if keys][:24]

    reasons: dict[str, str] = {}
    if available:
        favorites = [
            {
                "title": detail.get("name") or detail.get("title") or w["title"] or w["id"],
                "user_rating": w["rating"],
                "genres": [g.get("name") for g in detail.get("genres", [])
                           if isinstance(g, dict) and g.get("name")] or w["genres"],
                "synopsis": (detail.get("overview") or "")[:350],
            }
            for w, detail in seed_pairs[:len(seeds)]
        ]
        disliked = [
            {
                "title": detail.get("name") or detail.get("title") or w["title"] or w["id"],
                "user_rating": w["rating"],
                "synopsis": (detail.get("overview") or "")[:250],
            }
            for w, detail in seed_pairs[len(seeds):]
        ]
        explanations = await rerank_candidates(
            full_prompt + "\nPersonal viewing profile (ratings out of 10). " +
            "Unrated watched titles are NOT evidence of enjoyment: " +
            json.dumps({
                "liked": [item for item in favorites if item["user_rating"] is not None],
                "watched_unrated": [item for item in favorites if item["user_rating"] is None],
                "disliked": disliked,
            }, ensure_ascii=False),
            [
                {
                    "id": identity, "title": item["title"], "kind": item["kind"],
                    "year": item["year"],
                    "genres": [genre_labels.get(g, str(g)) for g in item["genre_ids"]],
                    "synopsis": item["overview"][:750], "rating": item["rating"],
                }
                for identity, item in available
            ],
            model, api_key, provider=provider,
        )
        reasons = {entry["id"]: entry["reason"] for entry in explanations}
        available = [(identity, item) for identity, item in available if identity in reasons]

    items = [item for _, item in available[:20]]
    return {
        "items": items, "total": len(items), "relevance_reasons": reasons,
        "profile_count": profile["rated_count"],
        "note": None if profile["rated_count"] else "Recommendations use watched history; ratings improve accuracy.",
        "assistant_message": (
            f"{len(items)} personalized titles on the selected subscriptions in {request.region}."
            if items else "No sufficiently relevant unwatched titles were confirmed."
        ),
    }
