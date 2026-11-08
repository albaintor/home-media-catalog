"""Launch Kodi video scans with a short WebSocket probe and HTTP JSON-RPC fallback.

Only publish a new local MariaDB snapshot after Kodi scan completion can be
confirmed. An unreachable remote may be reported to callers as unavailable,
allowing an explicitly labelled SQL-only refresh of existing Kodi data.
"""

import asyncio
import json
from time import monotonic
from urllib.parse import urlsplit

import aiohttp

from .connectors.base import ConnectorError
from .players import KodiUnavailable, connection


class KodiScanUnavailable(ConnectorError):
    """No scan was started; the caller may refresh existing SQL data only."""


def websocket_url(config: dict) -> str:
    host = config.get("host", "")
    if not host or any(c in host for c in ("/", "@", "?", "#", "\\")):
        raise KodiScanUnavailable("Adresse Kodi WebSocket invalide.")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = config.get("websocket_port", 9090)
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise KodiScanUnavailable("Port WebSocket Kodi invalide.")
    url = f"ws://{host}:{port}/jsonrpc"
    if urlsplit(url).hostname is None:
        raise KodiScanUnavailable("Adresse WebSocket Kodi invalide.")
    return url


def _allowed(permissions):
    return isinstance(permissions, dict) and (
        permissions.get("UpdateData", permissions.get("updatedata")) is True
    )


async def _websocket_scan(config: dict) -> None:
    """Subscribe before sending Scan; only OnScanFinished confirms completion."""
    started_request = False
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, connect=4, sock_connect=4),
            trust_env=False,
        ) as client:
            async with client.ws_connect(
                websocket_url(config),
                timeout=aiohttp.ClientWSTimeout(ws_close=5),
                heartbeat=30,
                max_msg_size=256_000,
            ) as ws:

                async def rpc(method, rpc_id, params=None):
                    await ws.send_json(
                        {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params or {}}
                    )

                async def read():
                    message = await ws.receive()
                    if message.type != aiohttp.WSMsgType.TEXT:
                        raise ConnectorError("Connexion Kodi WebSocket interrompue.")
                    try:
                        value = json.loads(message.data)
                    except (ValueError, TypeError) as exc:
                        raise ConnectorError("Notification Kodi WebSocket invalide.") from exc
                    if not isinstance(value, dict):
                        raise ConnectorError("Réponse Kodi WebSocket inattendue.")
                    return value

                await rpc("JSONRPC.Permission", 1)
                async with asyncio.timeout(4):
                    while True:
                        response = await read()
                        if response.get("id") != 1:
                            continue
                        if not _allowed(response.get("result")):
                            raise KodiScanUnavailable("WebSocket Kodi sans droit UpdateData.")
                        break

                await rpc("VideoLibrary.Scan", 2, {"showdialogs": False})
                started_request = True
                acknowledged = started = finished = False
                async with asyncio.timeout(1800):
                    while not (acknowledged and started and finished):
                        response = await read()
                        method = response.get("method")
                        if method == "VideoLibrary.OnScanStarted":
                            started = True
                        elif method == "VideoLibrary.OnScanFinished" and started:
                            finished = True
                        elif response.get("id") == 2:
                            if response.get("result") != "OK":
                                raise ConnectorError("Kodi a refusé VideoLibrary.Scan.")
                            acknowledged = True
    except KodiScanUnavailable:
        raise
    except ConnectorError as exc:
        if not started_request:
            raise KodiScanUnavailable("WebSocket Kodi inutilisable avant analyse.") from exc
        raise
    except (TimeoutError, aiohttp.ClientError, OSError) as exc:
        if not started_request:
            raise KodiScanUnavailable(
                "WebSocket Kodi indisponible (port 9090 ou configuration)."
            ) from exc
        raise ConnectorError(
            "Analyse Kodi déclenchée, mais fin non confirmée par WebSocket ; "
            "le catalogue local reste inchangé."
        ) from exc


async def _http_scan(config: dict) -> None:
    """HTTP JSON-RPC shares the connection tested in the web settings.

    Poll Kodi's scanning info boolean. Allow brief fast scans which have
    finished before the first poll, but never treat an unsupported response
    as confirmation of completion.
    """
    started = False
    try:
        async with connection(config) as rpc:
            async with asyncio.timeout(6):
                permissions = await rpc("JSONRPC.Permission", {})
                if not _allowed(permissions):
                    raise KodiScanUnavailable("Kodi HTTP ne permet pas UpdateData.")
                if await rpc("VideoLibrary.Scan", {"showdialogs": False}) != "OK":
                    raise KodiScanUnavailable("Kodi HTTP a refusé VideoLibrary.Scan.")
                started = True
            saw_scanning = False
            idle_samples = 0
            began = monotonic()
            async with asyncio.timeout(1800):
                while True:
                    result = await rpc(
                        "XBMC.GetInfoBooleans", {"booleans": ["Library.IsScanningVideo"]}
                    )
                    scanning = (
                        result.get("Library.IsScanningVideo") if isinstance(result, dict) else None
                    )
                    if type(scanning) is not bool:
                        raise ConnectorError(
                            "Kodi ne fournit pas Library.IsScanningVideo : "
                            "fin d'analyse non vérifiable."
                        )
                    if scanning:
                        saw_scanning = True
                        idle_samples = 0
                    else:
                        idle_samples += 1
                        # For a very fast scan, allow a short stabilization
                        # window rather than waiting indefinitely for 'true'.
                        if saw_scanning or (monotonic() - began >= 4 and idle_samples >= 3):
                            return
                    await asyncio.sleep(1)
    except (KodiUnavailable, TimeoutError) as exc:
        if not started:
            raise KodiScanUnavailable("Kodi HTTP JSON-RPC indisponible après 6 secondes.") from exc
        raise ConnectorError(
            "Analyse Kodi démarrée, mais confirmation HTTP impossible ; "
            "synchronisation SQL différée."
        ) from exc
    except ConnectorError as exc:
        if not started and not isinstance(exc, KodiScanUnavailable):
            raise KodiScanUnavailable(str(exc)) from exc
        raise


