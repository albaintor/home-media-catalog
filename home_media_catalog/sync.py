"""Background imports with one job per source and atomic snapshot publication."""

import asyncio
import logging
from datetime import UTC, datetime
from time import monotonic
from typing import Any

from .connectors.base import ConnectorError
from .connectors.clz import parse_clz
from .connectors.collection_xml import COLLECTION_TYPES, parse_collection
from .connectors.kodi import KodiConnector
from .connectors.kodi_jsonrpc import fetch_kodi_snapshot
from .connectors.kodi_music import KodiMusicConnector, fetch_kodi_music_snapshot
from .connectors.music_collections import parse_music_collection
from .kodi_scan import KodiScanUnavailable, clean_until_finished, scan_until_finished
from .music import MUSIC_COLLECTION_TYPES, MusicCatalogue
from .players import KodiUnavailable, connection

LOGGER = logging.getLogger(__name__)


class BusyError(Exception):
    pass


class SyncManager:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        self.active: set[str] = set()
        self.tasks: set[asyncio.Task] = set()
        self.scan_tasks: set[asyncio.Task] = set()
        self.stale_sync_lock = asyncio.Lock()
        self.on_catalog_changed = None  # Optional non-blocking post-publish hook.
        # Prefer the last player that delivered a complete snapshot for each source.
        self.last_reachable_kodi_players: dict[str, str] = {}

    async def submit(self, source_id: str, xml: bytes | None = None, *,
                     mariadb_only: bool = False) -> dict[str, Any]:
        if source_id in self.active:
            raise BusyError("An import or synchronization is already running for this source.")
        source = await asyncio.to_thread(self.store.source, source_id, private=True)
        if not source:
            raise ValueError("Unknown source.")
        if source["type"] in MUSIC_COLLECTION_TYPES:
            raise ValueError("Music collections are refreshed by uploading their export.")
        if xml is None and source["type"] != "kodi":
            raise ValueError("Collection sources are refreshed by uploading an XML export.")
        if xml is not None and source["type"] not in COLLECTION_TYPES:
            raise ValueError("XML imports require a collection source.")
        if mariadb_only and (xml is not None or source["type"] != "kodi"):
            raise ValueError("MariaDB-only refresh is available for Kodi sources only.")
        if mariadb_only and not (
            source["config"].get("host") and source["config"].get("username")
        ):
            raise ValueError("Configure a MariaDB host and username before refreshing without Kodi.")
        if not source["enabled"]:
            raise ValueError("Enable the source before synchronizing it.")
        # No await between checking again and claiming the source.
        if source_id in self.active:
            raise BusyError("A synchronization is already running for this source.")
        self.active.add(source_id)
        try:
            job = await asyncio.to_thread(
                self.store.new_job,
                source_id,
                "collection_import" if xml is not None else "kodi_sync",
            )
        except Exception:
            self.active.remove(source_id)
            raise
        task = asyncio.create_task(self._run(job, source, xml, mariadb_only=mariadb_only))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return job

    async def submit_clean(self, source_id: str, player: dict) -> dict[str, Any]:
        """Clean orphans on one Kodi instance and publish only after confirmation."""
        if source_id in self.active:
            raise BusyError("Une opération est déjà en cours pour cette source.")
        source = await asyncio.to_thread(self.store.source, source_id, private=True)
        if not source or source["type"] != "kodi" or not source["enabled"]:
            raise ValueError("Source Kodi inconnue ou désactivée.")
        if player["source_id"] != source_id:
            raise ValueError("Instance Kodi non associée à cette source.")
        self.active.add(source_id)
        try:
            job = await asyncio.to_thread(self.store.new_job, source_id, "kodi_clean")
        except Exception:
            self.active.discard(source_id)
            raise
        task = asyncio.create_task(self._run_clean(job, source, player))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return job

    async def _run_clean(self, job: dict, source: dict, player: dict) -> None:
        try:
            await asyncio.to_thread(self.store.start_job, job["id"])
            await asyncio.to_thread(self.store.update_job_progress, job["id"], "kodi_cleaning")
            method = await clean_until_finished(player["config"])
            await asyncio.to_thread(
                self.store.update_job_progress, job["id"], "kodi_clean_finished"
            )
            # _run will not replace the catalogue when its follow-up import
            # fails: the existing atomic snapshot guard remains in effect.
            await self._run(
                job,
                source,
                None,
                scan_triggered=True,
                scan_info={
                    "kodi_clean": "finished",
                    "kodi_clean_method": method,
                    "kodi_clean_player": player["name"],
                },
            )
        except (KodiScanUnavailable, ConnectorError) as exc:
            await asyncio.to_thread(self.store.finish_job, job["id"], error=str(exc))
        except asyncio.CancelledError:
            await asyncio.to_thread(
                self.store.finish_job,
                job["id"],
                error="Nettoyage Kodi interrompu avant confirmation de fin.",
            )
            raise
        except Exception:
            LOGGER.exception("Unexpected Kodi cleanup failure")
            await asyncio.to_thread(
                self.store.finish_job,
                job["id"],
                error="Nettoyage Kodi non confirmé ; catalogue local inchangé.",
            )
        finally:
            self.active.discard(source["id"])

    async def submit_scan(self, source_id: str, player: dict) -> dict[str, Any]:
        return await self.submit_scans(source_id, [player])

    async def submit_scans(self, source_id: str, players: list[dict]) -> dict[str, Any]:
        """Reserve one job per source, scanning all configured instances first."""
        if source_id in self.active:
            raise BusyError("Une actualisation est déjà en cours pour cette source.")
        source = await asyncio.to_thread(self.store.source, source_id, private=True)
        if not source or source["type"] != "kodi" or not source["enabled"]:
            raise ValueError("Source Kodi inconnue ou désactivée.")
        if not players or any(player["source_id"] != source_id for player in players):
            raise ValueError("Aucune instance Kodi valide pour cette source.")
        self.active.add(source_id)
        try:
            job = await asyncio.to_thread(self.store.new_job, source_id, "kodi_scan")
        except Exception:
            self.active.discard(source_id)
            raise
        task = asyncio.create_task(self._run_scan(job, source, players))
        self.tasks.add(task)
        self.scan_tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        task.add_done_callback(self.scan_tasks.discard)
        return job

    async def _run_scan(self, job: dict, source: dict, players: list[dict]) -> None:
        try:
            await asyncio.to_thread(self.store.start_job, job["id"])
            scanned, warnings = [], []
            offline_player_ids = set()
            total = len(players)
            for index, player in enumerate(players):
                await asyncio.to_thread(
                    self.store.update_job_progress, job["id"], "kodi_scanning", index, total
                )
                try:
                    # Check offline hosts before starting a potentially long scan.
                    async with asyncio.timeout(1.0):
                        async with connection(player["config"], timeout=0.9) as rpc:
                            if await rpc("JSONRPC.Ping", {}) != "pong":
                                raise KodiScanUnavailable("Kodi JSON-RPC ping did not respond.")
                    method = await scan_until_finished(player["config"])
                    scanned.append({"name": player["name"], "method": method})
                except (KodiUnavailable, TimeoutError) as exc:
                    offline_player_ids.add(player["id"])
                    warnings.append(f"{player['name']} : {exc}")
                except KodiScanUnavailable as exc:
                    warnings.append(f"{player['name']} : {exc}")
                await asyncio.to_thread(
                    self.store.update_job_progress, job["id"], "kodi_scanning", index + 1, total
                )
            phase = "kodi_scan_finished" if scanned else "kodi_sql_fallback"
            await asyncio.to_thread(self.store.update_job_progress, job["id"], phase)
            await self._run(
                job,
                source,
                None,
                scan_triggered=bool(scanned),
                scan_info={"kodi_scanned_players": scanned, "kodi_scan_warnings": warnings},
                offline_player_ids=offline_player_ids,
            )
        except ConnectorError as exc:
            await asyncio.to_thread(self.store.finish_job, job["id"], error=str(exc))
        except asyncio.CancelledError:
            await asyncio.to_thread(
                self.store.finish_job,
                job["id"],
                error="Analyse Kodi interrompue avant confirmation de fin.",
            )
            raise
        except Exception:
            LOGGER.exception("Unexpected Kodi scan monitoring failure")
            await asyncio.to_thread(
                self.store.finish_job,
                job["id"],
                error="Erreur de suivi de l'analyse Kodi ; catalogue local non actualisé.",
            )
        finally:
            self.active.discard(source["id"])

    async def _run(
        self,
        job,
        source,
        xml,
        *,
        scan_triggered: bool = False,
        scan_info: dict | None = None,
        offline_player_ids: set[str] | None = None,
        mariadb_only: bool = False,
    ):
        try:
            await asyncio.to_thread(self.store.start_job, job["id"])
            if xml is not None:
                await asyncio.to_thread(
                    self.store.update_job_progress, job["id"], f"parsing_{source['type']}"
                )
                if source["type"] == "clz":
                    records = await asyncio.to_thread(parse_clz, xml)
                else:
                    records = await asyncio.to_thread(parse_collection, xml, source["type"])
                database = None
            else:
                database = None
                connector = None
                mariadb_read_ms = None
                transport = None
                players = await asyncio.to_thread(self.store.players)
                candidates = [p for p in players if p["source_id"] == source["id"]]
                offline = bool(candidates)
                # JSON-RPC exposes only the default file for a Kodi movie.
                # A merged standalone/server backup can contain several
                # file-specific records under the same canonical source ID.
                # Protect them during ALL imports (not just Kodi cleanup).
                # The record keys indicate multiple file versions, not how or
                # when those records were originally imported.
                protected_versions = await asyncio.to_thread(
                    self.store.has_multiple_kodi_movie_versions, source["id"]
                )
                mariadb_available = bool(
                    source["config"].get("host") and source["config"].get("username")
                )
                if protected_versions and not mariadb_available:
                    raise ConnectorError(
                        "Cette source contient plusieurs éditions de certains films. "
                        "Une synchronisation JSON-RPC seule risquerait de supprimer "
                        "leurs fichiers supplémentaires. Configurez le repli MariaDB "
                        "pour les préserver ; le catalogue reste inchangé."
                    )
                # A catalog-only refresh requested by iOS can read the NAS
                # database directly, even when no Kodi instance exists.
                prefer_sql = mariadb_available and (protected_versions or mariadb_only)
                preferred_player = self.last_reachable_kodi_players.get(source["id"])
                ordered_candidates = sorted(
                    candidates, key=lambda player: player["id"] != preferred_player
                )
                for player in [] if prefer_sql else ordered_candidates:
                    if player["id"] in (offline_player_ids or set()):
                        continue
                    private = await asyncio.to_thread(self.store.player, player["id"])
                    if private is None:
                        continue
                    try:
                        # Each instance has its OWN three-second probe budget.
                        # A failed host must not consume the budget of later hosts.
                        # Full-library transfer is allowed longer once reachable.
                        async with asyncio.timeout(3.0):
                            async with connection(private["config"], timeout=3.0) as rpc:
                                if await rpc("JSONRPC.Ping", {}) != "pong":
                                    raise ConnectorError("Kodi returned an invalid JSON-RPC ping.")
                        records = await fetch_kodi_snapshot(
                            private["config"],
                            progress=lambda phase, current, total: self.store.update_job_progress(
                                job["id"], phase, current, total
                            ),
                        )
                        offline = False
                        transport = "jsonrpc"
                        self.last_reachable_kodi_players[source["id"]] = player["id"]
                        break
                    except (ConnectorError, TimeoutError) as exc:
                        LOGGER.info("Kodi instance %s could not supply a snapshot: %s", player["name"], exc)
                        continue
                if transport is None:
                    if not source["config"].get("host") or not source["config"].get("username"):
                        raise ConnectorError(
                            "Kodi JSON-RPC est indisponible et aucun repli MariaDB n'est configuré."
                            if offline
                            else "Configurez une instance Kodi JSON-RPC ou un repli MariaDB."
                        )
                    connector = KodiConnector(
                        source["config"],
                        progress=lambda phase, current, total: self.store.update_job_progress(
                            job["id"], phase, current, total
                        ),
                    )
                    fetch_started = monotonic()
                    records = await asyncio.to_thread(connector.fetch)
                    mariadb_read_ms = round((monotonic() - fetch_started) * 1000)
                    database = connector.database
                    transport = "mariadb"
            if (
                xml is None
                and transport == "jsonrpc"
                and await asyncio.to_thread(
                    self.store.has_multiple_kodi_movie_versions, source["id"]
                )
            ):
                # Defensive recheck in case records changed while downloading
                # the RPC snapshot. Never publish a partial set of editions.
                raise ConnectorError(
                    "Plusieurs éditions d'un même film sont enregistrées dans le catalogue. "
                    "JSON-RPC ne les expose pas toutes : import annulé sans modifier "
                    "les éditions existantes. Configurez le repli MariaDB."
                )
            await asyncio.to_thread(self.store.update_job_progress, job["id"], "publishing")
            publish_started = monotonic()
            result = await asyncio.to_thread(self.store.apply_snapshot, source["id"], records)
            result["sqlite_publish_ms"] = round((monotonic() - publish_started) * 1000)
            if scan_info is not None:
                if "kodi_clean" not in scan_info:
                    result["kodi_scan"] = (
                        "finished"
                        if scan_triggered
                        else "sql_only"
                        if transport == "mariadb"
                        else "jsonrpc_only"
                    )
                result.update(scan_info)
            if xml is None:
                result["import_method"] = transport
                if protected_versions and prefer_sql:
                    result["edition_protection"] = "mariadb"
            if database:
                result["database"] = database
                result["mariadb_read_ms"] = mariadb_read_ms
                result.update(connector.snapshot_report)
            if xml is None and source["config"].get("music_enabled"):
                # Music is best-effort: a failing AudioLibrary never fails or
                # rolls back the video snapshot that was just published.
                result.update(await self._music_from_kodi(job, source, transport))
            await asyncio.to_thread(self.store.finish_job, job["id"], result=result)
            if result.get("rebuilt_works") and self.on_catalog_changed:
                # Failure to refresh the optional AI index must never roll back
                # or change the status of a successful Kodi/collection import.
                try:
                    self.on_catalog_changed()
                except Exception:
                    LOGGER.exception("Failed to schedule optional semantic indexing")
        except (ConnectorError, ValueError) as exc:
            await asyncio.to_thread(self.store.finish_job, job["id"], error=str(exc))
        except Exception:
            LOGGER.exception("Unexpected catalogue job failure")
            await asyncio.to_thread(
                self.store.finish_job,
                job["id"],
                error="Unexpected import failure; see server logs. The previous catalogue was retained if publication failed.",
            )
        finally:
            self.active.discard(source["id"])

    # ------------------------------------------------------------------ music
    async def submit_music(self, source_id: str, data: bytes | None = None, *,
                           mariadb_only: bool = False) -> dict[str, Any]:
        """Kodi AudioLibrary refresh (data=None) or a physical collection import."""
        if source_id in self.active:
            raise BusyError("An import or synchronization is already running for this source.")
        source = await asyncio.to_thread(self.store.source, source_id, private=True)
        if not source:
            raise ValueError("Unknown source.")
        if not source["enabled"]:
            raise ValueError("Enable the source before synchronizing it.")
        if data is None and source["type"] != "kodi":
            raise ValueError("Music collections are refreshed by uploading their export.")
        if data is not None and source["type"] not in MUSIC_COLLECTION_TYPES:
            raise ValueError("Music imports require a music collection source.")
        if source_id in self.active:
            raise BusyError("A synchronization is already running for this source.")
        self.active.add(source_id)
        try:
            job = await asyncio.to_thread(
                self.store.new_job, source_id, "music_import" if data is not None else "music_sync"
            )
        except Exception:
            self.active.discard(source_id)
            raise
        task = asyncio.create_task(self._run_music(job, source, data, mariadb_only))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return job

    async def _run_music(self, job, source, data, mariadb_only):
        try:
            await asyncio.to_thread(self.store.start_job, job["id"])
            if data is not None:
                await asyncio.to_thread(
                    self.store.update_job_progress, job["id"], f"parsing_{source['type']}"
                )
                snapshot = await asyncio.to_thread(parse_music_collection, data, source["type"])
                await asyncio.to_thread(self.store.update_job_progress, job["id"], "publishing")
                result = await asyncio.to_thread(
                    MusicCatalogue(self.store).apply_snapshot, source["id"], snapshot
                )
            else:
                result = await self._music_from_kodi(job, source, None, raise_errors=True,
                                                     mariadb_only=mariadb_only)
            await asyncio.to_thread(self.store.finish_job, job["id"], result=result)
        except (ConnectorError, ValueError) as exc:
            await asyncio.to_thread(self.store.finish_job, job["id"], error=str(exc))
        except Exception:
            LOGGER.exception("Unexpected music job failure")
            await asyncio.to_thread(
                self.store.finish_job,
                job["id"],
                error="Unexpected music import failure; see server logs. "
                "The previous music catalogue was retained.",
            )
        finally:
            self.active.discard(source["id"])

    async def _music_from_kodi(self, job, source, video_transport, *, raise_errors=False,
                               mariadb_only=False) -> dict[str, Any]:
        """AudioLibrary via the reachable Kodi instance, else the MyMusic schema."""

        def progress(phase, current, total):
            self.store.update_job_progress(job["id"], phase, current, total)

        snapshot = None
        errors = []
        mariadb_available = bool(
            source["config"].get("host") and source["config"].get("username")
        )
        try:
            if not mariadb_only and video_transport != "mariadb":
                players = await asyncio.to_thread(self.store.players)
                candidates = [p for p in players if p["source_id"] == source["id"]]
                preferred = self.last_reachable_kodi_players.get(source["id"])
                candidates.sort(key=lambda player: player["id"] != preferred)
                for player in candidates:
                    private = await asyncio.to_thread(self.store.player, player["id"])
                    if private is None:
                        continue
                    try:
                        async with asyncio.timeout(3.0):
                            async with connection(private["config"], timeout=3.0) as rpc:
                                if await rpc("JSONRPC.Ping", {}) != "pong":
                                    raise ConnectorError("Kodi returned an invalid ping.")
                        snapshot = await fetch_kodi_music_snapshot(private["config"], progress)
                        break
                    except (ConnectorError, TimeoutError) as exc:
                        errors.append(f"{player['name']}: {exc}")
            if snapshot is None and mariadb_available:
                connector = KodiMusicConnector(source["config"], progress=progress)
                snapshot = await asyncio.to_thread(connector.fetch_music)
            if snapshot is None:
                raise ConnectorError(
                    "Kodi AudioLibrary is unavailable and no MariaDB fallback is configured."
                    + (f" ({'; '.join(errors)})" if errors else "")
                )
            await asyncio.to_thread(self.store.update_job_progress, job["id"], "publishing")
            return await asyncio.to_thread(
                MusicCatalogue(self.store).apply_snapshot, source["id"], snapshot
            )
        except (ConnectorError, ValueError) as exc:
            if raise_errors:
                raise
            return {"music_error": str(exc)}

    async def sync_stale_on_open(self) -> list[dict[str, Any]]:
        """Schedule a SQL-only refresh on open if the last successful one exceeds 24h.

        Serialize the due check and job creation, so concurrent web/iOS opens
        do not submit the same source twice within the running server process.
        """
        cutoff = datetime.now(UTC).timestamp() - 24 * 60 * 60
        created = []
        async with self.stale_sync_lock:
            for source in await asyncio.to_thread(self.store.sources):
                if source["type"] != "kodi" or not source["enabled"]:
                    continue
                if source["id"] in self.active:
                    continue
                last = source.get("last_sync")
                if last:
                    try:
                        date = datetime.fromisoformat(last.replace("Z", "+00:00"))
                        if date.tzinfo is None:
                            date = date.replace(tzinfo=UTC)
                        if date.timestamp() >= cutoff:
                            continue
                    except (TypeError, ValueError, OverflowError):
                        pass
                # Last successful publication can stay stale when Kodi is
                # offline or a MariaDB import fails. A durable attempt cooldown
                # prevents repeated automatic imports on every web/iOS launch.
                # The explicit /sources/{id}/sync action bypasses this limit.
                last_attempt = await asyncio.to_thread(
                    self.store.last_kodi_sync_attempt, source["id"]
                )
                if last_attempt:
                    try:
                        attempted = datetime.fromisoformat(last_attempt.replace("Z", "+00:00"))
                        if attempted.tzinfo is None:
                            attempted = attempted.replace(tzinfo=UTC)
                        if attempted.timestamp() >= cutoff:
                            continue
                    except (TypeError, ValueError, OverflowError):
                        pass
                # Auto-import on an empty catalogue must also be bounded
                # across container restarts if Kodi is repeatedly unavailable.
                last_attempt = await asyncio.to_thread(
                    self.store.last_kodi_sync_attempt, source["id"]
                )
                if last_attempt:
                    try:
                        attempt = datetime.fromisoformat(last_attempt.replace("Z", "+00:00"))
                        if attempt.tzinfo is None:
                            attempt = attempt.replace(tzinfo=UTC)
                        if (datetime.now(UTC) - attempt).total_seconds() < 24 * 60 * 60:
                            continue
                    except (TypeError, ValueError, OverflowError):
                        pass
                try:
                    created.append(await self.submit(source["id"]))
                except (BusyError, ValueError):
                    continue
        return created

    async def bootstrap_empty_catalogue(self) -> list[dict[str, Any]]:
        """Do the first import when the database has no records.

        Existing populated catalogues must *not* synchronize on server startup.
        Once the first import is complete, only explicit web/iOS opening checks
        (or manual actions) can trigger subsequent imports.
        """
        async with self.stale_sync_lock:
            status = await asyncio.to_thread(self.store.status)
            if status["editions"] > 0:
                return []
            created = []
            for source in await asyncio.to_thread(self.store.sources):
                if source["type"] != "kodi" or not source["enabled"]:
                    continue
                if source["id"] in self.active:
                    continue
                # A successfully checked but genuinely empty Kodi library is
                # not imported again on every server/container restart.
                last = source.get("last_sync")
                if last:
                    try:
                        dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=UTC)
                        if (datetime.now(UTC) - dt).total_seconds() < 24 * 60 * 60:
                            continue
                    except (ValueError, TypeError, OverflowError):
                        pass
                # An empty catalogue may indicate a failed previous import,
                # not just the first launch. Prevent hourly server restarts
                # from repeatedly contacting unreachable Kodi instances.
                last_attempt = await asyncio.to_thread(
                    self.store.last_kodi_sync_attempt, source["id"]
                )
                if last_attempt:
                    try:
                        attempt = datetime.fromisoformat(last_attempt.replace("Z", "+00:00"))
                        if attempt.tzinfo is None:
                            attempt = attempt.replace(tzinfo=UTC)
                        if (datetime.now(UTC) - attempt).total_seconds() < 24 * 60 * 60:
                            continue
                    except (TypeError, ValueError, OverflowError):
                        pass
                try:
                    created.append(await self.submit(source["id"]))
                except (BusyError, ValueError):
                    continue
            return created

    async def close(self):
        # Scan listeners must not delay shutdown indefinitely.
        for task in self.scan_tasks:
            task.cancel()
        # Let regular imports finish before the database/container exits.
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
