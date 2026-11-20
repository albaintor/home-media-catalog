"""Propagate personal media changes to writable source connectors.

Source snapshots remain read-only; Kodi writes use a configured JSON-RPC player
associated with the same MariaDB library. The dispatch boundary can host other
writable connector types without changing the personal-edit API.
"""

import asyncio
import logging
import math
from collections import defaultdict
from typing import Any

from .connectors.base import ConnectorError
from .kodi_mariadb_write import write_kodi_mariadb
from .mymovies_writeback import write_mymovies
from .players import KodiUnavailable, connection
from .utils import identifiers

_LOG = logging.getLogger(__name__)

# Null watched clears the local override ("use source value"), not the
# source itself. Null user_rating removes the personal rating everywhere:
# Kodi stores it as userrating 0 ("no rating").
# Extend this registry when another connector supports updates.
WRITABLE_FIELDS = {
    "kodi": frozenset({"user_rating", "watched"}),
    "mymovies": frozenset({"watched"}),
}

_KODI_METHODS = {
    "movie": ("movieid", "moviedetails", "GetMovieDetails", "SetMovieDetails"),
    "tvshow": ("tvshowid", "tvshowdetails", "GetTVShowDetails", "SetTVShowDetails"),
    "episode": ("episodeid", "episodedetails", "GetEpisodeDetails", "SetEpisodeDetails"),
}


def _verify_identity(item: dict, edition: dict, detail: dict) -> None:
    """Refuse writes against a stale ID or an unrelated Kodi library."""
    local = edition.get("identifiers") or {}
    remote = identifiers(item.get("uniqueid") or {})
    shared = local.keys() & remote.keys()
    if any(local[key] != remote[key] for key in shared):
        raise ConnectorError(
            "Les identifiants du média diffèrent dans Kodi. Synchronisez la source."
        )
    # If there is no shared external ID, require an exact title match; IDs
    # alone are not safe across distinct MyVideos databases.
    if (
        not shared
        and (item.get("title") or "").strip().casefold()
        != (detail.get("title") or "").strip().casefold()
    ):
        raise ConnectorError(
            "Le titre du média ne correspond pas dans Kodi. Synchronisez la source."
        )
    if detail["kind"] == "episode":
        for field in ("season", "episode"):
            expected = detail.get(field)
            actual = item.get(field)
            if expected is not None and actual is not None and expected != actual:
                raise ConnectorError(
                    "La saison ou le numéro d'épisode ne correspond pas dans Kodi."
                )


async def _update_kodi_show_episodes(rpc, kodi_id: int, watched: bool) -> None:
    """Kodi's SetTVShowDetails(playcount) may return OK without touching episodes."""
    episodes = []
    start = 0
    while True:
        response = await rpc(
            "VideoLibrary.GetEpisodes",
            {
                "tvshowid": kodi_id,
                "properties": ["playcount", "tvshowid"],
                "limits": {"start": start, "end": start + 100},
            },
        )
        page = response.get("episodes") or []
        if not isinstance(page, list):
            raise ConnectorError("Kodi n'a pas retourné les épisodes de la série.")
        for episode in page:
            if episode.get("tvshowid", kodi_id) != kodi_id:
                raise ConnectorError("Kodi a retourné un épisode d'une autre série.")
            if type(episode.get("episodeid")) is not int:
                raise ConnectorError("Un épisode Kodi ne possède pas d'identifiant valide.")
        episodes.extend(page)
        total = (response.get("limits") or {}).get("total", start + len(page))
        if len(episodes) >= total:
            break
        if not page or len(episodes) > 10000:
            raise ConnectorError("Pagination Kodi invalide : lecture des épisodes interrompue.")
        start += len(page)
    if not episodes:
        raise ConnectorError("La série Kodi n'a aucun épisode à marquer vu/non vu.")
    for episode in episodes:
        playcount = int(episode.get("playcount") or 0)
        response = await rpc(
            "VideoLibrary.SetEpisodeDetails",
            {
                "episodeid": episode["episodeid"],
                "playcount": max(1, playcount) if watched else 0,
            },
        )
        if response != "OK":
            raise ConnectorError("Kodi a refusé la modification d'un épisode de la série.")


