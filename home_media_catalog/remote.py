"""Kodi remote control and live playback state for the web and iOS clients.

The server owns Kodi credentials; clients never connect to Kodi directly.
"""

import asyncio
import hashlib
import hmac
import logging
import secrets
import socket
import time
from collections import OrderedDict
from urllib.parse import quote, quote_plus, urlparse, urlsplit

import aiohttp
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketDisconnect

from .artwork import Artwork, ArtworkError
from .auth import COOKIE, authenticated, secure_equal, writable
from .connectors.base import ConnectorError
from .players import connection, endpoint

LOG = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/players", tags=["Players"])
# Opaque, deterministic-per-process artwork handles keep even embedded SMB or
# HTTP image credentials out of web/iOS URLs and JSON responses.
_ARTWORK_SECRET = secrets.token_bytes(32)
_ARTWORK_REFS = OrderedDict()
_ARTWORK_MAX = 512
_ARTWORK_TTL = 3600


def _artwork_sources(media: dict) -> list[str]:
    """Kodi's art fields take priority over thumbnail, as in integration-kodi."""
    art = media.get("art") or {}
    if not isinstance(art, dict):
        art = {}
    candidates = (
        art.get("poster"),
        art.get("thumb"),
        art.get("fanart"),
        media.get("fanart"),
        media.get("thumbnail"),
    )
    return list(
        dict.fromkeys(value for value in candidates if isinstance(value, str) and value.strip())
    )


