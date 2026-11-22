"""Season-aware watched status shared by streaming and the canonical catalogue.

The compact snapshot contains *only the episodes aired when watched was marked*.
A separate discovery inventory may grow without rewriting that user history.
No user watch data is inferred from provider or TMDB popularity metadata.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

_COORDINATE = re.compile(r"^[1-9][0-9]{0,4}$")


def decode_episodes(raw: Any) -> set[tuple[int, int]]:
    if not isinstance(raw, str) or len(raw) > 32000:
        return set()
    result: set[tuple[int, int]] = set()
    for season_part in raw.split(";"):
        if "=" not in season_part:
            continue
        season_raw, episodes_raw = season_part.split("=", 1)
        if not _COORDINATE.fullmatch(season_raw) or int(season_raw) > 10000:
            continue
        season = int(season_raw)
        for piece in episodes_raw.split(","):
            bounds = piece.split("-", 1)
            if not all(_COORDINATE.fullmatch(v) for v in bounds):
                continue
            start = int(bounds[0])
            end = int(bounds[1]) if len(bounds) == 2 else start
            if not (1 <= start <= end <= 10000) or end - start >= 5000:
                continue
            result.update((season, number) for number in range(start, end + 1))
    return result


def encode_episodes(coordinates: set[tuple[int, int]]) -> str:
    by_season: dict[int, set[int]] = defaultdict(set)
    for season, episode in coordinates:
        if 1 <= season <= 10000 and 1 <= episode <= 10000:
            by_season[season].add(episode)
    encoded = []
    for season, numbers in sorted(by_season.items()):
        ranges: list[str] = []
        begin = end = None
        for number in sorted(numbers):
            if begin is None:
                begin = end = number
            elif number == end + 1:
                end = number
            else:
                ranges.append(str(begin) if begin == end else f"{begin}-{end}")
                begin = end = number
        if begin is not None:
            ranges.append(str(begin) if begin == end else f"{begin}-{end}")
        encoded.append(str(season) + "=" + ",".join(ranges))
    return ";".join(encoded)


def effective_series_status(personal: dict[str, Any] | None,
                            inventory: dict[str, Any] | None) -> str | None:
    """Return a derived status, or None while legacy/inventory data is missing.

    Specials (season zero) never affect completion. Unreleased episodes must
    not be inserted into the inventory by the metadata refresh process.
    """
    if personal is None or inventory is None:
        return None
    available = decode_episodes(inventory.get("episodes"))
    if not available:
        return None
    if "watched_episode_snapshot" in personal:
        watched = decode_episodes(personal["watched_episode_snapshot"])
    else:
        # A pre-migration boolean is retained until an authoritative aired
        # episode snapshot has been reconstructed from TMDB dates.
        if str(personal.get("watched")).lower() == "true":
            return None
        watched = set()
    try:
        overrides = json.loads(personal.get("episode_overrides") or "{}")
    except (TypeError, ValueError):
        overrides = {}
    if isinstance(overrides, dict):
        for key, value in overrides.items():
            if not isinstance(key, str) or ":" not in key or not isinstance(value, bool):
                continue
            season_raw, episode_raw = key.split(":", 1)
            if not (season_raw.isdecimal() and episode_raw.isdecimal()):
                continue
            coordinate = (int(season_raw), int(episode_raw))
            if value:
                watched.add(coordinate)
            else:
                watched.discard(coordinate)
    complete = len(available & watched)
    if complete == len(available):
        return "watched"
    return "in_progress" if complete else "unwatched"