async def _write_kodi(
    player: dict, detail: dict, editions: list[dict], changes: dict
) -> str | None:
    kind = detail["kind"]
    if kind not in _KODI_METHODS:
        raise ConnectorError("Ce type de média ne peut pas être modifié dans Kodi.")
    id_field, response_key, read_method, write_method = _KODI_METHODS[kind]
    # Movie versions share the same Kodi movieid: issue just one update.
    distinct: dict[int, dict] = {}
    for edition in editions:
        kodi_id = edition.get("metadata", {}).get("kodi_id")
        if type(kodi_id) is not int or kodi_id < 0:
            raise ConnectorError("Identifiant Kodi manquant. Synchronisez la source.")
        distinct.setdefault(kodi_id, edition)

    updates: dict[str, Any] = {}
    warning = None
    if "user_rating" in changes and changes["user_rating"] is None:
        updates["userrating"] = 0
    elif changes.get("user_rating") is not None:
        # Kodi's userrating property is an integer, whereas the local UI
        # permits halves. Preserve the precise value locally.
        rating = float(changes["user_rating"])
        rounded = math.floor(rating + 0.5)
        updates["userrating"] = rounded
        if rounded != rating:
            warning = f"Kodi accepte uniquement les notes entières : note arrondie à {rounded}/10."
    if type(changes.get("watched")) is bool:
        updates["watched"] = changes["watched"]
    if not updates:
        return None

    # A disconnected Kodi must not hold the HTTP PATCH for 15 seconds per
    # JSON-RPC operation. A one-second transport timeout signals KodiUnavailable,
    # allowing the existing opt-in, validated MariaDB fallback.
    # This applies to writeback only; playback and connection tests retain 15 s.
    async with connection(player["config"], timeout=1.0) as rpc:
        permissions = await rpc("JSONRPC.Permission", {})
        if (
            not isinstance(permissions, dict)
            or permissions.get("UpdateData", permissions.get("updatedata")) is not True
        ):
            raise ConnectorError(
                "Kodi n'autorise pas la modification de la bibliothèque (UpdateData)."
            )
        for kodi_id, edition in distinct.items():
            result = await rpc(
                "VideoLibrary." + read_method,
                {
                    id_field: kodi_id,
                    "properties": ["uniqueid", "title", "playcount", "season", "episode"]
                    if kind == "episode"
                    else ["uniqueid", "title", "playcount"],
                },
            )
            item = result.get(response_key) if isinstance(result, dict) else None
            if not isinstance(item, dict):
                raise ConnectorError(
                    "Ce média n'existe plus dans la bibliothèque Kodi cible. Synchronisez la source."
                )
            _verify_identity(item, edition, detail)
            params = {id_field: kodi_id}
            if "userrating" in updates:
                params["userrating"] = updates["userrating"]
            if "watched" in updates and kind != "tvshow":
                # Retain existing positive play counts when marking as watched.
                count = item.get("playcount")
                params["playcount"] = max(1, int(count or 0)) if updates["watched"] else 0
            # Do not call SetTVShowDetails with just the ID: use the episode
            # setters instead, since series playcount is an aggregate.
            if len(params) > 1:
                response = await rpc("VideoLibrary." + write_method, params)
                if response != "OK":
                    raise ConnectorError("Kodi n'a pas confirmé la modification du média.")
            if kind == "tvshow" and "watched" in updates:
                await _update_kodi_show_episodes(rpc, kodi_id, updates["watched"])
    return warning