def _remember_artwork(player_id: str, kodi_image: str | list[str]) -> str:
    candidates = (kodi_image,) if isinstance(kodi_image, str) else tuple(kodi_image)
    if not candidates:
        raise ValueError("Artwork reference requires at least one image.")
    key = (
        player_id,
        hmac.new(
            _ARTWORK_SECRET,
            (player_id + "\0" + "\0".join(candidates)).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest(),
    )
    _ARTWORK_REFS[key] = (candidates, time.monotonic() + _ARTWORK_TTL)
    _ARTWORK_REFS.move_to_end(key)
    while len(_ARTWORK_REFS) > _ARTWORK_MAX:
        _ARTWORK_REFS.popitem(last=False)
    return f"/api/v1/players/{player_id}/artwork?ref={key[1]}"


def _resolve_artwork_candidates(player_id: str, ref: str) -> tuple[str, ...]:
    if len(ref) != 64 or any(c not in "0123456789abcdef" for c in ref):
        raise HTTPException(404, "Unknown Kodi artwork.")
    item = _ARTWORK_REFS.get((player_id, ref))
    if not item or time.monotonic() >= item[1]:
        raise HTTPException(404, "Kodi artwork reference expired.")
    return item[0]


def _resolve_artwork(player_id: str, ref: str) -> str:
    """Compatibility helper for a single artwork reference."""
    return _resolve_artwork_candidates(player_id, ref)[0]


def _kodi_artwork_path(image: str) -> str:
    """Match integration-kodi's two different image URL encoders exactly.

    Kodi.thumbnail_url: quote_plus(existing image:// URI).
    Kodi.get_thumbnail_from_file: quote(path) -> image://.../ -> quote again.
    """
    if urlparse(image).scheme.lower() == "image":
        return quote_plus(image)
    encoded = quote(image, safe="")
    image_url = f"image://{encoded}/"
    return quote(image_url, safe="")


def _kodi_artwork_url(config: dict, image: str) -> str:
    """Construct the same credential-encoded /image URL as integration-kodi.

    This URL only exists server-side. Clients receive opaque artwork refs.
    Never log this full URL: it contains the Kodi HTTP credentials.
    """
    address = urlsplit(endpoint(config).removesuffix("/jsonrpc"))
    user = config.get("username")
    auth = ""
    if user is not None:
        auth = f"{quote(str(user), safe='')}:{quote(str(config.get('password') or ''), safe='')}@"
    return f"{address.scheme}://{auth}{address.netloc}/image/{_kodi_artwork_path(image)}"


class RemoteAction(BaseModel):
    action: str = Field(
        pattern=r"^(up|down|left|right|select|back|home|context|info|osd|fullscreen|playpause|stop|next|previous|seek|subtitle_next|subtitle_off|subtitle_on|subtitle_select|audio_next|audio_select|set_volume|set_mute)$"
    )
    percentage: float | None = Field(default=None, ge=0, le=100)
    seconds: float | None = Field(default=None, ge=0, le=172800)
    index: int | None = Field(default=None, ge=0, le=999)
    volume: int | None = Field(default=None, ge=0, le=100)
    muted: bool | None = None


def kodi_seek_time(position: float) -> dict[str, int]:
    """JSON-RPC Player.Seek's absolute time, as in integration-kodi."""
    millis = max(0, round(position * 1000))
    seconds_total, milliseconds = divmod(millis, 1000)
    hours, remainder = divmod(seconds_total, 3600)
    minutes, secs = divmod(remainder, 60)
    return {"hours": hours, "minutes": minutes, "seconds": secs, "milliseconds": milliseconds}


def seconds(value):
    if not isinstance(value, dict):
        return 0.0
    return float(
        value.get("hours", 0) * 3600
        + value.get("minutes", 0) * 60
        + value.get("seconds", 0)
        + value.get("milliseconds", 0) / 1000
    )


async def current_state(player):
    """Fetch an authoritative snapshot after WebSocket notifications and on a slow tick."""
    async with connection(player["config"], timeout=5) as rpc:
        active = await rpc("Player.GetActivePlayers", {})
        active = next((p for p in active if p.get("type") in ("video", "audio")), None)
        if active is None:
            return {"active": False}
        pid = active["playerid"]
        props, item = await asyncio.gather(
            rpc(
                "Player.GetProperties",
                {
                    "playerid": pid,
                    "properties": [
                        "time",
                        "totaltime",
                        "speed",
                        "percentage",
                        "audiostreams",
                        "currentaudiostream",
                        "subtitles",
                        "currentsubtitle",
                        "subtitleenabled",
                    ],
                },
            ),
            rpc(
                "Player.GetItem",
                {
                    "playerid": pid,
                    "properties": [
                        "title",
                        "showtitle",
                        "season",
                        "episode",
                        "thumbnail",
                        "fanart",
                        "art",
                        "file",
                    ],
                },
            ),
        )
        media = item.get("item", {})
        images = _artwork_sources(media)
        return {
            "active": True,
            "player_id": pid,
            "type": active.get("type"),
            "title": media.get("title") or media.get("label") or "",
            "show_title": media.get("showtitle") or "",
            "season": media.get("season"),
            "episode": media.get("episode"),
            "artwork": _remember_artwork(player["id"], images) if images else None,
            "position": seconds(props.get("time")),
            "duration": seconds(props.get("totaltime")),
            "percentage": props.get("percentage", 0),
            "paused": props.get("speed", 0) == 0,
            "speed": props.get("speed", 0),
            "audio_streams": props.get("audiostreams") or [],
            "current_audio": props.get("currentaudiostream"),
            "subtitles": props.get("subtitles") or [],
            "current_subtitle": props.get("currentsubtitle"),
            "subtitles_enabled": bool(props.get("subtitleenabled")),
        }


def lookup(request, player_id):
    player = request.app.state.store.player(player_id)
    if not player:
        raise HTTPException(404, "Unknown Kodi player.")
    return player


@router.get("/{player_id}/remote")
async def remote_status(player_id: str, request: Request, _=Depends(authenticated)):
    player = lookup(request, player_id)
    try:
        return await current_state(player)
    except (ConnectorError, httpx.HTTPError) as exc:
        raise HTTPException(502, str(exc)) from exc


def wake_on_lan(mac: str) -> None:
    """Broadcast a magic packet to the LAN without accepting an arbitrary target IP."""
    clean = mac.replace(":", "").replace("-", "").strip()
    if len(clean) != 12 or any(c not in "0123456789abcdefABCDEF" for c in clean):
        raise HTTPException(422, "Configure a valid MAC address for this Kodi instance.")
    payload = bytes([255]) * 6 + bytes.fromhex(clean) * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(2)
        sock.sendto(payload, ("255.255.255.255", 9))


@router.post("/{player_id}/wake")
async def wake_player(player_id: str, request: Request, _=Depends(writable)):
    player = lookup(request, player_id)
    mac = player["config"].get("mac") or ""
    try:
        await asyncio.to_thread(wake_on_lan, mac)
    except OSError as exc:
        raise HTTPException(502, "Could not broadcast Wake-on-LAN on this network.") from exc
    return {"ok": True, "message": "Wake-on-LAN packet sent."}


@router.get("/{player_id}/volume")
async def kodi_volume(player_id: str, request: Request, _=Depends(authenticated)):
    player = lookup(request, player_id)
    try:
        async with connection(player["config"], timeout=5) as rpc:
            return await rpc("Application.GetProperties", {"properties": ["volume", "muted"]})
    except (ConnectorError, httpx.HTTPError) as exc:
        raise HTTPException(502, str(exc)) from exc


@router.post("/{player_id}/remote")
async def remote_command(
    player_id: str, action: RemoteAction, request: Request, _=Depends(writable)
):
    player = lookup(request, player_id)
    directions = {
        "up": "Up",
        "down": "Down",
        "left": "Left",
        "right": "Right",
        "select": "Select",
        "back": "Back",
        "home": "Home",
        "context": "ContextMenu",
        "info": "Info",
        "osd": "ShowOSD",
    }
    try:
        async with connection(player["config"], timeout=5) as rpc:
            if action.action in directions:
                await rpc("Input." + directions[action.action], {})
            elif action.action == "fullscreen":
                prop = await rpc("GUI.GetProperties", {"properties": ["fullscreen"]})
                await rpc("GUI.SetFullscreen", {"fullscreen": not prop.get("fullscreen", False)})
            elif action.action == "set_volume":
                if action.volume is None:
                    raise HTTPException(422, "Volume is required.")
                await rpc("Application.SetVolume", {"volume": action.volume})
            elif action.action == "set_mute":
                if action.muted is None:
                    raise HTTPException(422, "Muted status is required.")
                await rpc("Application.SetMute", {"mute": action.muted})
            else:
                active = await rpc("Player.GetActivePlayers", {})
                current = next((p for p in active if p.get("type") in ("video", "audio")), None)
                if current is None:
                    raise HTTPException(409, "No active Kodi playback.")
                pid = current["playerid"]
                if action.action == "playpause":
                    await rpc("Player.PlayPause", {"playerid": pid})
                elif action.action == "stop":
                    await rpc("Player.Stop", {"playerid": pid})
                elif action.action in ("next", "previous"):
                    await rpc("Player.GoTo", {"playerid": pid, "to": action.action})
                elif action.action == "seek":
                    if action.seconds is not None:
                        # Time-based seek mirrors integration-kodi and avoids
                        # percentage/duration rounding or incorrectly naked
                        # numbers in the JSON-RPC value parameter.
                        seek_value = {"time": kodi_seek_time(action.seconds)}
                    elif action.percentage is not None:
                        seek_value = {"percentage": action.percentage}
                    else:
                        raise HTTPException(422, "A position or percentage is required to seek.")
                    await rpc("Player.Seek", {"playerid": pid, "value": seek_value})
                elif action.action in ("audio_next", "audio_select"):
                    if action.action == "audio_select" and action.index is None:
                        raise HTTPException(422, "An audio stream index is required.")
                    await rpc(
                        "Player.SetAudioStream",
                        {
                            "playerid": pid,
                            "stream": action.index if action.action == "audio_select" else "next",
                        },
                    )
                elif action.action.startswith("subtitle_"):
                    if action.action == "subtitle_select" and action.index is None:
                        raise HTTPException(422, "A subtitle index is required.")
                    value = (
                        action.index
                        if action.action == "subtitle_select"
                        else action.action.removeprefix("subtitle_")
                    )
                    await rpc(
                        "Player.SetSubtitle",
                        {
                            "playerid": pid,
                            "subtitle": value,
                            "enable": action.action
                            in ("subtitle_next", "subtitle_on", "subtitle_select"),
                        },
                    )
        return {"ok": True}
    except ConnectorError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/{player_id}/artwork")
async def remote_artwork(player_id: str, ref: str, request: Request, _=Depends(authenticated)):
    player = lookup(request, player_id)
    sources = _resolve_artwork_candidates(player_id, ref)
    last_failure = "unknown error"
    # HTTPX resolves the percent-escaped user:pass@host exactly as the
    # integration-kodi image URL and emits the corresponding Basic Auth header.
    # All credentials remain in the server, never in the web/iOS URL.
    async with httpx.AsyncClient(timeout=8, trust_env=False, follow_redirects=False) as client:
        for image in sources:
            if len(image) > 2048 or any(ord(c) < 32 for c in image):
                last_failure = "invalid artwork reference"
                continue
            try:
                response = await client.get(_kodi_artwork_url(player["config"], image))
            except (httpx.HTTPError, ValueError) as exc:
                last_failure = f"HTTP transport {type(exc).__name__}"
                continue
            if response.status_code != 200:
                last_failure = f"Kodi returned HTTP {response.status_code}"
                continue
            content = response.content
            if len(content) > 16_000_000:
                last_failure = "Kodi image too large"
                continue
            try:
                mime = Artwork._image_type(content)
            except ArtworkError:
                # Kodi and some reverse proxies send application/octet-stream
                # even for images. Identify by bytes rather than trusting MIME.
                last_failure = "Kodi returned unsupported image bytes"
                continue
            return Response(
                content=content,
                media_type=mime,
                headers={
                    "Cache-Control": "private, max-age=300",
                    "X-Content-Type-Options": "nosniff",
                },
            )
    # Do not log the image URL: filenames and image:// references can carry
    # embedded SMB authentication credentials.
    LOG.warning("Kodi artwork unavailable for player %s: %s", player_id, last_failure)
    raise HTTPException(502, f"Kodi artwork unavailable: {last_failure}.")


def websocket_authorized(socket: WebSocket):
    """Authenticate browser sessions or native bearer tokens without URL credentials."""
    origin = socket.headers.get("origin")
    if origin:
        value = urlsplit(origin)
        if (
            value.scheme not in ("http", "https")
            or value.netloc.lower() != socket.headers.get("host", "").lower()
        ):
            return False
    auth = socket.app.state.auth
    header = socket.headers.get("authorization", "")
    if header:
        parts = header.split(" ", 1)
        return len(parts) == 2 and parts[0].lower() == "bearer" and bool(auth.token_role(parts[1]))
    cookie = socket.cookies.get(COOKIE)
    if cookie:
        record = auth.sessions.get(cookie)
        return bool(record and record[0] > time.monotonic())
    ingress = socket.headers.get("x-kodi-library-ingress-token", "")
    if auth.settings.ingress_token and secure_equal(ingress, auth.settings.ingress_token):
        return True
    # Use exactly the same trusted-LAN policy as authenticated HTTP requests.
    scope = dict(socket.scope)
    scope["type"] = "http"
    return auth.is_local(Request(scope))


@router.websocket("/{player_id}/events")
async def remote_events(socket: WebSocket, player_id: str):
    if not websocket_authorized(socket):
        await socket.close(code=1008)
        return
    player = await asyncio.to_thread(socket.app.state.store.player, player_id)
    if not player:
        await socket.close(code=1008)
        return
    await socket.accept()
    config = player["config"]
    host = config["host"]
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    protocol = "wss" if config.get("tls") else "ws"
    ws_url = f"{protocol}://{host}:{config.get('websocket_port', 9090)}/jsonrpc"
    credentials = (
        aiohttp.BasicAuth(config["username"], config.get("password") or "")
        if config.get("username")
        else None
    )
    # The HTTP JSON-RPC connection and Kodi's TCP WebSocket service are
    # independent. A disabled/blocked port 9090 must NEVER make a working
    # HTTP-controlled Kodi look offline. Continue polling playback via HTTP
    # while retrying the notification subscription.
    upstream = None
    notification_task = None
    next_retry = 0.0
    retry_delay = 2.0
    last_state = None
    last_heartbeat = time.monotonic()
    http_retry_delay = 5.0
    next_http_retry = 0.0
    http_offline = False
    notifications_offline = False
    try:
        async with aiohttp.ClientSession(
            trust_env=False, timeout=aiohttp.ClientTimeout(total=None, sock_connect=5)
        ) as session:
            while True:
                if upstream is None and time.monotonic() >= next_retry:
                    try:
                        upstream = await session.ws_connect(
                            ws_url,
                            auth=credentials,
                            heartbeat=20,
                            timeout=aiohttp.ClientWSTimeout(ws_receive=None, ws_close=3),
                        )
                        notification_task = asyncio.create_task(upstream.receive())
                        retry_delay = 2.0
                        if notifications_offline:
                            LOG.info("Kodi notifications restored for player %s", player_id)
                        else:
                            LOG.info("Kodi notifications connected for player %s", player_id)
                        notifications_offline = False
                    except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as exc:
                        if not notifications_offline:
                            LOG.info(
                                "Kodi notifications unavailable for player %s (%s); "
                                "using HTTP state polling when available",
                                player_id,
                                type(exc).__name__,
                            )
                        notifications_offline = True
                        next_retry = time.monotonic() + retry_delay
                        retry_delay = min(30.0, retry_delay * 1.7)

                # Independent playback snapshot, including initial connection
                # and time changes when Kodi emits no position notifications.
                if time.monotonic() >= next_http_retry:
                    try:
                        state = await asyncio.wait_for(current_state(player), timeout=8)
                        # A websocket transport error is NOT an offline player.
                        state["websocket_connected"] = upstream is not None
                        if http_offline:
                            LOG.info("Kodi HTTP state restored for player %s", player_id)
                        http_offline = False
                        http_retry_delay = 5.0
                        next_http_retry = 0.0
                        if state != last_state:
                            await socket.send_json({"type": "state", "state": state})
                            last_state = state
                    except (ConnectorError, httpx.HTTPError, TimeoutError, OSError) as exc:
                        if not http_offline:
                            LOG.info(
                                "Kodi HTTP state unavailable for player %s: %s; "
                                "backing off until Kodi returns",
                                player_id,
                                type(exc).__name__,
                            )
                        http_offline = True
                        next_http_retry = time.monotonic() + http_retry_delay
                        http_retry_delay = min(60.0, http_retry_delay * 2)
                        offline = {"type": "offline", "message": "Kodi HTTP JSON-RPC unavailable."}
                        if last_state != offline:
                            await socket.send_json(offline)
                            last_state = offline

                if time.monotonic() - last_heartbeat >= 15:
                    await socket.send_json({"type": "heartbeat"})
                    last_heartbeat = time.monotonic()

                wait = 2.0 if last_state and last_state.get("active") else 5.0
                if notification_task is not None:
                    done, _ = await asyncio.wait({notification_task}, timeout=wait)
                    if done:
                        try:
                            message = notification_task.result()
                        except (aiohttp.ClientError, RuntimeError, OSError):
                            message = None
                        if message is None or message.type in (
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            notification_task = None
                            await upstream.close()
                            upstream = None
                            next_retry = time.monotonic() + retry_delay
                            retry_delay = min(30.0, retry_delay * 1.7)
                        else:
                            # Subscribed automatically to Player.OnPlay/OnPause,
                            # OnStop/OnSeek etc. by the Kodi JSON-RPC WS service.
                            notification_task = asyncio.create_task(upstream.receive())
                else:
                    await asyncio.sleep(wait)
    except (WebSocketDisconnect, RuntimeError, OSError):
        return
    finally:
        if notification_task is not None:
            notification_task.cancel()
            await asyncio.gather(notification_task, return_exceptions=True)
        if upstream is not None:
            await upstream.close()