async def scan_until_finished(config: dict) -> str:
    """Return 'websocket' or 'http'; never claim an unconfirmed scan finished."""
    try:
        await _websocket_scan(config)
        return "websocket"
    except KodiScanUnavailable:
        # WebSocket and HTTP are separate Kodi servers/ports. The HTTP
        # connection may work perfectly when port 9090 is disabled.
        await _http_scan(config)
        return "http"


async def _websocket_clean_until_finished(config: dict) -> str:
    """Clean Kodi and confirm completion with OnCleanFinished over WebSocket."""
    requested = False
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, connect=4, sock_connect=4),
            trust_env=False,
        ) as client:
            async with client.ws_connect(
                websocket_url(config),
                timeout=aiohttp.ClientWSTimeout(ws_close=5),
                heartbeat=30,
                max_msg_size=256_000,
            ) as ws:

                async def rpc(method, rpc_id, params=None):
                    await ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": rpc_id,
                            "method": method,
                            "params": params or {},
                        }
                    )

                async def read():
                    message = await ws.receive()
                    if message.type != aiohttp.WSMsgType.TEXT:
                        raise ConnectorError("Connexion Kodi WebSocket interrompue.")
                    try:
                        value = json.loads(message.data)
                    except (ValueError, TypeError) as exc:
                        raise ConnectorError("Notification Kodi WebSocket invalide.") from exc
                    if not isinstance(value, dict):
                        raise ConnectorError("Réponse Kodi WebSocket inattendue.")
                    return value

                await rpc("JSONRPC.Permission", 1)
                async with asyncio.timeout(4):
                    while True:
                        response = await read()
                        if response.get("id") == 1:
                            permissions = response.get("result")
                            if (
                                not isinstance(permissions, dict)
                                or permissions.get("RemoveData", permissions.get("removedata"))
                                is not True
                            ):
                                raise KodiScanUnavailable("Kodi WebSocket sans droit RemoveData.")
                            break

                # Subscribe before issuing cleanup, so even fast runs are observed.
                await rpc("VideoLibrary.Clean", 2, {"showdialogs": False})
                requested = True
                acknowledged = started = finished = False
                async with asyncio.timeout(1800):
                    while not (acknowledged and started and finished):
                        response = await read()
                        method = response.get("method")
                        if method == "VideoLibrary.OnCleanStarted":
                            started = True
                        elif method == "VideoLibrary.OnCleanFinished" and started:
                            finished = True
                        elif response.get("id") == 2:
                            if response.get("result") != "OK":
                                raise ConnectorError("Kodi a refusé VideoLibrary.Clean.")
                            acknowledged = True
        return "websocket"
    except KodiScanUnavailable:
        raise
    except ConnectorError as exc:
        if not requested:
            raise KodiScanUnavailable("WebSocket Kodi inutilisable avant nettoyage.") from exc
        raise
    except (TimeoutError, aiohttp.ClientError, OSError) as exc:
        if not requested:
            raise KodiScanUnavailable(
                "Le nettoyage requiert Kodi WebSocket sur le port configuré (habituellement 9090)."
            ) from exc
        raise ConnectorError(
            "Nettoyage Kodi déclenché mais fin non confirmée ; catalogue local inchangé."
        ) from exc


async def _http_clean_request(config: dict) -> str:
    """Trigger Kodi cleanup when WebSocket is disabled.

    Kodi acknowledges VideoLibrary.Clean before its background job finishes.
    Do not import or prune the local catalogue based on this acknowledgement.
    A later explicit sync can reconcile the authoritative Kodi snapshot.
    """
    try:
        async with connection(config, timeout=6) as rpc:
            permissions = await rpc("JSONRPC.Permission", {})
            if not isinstance(permissions, dict) or (
                permissions.get("RemoveData", permissions.get("removedata")) is not True
            ):
                raise KodiScanUnavailable(
                    "Kodi HTTP refuse le droit RemoveData requis pour le nettoyage."
                )
            if await rpc("VideoLibrary.Clean", {"showdialogs": False}) != "OK":
                raise KodiScanUnavailable("Kodi HTTP a refusé VideoLibrary.Clean.")
    except KodiUnavailable as exc:
        raise KodiScanUnavailable(
            "Kodi ne répond ni sur WebSocket ni sur HTTP JSON-RPC. "
            "Démarrez une instance Kodi et vérifiez son adresse, ses ports "
            "et les autorisations de contrôle distant."
        ) from exc
    return "http_requested"


async def clean_until_finished(config: dict) -> str:
    """Prefer confirmed WebSocket cleanup; otherwise request it over HTTP.

    'http_requested' is NOT a completion signal. Callers must not import a
    potentially intermediate library snapshot on that result.
    """
    try:
        return await _websocket_clean_until_finished(config)
    except KodiScanUnavailable:
        # Kodi's HTTP JSON-RPC port can be reachable while port 9090 is closed.
        # Do not retry when the WebSocket command may already have been sent.
        return await _http_clean_request(config)
