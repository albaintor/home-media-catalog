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
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .auth import authenticated, writable
from .watch_progress import decode_episodes, encode_episodes, effective_series_status

TMDB = "https://api.themoviedb.org/3"
NETFLIX_COUNTRIES = "https://www.netflix.com/tudum/top10/data/all-weeks-countries.tsv"
PROVIDERS = {
    "netflix": ("Netflix", (8,)),
    "disney_plus": ("Disney+", (337,)),
    "prime_video": ("Prime Video", (119,)),
    "apple_tv_plus": ("Apple TV+", (350,)),
    "hbo_max": ("HBO Max", (1899, 384)),
    "paramount_plus": ("Paramount+", (531,)),
    "crunchyroll": ("Crunchyroll", (283,)),
    "rakuten_viki": ("Rakuten Viki", (344,)),
    "mubi": ("MUBI", (11,)),
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
    "crunchyroll": ("crunchyroll.com",),
    "rakuten_viki": ("viki.com",),
    "mubi": ("mubi.com",),
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



def _bootstrap_catalogue_watch_progress(store: Any) -> int:
    """One-time retrofit of existing catalogue watched/rating states.

    Preserve every existing Streaming journal row. Imported episode watch
    states are authoritative; for a watched series without episode records,
    record the migration time and resolve historical episode air dates later.
    The original click date cannot be recovered from old boolean fields.
    """
    current = {row["key"] for row in _merge(store, []) if not row["deleted"]}
    pending: list[StreamingStateRow] = []
    now_stamp = datetime.now(UTC).isoformat(timespec="microseconds")
    with store.connect() as db:
        rows = db.execute(
            "SELECT DISTINCT i.value AS tmdb_id, w.id AS work_id, w.kind,"
            " w.watch_status, w.user_rating "
            "FROM identifiers i JOIN works w ON w.id=i.work_id "
            "WHERE i.namespace='tmdb' AND w.kind IN ('tvshow','movie') "
            "ORDER BY w.id"
        ).fetchall()
        for row in rows:
            raw_id = str(row["tmdb_id"])
            if not raw_id.isdecimal() or int(raw_id) < 1:
                continue
            kind = "tv" if row["kind"] == "tvshow" else "movie"
            key = f"personal:{kind}:{raw_id}"
            if key in current:
                continue
            state = row["watch_status"] or "unknown"
            rating = row["user_rating"]
            if state not in ("watched", "in_progress", "unwatched") and rating is None:
                continue
            payload: dict[str, str] = {"watch_schema": "2"}
            if state in ("watched", "unwatched", "in_progress"):
                payload["watched"] = "true" if state == "watched" else "false"
            if rating is not None:
                payload["rating"] = str(rating)
            if kind == "tv":
                watched = db.execute(
                    "SELECT season,episode FROM works "
                    "WHERE parent_id=? AND kind='episode' AND season>0 "
                    "AND watch_status='watched'",
                    (row["work_id"],)
                ).fetchall()
                coordinates = {(e["season"], e["episode"]) for e in watched
                               if e["episode"] is not None}
                if state == "watched":
                    # A legacy series-level "watched" means all episodes
                    # available when it was set, including those never
                    # imported into the physical catalogue. Freeze them from
                    # TMDB air dates at the migration boundary.
                    payload["watched_at"] = now_stamp
                elif coordinates:
                    payload["watched_episode_snapshot"] = encode_episodes(coordinates)
                else:
                    payload["watched_episode_snapshot"] = ""
            pending.append(StreamingStateRow(
                key=key, payload=payload, modified_at=now_stamp))
            current.add(key)
    if pending:
        _merge(store, pending)
    return len(pending)


def _reconcile_watch_progress(store: Any, rows: list[dict[str, Any]]) -> int:
    """Project the shared local-first journal into the canonical catalogue.

    The inventory is public reference metadata. An arrival can change a
    series' derived status, but never modifies its watched snapshot. User edits
    from either client are kept in the same bidirectional journal.
    """
    states = {
        row["key"]: row["payload"]
        for row in rows if not row["deleted"] and isinstance(row["payload"], dict)
    }
    changed = 0
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        series = db.execute(
            "SELECT DISTINCT i.work_id,i.value AS tmdb_id "
            "FROM identifiers i JOIN works w ON w.id=i.work_id "
            "WHERE i.namespace='tmdb' AND w.kind='tvshow'"
        ).fetchall()
        for series_row in series:
            tmdb_id = str(series_row["tmdb_id"])
            personal = states.get("personal:tv:" + tmdb_id)
            inventory = states.get("inventory:tv:" + tmdb_id)
            status = effective_series_status(personal, inventory)
            if status is None:
                continue
            available = decode_episodes(inventory.get("episodes"))
            baseline = decode_episodes(personal.get("watched_episode_snapshot"))
            try:
                overrides = json.loads(personal.get("episode_overrides") or "{}")
            except (ValueError, TypeError):
                overrides = {}
            if not isinstance(overrides, dict):
                overrides = {}
            for row in db.execute(
                "SELECT id,season,episode,watch_status FROM works "
                "WHERE kind='episode' AND parent_id=?",
                (series_row["work_id"],)
            ).fetchall():
                coord = (row["season"], row["episode"])
                if coord not in available:
                    continue
                key = f"{coord[0]}:{coord[1]}"
                watched = overrides.get(key, coord in baseline)
                if not isinstance(watched, bool):
                    watched = coord in baseline
                if (row["watch_status"] == "watched") != watched:
                    db.execute(
                        "INSERT INTO personal(work_id,watched) VALUES(?,?) "
                        "ON CONFLICT(work_id) DO UPDATE SET watched=excluded.watched",
                        (row["id"], int(watched)),
                    )
                    store._rebuild(db, row["id"])
                    changed += 1
            current = db.execute("SELECT watch_status FROM works WHERE id=?",
                                 (series_row["work_id"],)).fetchone()
            if current and current["watch_status"] != status:
                db.execute("UPDATE works SET watch_status=? WHERE id=?",
                           (status, series_row["work_id"]))
                changed += 1
    return changed


def _mirror_catalogue_personal(
    store: Any, media_id: str, *, watched: bool | None = None,
    rating_changed: bool = False, rating: float | None = None,
) -> None:
    """Server/web personal edits become journal revisions visible to every iOS device."""
    detail = store.detail(media_id)
    if not detail or detail.get("kind") not in ("movie", "tvshow"):
        return
    raw_ids = (detail.get("identifiers") or {}).get("tmdb") or []
    if isinstance(raw_ids, (str, int)):
        raw_ids = [raw_ids]
    tmdb_id = next((str(item) for item in raw_ids
                    if str(item).isdecimal() and int(item) > 0), None)
    if tmdb_id is None or (watched is None and not rating_changed):
        return
    kind = "tv" if detail["kind"] == "tvshow" else "movie"
    personal_key = f"personal:{kind}:{tmdb_id}"
    rows = _merge(store, [])
    existing = next((row for row in rows
                     if row["key"] == personal_key and not row["deleted"]), None)
    payload = dict(existing["payload"]) if existing else {}
    now_stamp = datetime.now(UTC)
    if existing:
        now_stamp = max(now_stamp, _stamp(existing["modified_at"]) + timedelta(microseconds=1))
        # iOS saves its complete episode snapshot *before* sending the
        # matching canonical PATCH. Do not replace that snapshot with a fresh
        # server-side bool revision (which could erase the migration boundary).
        same_watch = watched is None or payload.get("watched") == (
            "true" if watched else "false")
        same_rating = not rating_changed or (
            payload.get("rating") == (str(rating) if rating is not None else None)
            and (rating is not None or payload.get("rating_cleared") == "true"))
        if same_watch and same_rating:
            return
    should_mark = watched if watched is not None else (True if rating_changed and rating is not None else None)
    if should_mark is not None:
        payload["watched"] = "true" if should_mark else "false"
        payload["watch_schema"] = "2"
        payload.pop("episode_overrides", None)
        if should_mark:
            payload["watched_at"] = now_stamp.isoformat(timespec="microseconds")
            inventory = next((row["payload"] for row in rows
                              if row["key"] == f"inventory:tv:{tmdb_id}"
                              and not row["deleted"]), None)
            if kind == "tv" and inventory:
                payload["watched_episode_snapshot"] = inventory.get("episodes", "")
            else:
                payload.pop("watched_episode_snapshot", None)
        else:
            payload.pop("watched_at", None)
            payload["watched_episode_snapshot"] = ""
    if rating_changed:
        if rating is not None:
            payload["rating"] = str(rating)
            payload.pop("rating_cleared", None)
        else:
            payload.pop("rating", None)
            payload["rating_cleared"] = "true"
    merged = _merge(store, [StreamingStateRow(
        key=personal_key, payload=payload,
        modified_at=now_stamp.isoformat(timespec="microseconds"))])
    if kind == "tv":
        _reconcile_watch_progress(store, merged)


def _mirror_catalogue_episode_edit(
    store: Any, media_id: str, *, watched: bool,
    season: int | None, up_to_episode: int | None,
) -> None:
    """Represent web-side season/episode watch edits in the iOS journal."""
    detail = store.detail(media_id)
    if not detail or detail.get("kind") != "tvshow":
        return
    raw_ids = (detail.get("identifiers") or {}).get("tmdb") or []
    if isinstance(raw_ids, (str, int)):
        raw_ids = [raw_ids]
    tmdb_id = next((str(value) for value in raw_ids
                    if str(value).isdecimal() and int(value) > 0), None)
    if not tmdb_id:
        return
    key = f"personal:tv:{tmdb_id}"
    rows = _merge(store, [])
    existing = next((row for row in rows if row["key"] == key and not row["deleted"]), None)
    payload = dict(existing["payload"]) if existing else {}
    # Materialize the updated imported episode states into one compact,
    # revisioned snapshot. Avoid thousands of per-episode overrides and preserve
    # watched entries not imported into the canonical catalogue.
    snapshot = decode_episodes(payload.get("watched_episode_snapshot"))
    try:
        existing_overrides = json.loads(payload.get("episode_overrides") or "{}")
    except (TypeError, ValueError):
        existing_overrides = {}
    if isinstance(existing_overrides, dict):
        for key, watched_flag in existing_overrides.items():
            if not isinstance(watched_flag, bool) or not isinstance(key, str):
                continue
            parts = key.split(":")
            if len(parts) != 2 or not all(part.isdecimal() for part in parts):
                continue
            coordinates = (int(parts[0]), int(parts[1]))
            if watched_flag:
                snapshot.add(coordinates)
            else:
                snapshot.discard(coordinates)
    with store.connect() as db:
        episodes = db.execute(
            "SELECT season,episode,watch_status FROM works "
            "WHERE parent_id=? AND kind='episode'", (media_id,)
        ).fetchall()
    for episode_row in episodes:
        number = episode_row["season"]
        episode_number = episode_row["episode"]
        if number is None or episode_number is None or number == 0:
            continue
        coordinate = (number, episode_number)
        matches = (season is None or number == season) and (
            up_to_episode is None or episode_number <= up_to_episode)
        if matches:
            if watched:
                snapshot.add(coordinate)
            else:
                snapshot.discard(coordinate)
        elif episode_row["watch_status"] == "watched":
            snapshot.add(coordinate)
    payload["watched_episode_snapshot"] = encode_episodes(snapshot)
    payload.pop("episode_overrides", None)
    payload.setdefault("watched", "false")
    payload["watch_schema"] = "2"
    now_stamp = datetime.now(UTC)
    if existing:
        now_stamp = max(now_stamp, _stamp(existing["modified_at"]) + timedelta(microseconds=1))
    _merge(store, [StreamingStateRow(key=key, payload=payload,
                                   modified_at=now_stamp.isoformat(timespec="microseconds"))])


def _streaming_trailers(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Only verified YouTube trailers/teasers from TMDB may be opened."""
    videos = raw.get("videos") or {}
    entries = videos.get("results", []) if isinstance(videos, dict) else []
    if not isinstance(entries, list):
        return []
    trailers = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("site") != "YouTube":
            continue
        if entry.get("type") not in {"Trailer", "Teaser"}:
            continue
        key = entry.get("key")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", key):
            continue
        if key in seen:
            continue
        seen.add(key)
        language = entry.get("iso_639_1")
        trailers.append({
            "key": key,
            "name": str(entry.get("name") or "Trailer")[:160],
            "type": entry["type"],
            "official": entry.get("official") is True,
            "language": language if language in {"fr", "en"} else None,
        })
    trailers.sort(key=lambda item: (item["type"] != "Trailer",
                                    not item["official"],
                                    item["language"] != "fr"))
    return trailers[:8]


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
        "trailers": _streaming_trailers(raw),
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
    Tombstones remove only the corresponding heart-created internal edition;
    ordinary physical, Kodi and CLZ editions must remain untouched.
    """
    wanted = []
    deleted_keys: list[str] = []
    for row in rows:
        if not re.fullmatch(r"item:(movie|tv):[0-9]{1,24}", row["key"]):
            continue
        if row["deleted"]:
            deleted_keys.append(_streaming_edition_uuid(row["key"]))
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
    # A removed heart must clear its actual internal catalogue edition, not
    # merely its timestamped state row. Guard deletion using the provenance
    # marker so a regular physical edition can never be removed here.
    deleted = 0
    if deleted_keys:
        with store.connect() as db:
            safe_to_delete = [
                row["external_key"] for row in db.execute(
                    "SELECT external_key FROM records "
                    "WHERE source_id='internal' "
                    "AND json_extract(payload, '$.metadata.streaming_reference')=1 "
                    "AND external_key IN (" + ",".join("?" for _ in deleted_keys) + ")",
                    deleted_keys,
                )
            ]
        for edition_id in safe_to_delete:
            try:
                store.delete_internal_media(edition_id)
                deleted += 1
            except KeyError:
                # An iOS catalogue upload might already have removed it.
                continue
    if not wanted:
        return deleted
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
    return created + deleted


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
                    page: int, region: str, sort: str,
                    order: str = "desc", origin_country: str | None = None,
                    original_language: str | None = None,
                    rating_min: float | None = None,
                    rating_max: float | None = None,
                    keyword_id: int | None = None) -> dict[str, Any]:
    # TMDB sorts the entire matching catalogue *before* pagination. Sorting
    # only the 20 titles returned by each page produces misleading results.
    field = {
        "popularity": "popularity",
        "rating": "vote_average",
        "title": "name" if kind == "tv" else "title",
        "original_title": "original_name" if kind == "tv" else "original_title",
        "year": "first_air_date" if kind == "tv" else "primary_release_date",
        "recent": "first_air_date" if kind == "tv" else "primary_release_date",
    }[sort]
    direction = "desc" if sort == "recent" else order
    params: dict[str, Any] = {
        "watch_region": region, "with_watch_providers": _provider_ids(service),
        "with_watch_monetization_types": "flatrate",
        "language": "fr-FR" if region == "FR" else "en-US",
        "sort_by": f"{field}.{direction}",
        "page": page,
    }
    if sort == "rating":
        params["vote_count.gte"] = 100
    elif sort in {"recent", "year"}:
        # Recently released titles currently available, NOT newly added to the service.
        params["first_air_date.lte" if kind == "tv" else "primary_release_date.lte"] = datetime.now(UTC).date().isoformat()
    if genre is not None:
        params["with_genres"] = genre
    if keyword_id is not None:
        params["with_keywords"] = keyword_id
    if origin_country:
        params["with_origin_country"] = origin_country
    if original_language:
        params["with_original_language"] = original_language
    if rating_min is not None:
        params["vote_average.gte"] = rating_min
    if rating_max is not None:
        params["vote_average.lte"] = rating_max
    if (rating_min is not None or rating_max is not None) and sort != "rating":
        params["vote_count.gte"] = 1
    result = await _tmdb(token, "/discover/" + ("tv" if kind == "tv" else "movie"), params)
    return {
        "items": [_item(row, kind) for row in result.get("results", []) if row.get("id")],
        "page": result.get("page", page),
        "total_pages": min(result.get("total_pages", 1), 500),
        "source": "tmdb",
        "ranking_type": "tmdb_" + sort,
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
        cache_key = f"tmdb-title-details-videos:{language}:{kind}:{tmdb_id}"
        cached = _CACHE.get(cache_key)
        if cached is not None and time.monotonic() - cached[0] < 6 * 3600:
            return cached[1]
        raw = await _tmdb(
            settings.tmdb_api_token.strip(), f"/{kind}/{tmdb_id}",
            {"language": language, "append_to_response": "credits,videos"},
        )
        result = _title_metadata(raw, kind)
        if not result["trailers"] and language != "en-US":
            # A film may have no localized video even when an English trailer
            # exists. Keep the details usable if the fallback is unavailable.
            try:
                other = await _tmdb(
                    settings.tmdb_api_token.strip(), f"/{kind}/{tmdb_id}/videos",
                    {"language": "en-US"},
                )
                result["trailers"] = _streaming_trailers({"videos": other})
            except (httpx.HTTPError, HTTPException, ValueError, KeyError):
                pass
        if len(_CACHE) >= 160:
            # Bound global, per-installation metadata cache.
            oldest = next(iter(_CACHE))
            _CACHE.pop(oldest, None)
        _CACHE[cache_key] = (time.monotonic(), result)
        return result

    @router.get("/titles/tv/{tmdb_id}/seasons/{season_number}/episodes",
                dependencies=[Depends(authenticated)])
    async def streaming_season_episodes(
        tmdb_id: int, season_number: int,
        language: str = Query("fr-FR", pattern=r"^[a-z]{2}-[A-Z]{2}$"),
    ):
        """Read one TMDB season without writing to the internal catalogue.

        Streaming discovery must be able to browse episodes without the heart.
        Only a particular selected season is fetched, rather than importing the
        entire series into the media catalog just to display its contents.
        """
        if not 1 <= tmdb_id <= 999999999 or not 0 <= season_number <= 10000:
            raise HTTPException(422, "Invalid TMDB season identity")
        cache_key = f"tmdb-streaming-season:{language}:{tmdb_id}:{season_number}"
        cached = _CACHE.get(cache_key)
        if cached is not None and time.monotonic() - cached[0] < 6 * 3600:
            return cached[1]
        raw = await _tmdb(
            settings.tmdb_api_token.strip(),
            f"/tv/{tmdb_id}/season/{season_number}",
            {"language": language},
        )
        items = []
        for episode in raw.get("episodes", [])[:150]:
            if not isinstance(episode, dict):
                continue
            number = episode.get("episode_number")
            if not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= 10000:
                continue
            items.append({
                "season": season_number,
                "episode": number,
                "tmdb_id": episode.get("id"),
                "title": str(episode.get("name") or f"Episode {number}")[:300],
                "overview": str(episode.get("overview") or "")[:5000],
                "air_date": str(episode.get("air_date") or "")[:30],
                "still_path": episode.get("still_path"),
            })
        result = {"episodes": items}
        if len(_CACHE) >= 160:
            _CACHE.pop(next(iter(_CACHE)), None)
        _CACHE[cache_key] = (time.monotonic(), result)
        return result

    @router.get("/people/{person_id}", dependencies=[Depends(authenticated)])
    async def person_filmography(
        person_id: int,
        language: str = Query("fr-FR", pattern=r"^[a-z]{2}-[A-Z]{2}$"),
    ):
        """TMDB-identified person and unfiltered filmography for both web/iOS.

        Do not apply the user's streaming subscriptions here. The web client
        verifies the regional subscription offers independently for each title.
        """
        if not 1 <= person_id <= 999999999:
            raise HTTPException(422, "Invalid TMDB person identity")
        cache_key = f"tmdb-person:{language}:{person_id}"
        cached = _CACHE.get(cache_key)
        if cached is not None and time.monotonic() - cached[0] < 6 * 3600:
            return cached[1]
        raw = await _tmdb(
            settings.tmdb_api_token.strip(), f"/person/{person_id}",
            {"language": language, "append_to_response": "combined_credits"},
        )
        if raw.get("id") != person_id:
            raise HTTPException(502, "Unexpected TMDB person identity")
        credits = raw.get("combined_credits") or {}
        unique: dict[tuple[str, int], dict[str, Any]] = {}
        for member in (credits.get("cast") or []) + (credits.get("crew") or []):
            if not isinstance(member, dict) or member.get("media_type") not in {"movie", "tv"}:
                continue
            kind = member["media_type"]
            identifier = member.get("id")
            if not isinstance(identifier, int) or identifier < 1:
                continue
            record = _item(member, kind)
            if not record["title"]:
                continue
            identity = (kind, identifier)
            if identity not in unique or member.get("character"):
                unique[identity] = record
        filmography = sorted(
            unique.values(),
            key=lambda row: (row.get("year") or 0, row.get("rating") or 0),
            reverse=True,
        )[:300]
        photo = raw.get("profile_path")
        result = {
            "id": person_id,
            "name": str(raw.get("name") or "")[:160],
            "biography": str(raw.get("biography") or "")[:12000],
            "profile_path": photo if isinstance(photo, str)
                and re.fullmatch(r"/[A-Za-z0-9_.-]{1,150}", photo) else None,
            "department": str(raw.get("known_for_department") or "")[:100],
            "filmography": filmography,
        }
        if len(_CACHE) >= 160:
            _CACHE.pop(next(iter(_CACHE)), None)
        _CACHE[cache_key] = (time.monotonic(), result)
        return result

    @router.get("/catalogue-metrics", dependencies=[Depends(authenticated)])
    def catalogue_metrics():
        """Read-only Kodi/internal-catalogue status and personal scores by TMDB ID.

        A discoverable title is not automatically a catalogue entry. Only
        authenticated, actually catalogued titles have personal metadata.
        """
        with store.connect() as db:
            rows = db.execute(
                "SELECT i.value AS tmdb_id, w.id AS work_id, w.kind, w.watch_status, "
                "w.rating, w.user_rating "
                "FROM identifiers i JOIN works w ON w.id=i.work_id "
                "WHERE i.namespace='tmdb' AND w.kind IN ('movie','tvshow')"
            ).fetchall()
        return {"items": [
            {"kind": "tv" if row["kind"] == "tvshow" else "movie",
             "tmdb_id": int(row["tmdb_id"]), "work_id": row["work_id"],
             "watch_status": row["watch_status"] or "unknown",
             "rating": row["rating"], "user_rating": row["user_rating"]}
            for row in rows
            if str(row["tmdb_id"]).isdecimal()
        ]}

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

    @router.get("/categories/search", dependencies=[Depends(authenticated)])
    async def search_categories(
        q: str = Query(min_length=2, max_length=80),
    ):
        """Find TMDB keywords to use as additional personalized shelves.

        Keyword ids are discover filters, not Netflix editorial categories.
        Return only validated ids and short names to prevent arbitrary client
        fields from being persisted in the shared Streaming journal.
        """
        results = await _tmdb(
            settings.tmdb_api_token.strip(), "/search/keyword",
            {"query": q.strip(), "page": 1},
        )
        matches = []
        seen: set[int] = set()
        for row in results.get("results", []):
            if not isinstance(row, dict):
                continue
            key = row.get("id")
            title = row.get("name")
            if (not isinstance(key, int) or isinstance(key, bool) or key <= 0
                    or key in seen or not isinstance(title, str)
                    or not 1 <= len(title.strip()) <= 80):
                continue
            seen.add(key)
            matches.append({"id": key, "name": title.strip()})
            if len(matches) == 20:
                break
        return {"results": matches}

    @router.get("/discover", dependencies=[Depends(authenticated)])
    async def discover(
        service: str, kind: str = Query("movie", pattern=r"^(movie|tv)$"),
        genre: int | None = Query(None, ge=1), page: int = Query(1, ge=1, le=500),
        region: str = Query("FR", pattern=r"^[A-Z]{2}$"),
        sort: str = Query("popularity", pattern=r"^(popularity|rating|recent|title|original_title|year)$"),
        order: str = Query("desc", pattern=r"^(asc|desc)$"),
        origin_country: str | None = Query(None, pattern=r"^[A-Z]{2}$"),
        original_language: str | None = Query(None, pattern=r"^[a-z]{2}$"),
        rating_min: float | None = Query(None, ge=0, le=10),
        rating_max: float | None = Query(None, ge=0, le=10),
        keyword_id: int | None = Query(None, ge=1),
    ):
        return await _discover(settings.tmdb_api_token.strip(), service, kind, genre, page,
                               region, sort, order, origin_country, original_language,
                               rating_min, rating_max, keyword_id)

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
                                 1, region, "popularity", "desc")
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
        _reconcile_watch_progress(store, merged)
        return {"rows": merged}

    return router