async def propagate_personal(
    detail: dict,
    changes: dict,
    sources: list[dict],
    players: list[dict],
) -> list[dict]:
    """Return explicit statuses; source XML files are never modified.

    The caller persists local preferences even if an upstream service fails.
    Adding new source types only requires a field capability and adapter.
    """
    editions_by_source: dict[str, list[dict]] = defaultdict(list)
    for edition in detail["editions"]:
        if edition["source_type"] in WRITABLE_FIELDS:
            editions_by_source[edition["source_id"]].append(edition)
    source_info = {source["id"]: source for source in sources}
    results = []
    for source_id, editions in editions_by_source.items():
        kind = editions[0]["source_type"]
        fields = WRITABLE_FIELDS[kind] & changes.keys()
        if not any(changes[field] is not None or field == "user_rating" for field in fields):
            continue
        source = source_info.get(source_id, {})
        if kind == "mymovies" and not source.get("config", {}).get("remote_enabled"):
            # An XML-only source is intentionally read-only, not an error.
            continue
        entry = {
            "source_id": source_id,
            "source_name": source.get("name") or editions[0]["source_name"],
            "source_type": kind,
        }
        if not source.get("enabled", True):
            results.append(
                {**entry, "status": "unavailable", "message": "La source est désactivée."}
            )
            continue
        if kind == "mymovies":
            if type(changes.get("watched")) is not bool:
                continue
            try:
                warning = await write_mymovies(source, editions, changes)
                results.append(
                    {
                        **entry,
                        "status": "updated",
                        "method": "mymovies",
                        "attempts": ["mymovies:updated"],
                        "message": warning,
                    }
                )
            except ConnectorError as exc:
                results.append({**entry, "status": "failed", "message": str(exc)})
            except Exception:
                _LOG.exception("Unexpected My Movies writeback error for source %s", source_id)
                results.append(
                    {
                        **entry,
                        "status": "failed",
                        "message": "Erreur inattendue lors de la mise à jour My Movies.",
                    }
                )
            continue

        candidates = [player for player in players if player["source_id"] == source_id]
        if not candidates:
            # SQL can also serve a source without a registered Kodi player.
            if source.get("config", {}).get("mariadb_write_enabled"):
                try:
                    warning = await asyncio.to_thread(
                        write_kodi_mariadb, source, detail, editions, changes
                    )
                    results.append(
                        {
                            **entry,
                            "status": "updated",
                            "method": "mariadb",
                            "attempts": ["jsonrpc:not_configured", "mariadb:updated"],
                            "message": warning,
                        }
                    )
                except ConnectorError as exc:
                    results.append(
                        {
                            **entry,
                            "status": "failed",
                            "attempts": ["jsonrpc:not_configured", "mariadb:failed"],
                            "message": str(exc),
                        }
                    )
            else:
                results.append(
                    {
                        **entry,
                        "status": "unavailable",
                        "attempts": ["jsonrpc:not_configured"],
                        "message": "Aucune instance Kodi JSON-RPC n'est configurée pour cette source.",
                    }
                )
            continue
        # Multiple players can share one Kodi database. Only one success needed.
        errors = []
        attempts = []
        deadline = asyncio.get_running_loop().time() + 1.0
        jsonrpc_updated = False
        for player in candidates:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                attempts.append("jsonrpc:unavailable")
                errors.append("Kodi est injoignable après une seconde.")
                break
            try:
                warning = await asyncio.wait_for(
                    _write_kodi(player, detail, editions, changes), timeout=remaining
                )
                attempts.append("jsonrpc:updated")
                jsonrpc_updated = True
                results.append(
                    {
                        **entry,
                        "status": "updated",
                        "method": "jsonrpc",
                        "attempts": attempts,
                        "message": warning,
                    }
                )
                break
            except TimeoutError:
                attempts.append("jsonrpc:unavailable")
                errors.append("Kodi est injoignable après une seconde.")
            except KodiUnavailable as exc:
                attempts.append("jsonrpc:unavailable")
                errors.append(str(exc))
            except ConnectorError as exc:
                attempts.append("jsonrpc:rejected")
                errors.append(str(exc))
            except Exception:
                attempts.append("jsonrpc:error")
                _LOG.exception("Unexpected Kodi writeback error for source %s", source_id)
                errors.append("Erreur inattendue lors de la mise à jour Kodi.")
        if not jsonrpc_updated:
            # An explicitly authorized SQL fallback is also appropriate
            # when Kodi is reachable but refuses library updates. SQL uses
            # separate server-side credentials and independently verifies
            # the exact MyVideos media identity before writing. Therefore
            # JSON-RPC permission failures must not strand pending edits.
            if source.get("config", {}).get("mariadb_write_enabled"):
                try:
                    warning = await asyncio.to_thread(
                        write_kodi_mariadb, source, detail, editions, changes
                    )
                    attempts.append("mariadb:updated")
                    results.append(
                        {
                            **entry,
                            "status": "updated",
                            "method": "mariadb",
                            "attempts": attempts,
                            "message": warning,
                        }
                    )
                    continue
                except ConnectorError as exc:
                    attempts.append("mariadb:failed")
                    errors.append(str(exc))
                except Exception:
                    attempts.append("mariadb:error")
                    _LOG.exception("Unexpected Kodi MariaDB fallback failure for %s", source_id)
                    errors.append("Échec inattendu du repli MariaDB.")
            results.append(
                {
                    **entry,
                    "status": "failed",
                    "attempts": attempts,
                    "message": errors[-1] if errors else "Échec de la synchronisation Kodi.",
                }
            )
    return results
