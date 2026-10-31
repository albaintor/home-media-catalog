"""Short-lived streaming of a Kodi VFS file to a native iOS video player.

Kodi credentials and network-share credentials never leave the NAS. Streams
are byte ranges: a 24 GB movie is never downloaded into server memory.
"""

import asyncio
import logging
import mimetypes
import re
import secrets
import time
from collections import OrderedDict
from urllib.parse import quote, unquote, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse

from .auth import authenticated
from .connectors.base import ConnectorError
from .models import MusicPlayInput, PlayInput
from .playback_browser import _credentials, safe_path, stream_location
from .playback_media import (
    kodi_source,
    media_stream,
    network_source,
    parse_video_copy,
    probe,
    subtitle_vtt,
)
from .players import connection, endpoint
from .utils import identifiers

router = APIRouter(prefix="/api/v1/players", tags=["Players"])
LOG = logging.getLogger(__name__)
# Ephemeral tickets: only the iOS client with the authenticated API token may
# request one. AVPlayer doesn't support our API's bearer header, so the GET
# uses a non-reusable-to-other-media, high-entropy capability ticket.
_TICKETS = OrderedDict()
_TICKET_TTL = 4 * 3600
_TICKET_LIMIT = 512
_RANGE = re.compile(r"^bytes=(?:\d+-\d*|-\d+)$")


def _lookup(request, player_id):
    player = request.app.state.store.player(player_id)
    if not player:
        raise HTTPException(404, "Unknown Kodi instance.")
    return player


def _registered_media_location(request: Request, kodi_path: str):
    """Prefer a server-configured network connection matching the Kodi file.

    SMB/NFS/FTP/SFTP access uses the credentials stored on the server, not
    Kodi's VFS. Match by protocol, host (or Kodi hostname alias), port and
    share-root boundary. Choose the most specific configured root. Unknown or
    malformed paths are never interpreted as filesystem paths.
    """
    try:
        url = urlsplit(kodi_path)
        kind = url.scheme.lower()
        if (
            kind not in {"smb", "nfs", "ftp", "sftp"}
            or not url.hostname or url.username or url.password
            or url.query or url.fragment
        ):
            return None
        defaults = {"smb": 445, "nfs": 2049, "ftp": 21, "sftp": 22}
        requested_port = url.port or defaults[kind]
    except (ValueError, TypeError):
        return None
    requested_host = url.hostname.lower().rstrip(".")
    try:
        media_path = safe_path(unquote(url.path).lstrip("/"))
    except HTTPException:
        return None
    if not media_path:
        return None

    matches = []
    for location in request.app.state.store.playback_locations():
        if location.get("kind") != kind:
            continue
        known_hosts = {
            str(location.get("host") or "").lower().rstrip("."),
            str(location.get("hostname") or "").lower().rstrip("."),
        }
        if requested_host not in known_hosts:
            continue
        try:
            if int(location.get("port") or defaults[kind]) != requested_port:
                continue
            from .playback_browser import _root

            _, root = _root(location)
            root = safe_path(unquote(root))
        except (ValueError, TypeError, HTTPException):
            continue
        # A protocol root is valid for FTP/SFTP; SMB always needs a share
        # and NFS always needs a configured export.
        if not root and kind in {"smb", "nfs"}:
            continue
        compare_path = media_path.lower() if kind == "smb" else media_path
        compare_root = root.lower() if kind == "smb" else root
        if compare_root and not compare_path.startswith(compare_root + "/"):
            continue
        relative = media_path[len(root) + 1 :] if root else media_path
        try:
            relative = safe_path(relative)
        except HTTPException:
            continue
        if relative:
            matches.append((len(root), location, relative))
    if not matches:
        return None
    _, location, relative = max(matches, key=lambda match: match[0])
    return location, relative

async def _resolve_video(request, player, data):
    detail = await asyncio.to_thread(request.app.state.store.detail, data.media_id)
    if not detail or detail["kind"] not in ("movie", "episode"):
        raise HTTPException(404, "No playable Kodi movie or episode.")
    editions = [
        item
        for item in detail["editions"]
        if item["source_id"] == player["source_id"] and item["source_type"] == "kodi"
    ]
    if data.edition_id:
        editions = [item for item in editions if item["id"] == data.edition_id]
    if not editions:
        raise HTTPException(422, "The selected file is not in this Kodi source.")
    editions.sort(key=lambda item: not item["metadata"].get("is_default", True))
    edition = editions[0]
    kodi_id = edition["metadata"].get("kodi_id")
    if type(kodi_id) is not int or kodi_id < 0:
        raise HTTPException(422, "Invalid Kodi ID. Synchronize this library.")
    # Fast path: the catalogue already contains the canonical Kodi edition
    # path from its last import/scan. Only server-side Kodi editions linked to
    # this player are eligible; no client-supplied path is ever accepted.
    # Kodi VFS independently enforces its configured shared source directories.
    # Skipping another JSON-RPC lookup avoids an extra Kodi connection before
    # every remote SMB/NFS media open, which is noticeable for large files.
    known_path = edition["metadata"].get("file")
    if (
        isinstance(known_path, str)
        and known_path
        and len(known_path) <= 8192
        and all(ord(c) >= 32 for c in known_path)
    ):
        return known_path, edition

    # Backward compatibility for older imports without a persisted file path.
    key = "movieid" if detail["kind"] == "movie" else "episodeid"
    method = (
        "VideoLibrary.GetMovieDetails" if key == "movieid" else "VideoLibrary.GetEpisodeDetails"
    )
    result_key = "moviedetails" if key == "movieid" else "episodedetails"
    try:
        async with connection(player["config"]) as rpc:
            result = await rpc(method, {key: kodi_id, "properties": ["file", "uniqueid"]})
    except ConnectorError as exc:
        raise HTTPException(502, "Kodi is unavailable or rejected JSON-RPC.") from exc
    item = result.get(result_key, {})
    if not isinstance(item, dict) or not item:
        raise HTTPException(409, "Kodi no longer has this media ID. Synchronize the library.")
    remote_ids = identifiers(item.get("uniqueid") or {})
    if any(
        k in remote_ids and remote_ids[k] != value for k, value in edition["identifiers"].items()
    ):
        raise HTTPException(409, "Kodi library identity differs from the catalogue.")
    # Honor the explicit movie-version file; otherwise resolve live through Kodi.
    path = (
        edition["metadata"].get("file")
        if data.edition_id or (key == "movieid" and not edition["metadata"].get("is_default", True))
        else item.get("file") or edition["metadata"].get("file")
    )
    if not isinstance(path, str) or not path or len(path) > 8192 or any(ord(c) < 32 for c in path):
        raise HTTPException(422, "Kodi did not return a playable file path.")
    return path, edition


@router.get("/{player_id}/local-size")
async def kodi_local_file_size(
    player_id: str,
    media_id: str,
    edition_id: str,
    request: Request,
    _=Depends(authenticated),
):
    """Read the *entire* Kodi file byte length without opening a player.

    Unlike a HEAD against a temporary streaming ticket, this endpoint is
    authenticated, edition-scoped, and validates the VFS Content-Range for
    a one-byte GET. Do not confuse a 206's one-byte Content-Length with
    the actual file length.
    """
    player = _lookup(request, player_id)
    path, edition = await _resolve_video(
        request, player, PlayInput(media_id=media_id, edition_id=edition_id)
    )
    base = endpoint(player["config"]).removesuffix("/jsonrpc")
    vfs_url = f"{base}/vfs/{quote(path, safe='')}"
    config = player["config"]
    auth = (config["username"], config.get("password") or "") if config.get("username") else None
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=8, read=12, write=8, pool=8),
            auth=auth,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with client.stream(
                "GET",
                vfs_url,
                headers={"Range": "bytes=0-0", "Accept-Encoding": "identity"},
            ) as upstream:
                if upstream.status_code == 206:
                    content_range = upstream.headers.get("content-range", "")
                    match = re.fullmatch(r"bytes\s+0-0/(\d+)", content_range)
                    byte_count = int(match.group(1)) if match else None
                elif upstream.status_code == 200:
                    # Kodi ignored the range and returned the whole file.
                    # Headers are enough; do not read or buffer the body.
                    length = upstream.headers.get("content-length", "")
                    byte_count = int(length) if length.isdigit() else None
                else:
                    raise HTTPException(502, f"Kodi VFS returned HTTP {upstream.status_code}.")
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Cannot read Kodi VFS file size.") from exc
    if byte_count is None or byte_count <= 0:
        raise HTTPException(404, "Kodi VFS did not expose the total file size.")
    return {"edition_id": edition["id"], "bytes": byte_count}


@router.post("/{player_id}/local-play")
async def create_local_stream(
    player_id: str, data: PlayInput, request: Request, _=Depends(authenticated)
):
    """Validate the precise Kodi edition, return only a relative time-limited URL."""
    player = _lookup(request, player_id)
    started = time.monotonic()
    file_path, edition = await _resolve_video(request, player, data)
    resolved_ms = round((time.monotonic() - started) * 1000, 1)
    matched = _registered_media_location(request, file_path)
    LOG.info(
        "Video ticket resolved player=%s preferred=%s resolve_ms=%.1f",
        player_id, matched[0]["kind"] if matched else "kodi", resolved_ms,
    )
    current = time.monotonic()
    for old, value in list(_TICKETS.items()):
        if value["expires"] <= current:
            _TICKETS.pop(old, None)
    # Include a file extension in the URL so AVFoundation can recognize
    # the container even though the opaque capability token hides the filename.
    suffix = urlsplit(file_path).path.rsplit("/", 1)[-1].rsplit(".", 1)
    extension = suffix[-1].lower() if len(suffix) == 2 else "mp4"
    if extension not in {"mp4", "m4v", "mov", "mkv", "avi", "ts", "m2ts"}:
        extension = "mp4"
    filename = f"video.{extension}"
    token = secrets.token_urlsafe(40)
    _TICKETS[token] = {
        "player_id": player_id,
        "file": file_path,
        "filename": filename,
        "expires": current + _TICKET_TTL,
    }
    while len(_TICKETS) > _TICKET_LIMIT:
        _TICKETS.popitem(last=False)
    return {
        "path": f"/api/v1/players/{player_id}/local-stream/{token}/{filename}",
        "edition_id": edition["id"],
        "resume_seconds": edition.get("resume_seconds") or 0,
        "expires_in": _TICKET_TTL,
        "resolution_ms": resolved_ms,
        "preferred_transport": matched[0]["kind"] if matched else "kodi",
        "preferred_location": matched[0].get("name") if matched else None,
    }


AUDIO_STREAM_EXTENSIONS = {
    "mp3", "m4a", "aac", "flac", "alac", "ogg", "oga", "opus", "wav", "aiff", "aif", "aifc",
    "caf", "wma", "ape", "wv", "dsf", "dff", "mka", "mp2", "ac3", "eac3", "dts", "mpc", "tta",
}


@router.post("/{player_id}/music-stream")
async def create_music_stream(
    player_id: str, data: MusicPlayInput, request: Request, _=Depends(authenticated)
):
    """Short-lived byte-range URL for one Kodi music file (iOS player, web <audio>).

    The song must belong to the Kodi source linked to this player. Network
    credentials stay on the server exactly as for video files.
    """
    from .music import MusicCatalogue

    player = _lookup(request, player_id)
    if not data.track_id:
        raise HTTPException(422, "track_id is required.")
    track = await asyncio.to_thread(MusicCatalogue(request.app.state.store).track, data.track_id)
    if (
        not track
        or track["source_id"] != player["source_id"]
        or not track["playable"]
        or not track.get("file")
    ):
        raise HTTPException(404, "This song has no playable file on this Kodi source.")
    file_path = track["file"]
    matched = _registered_media_location(request, file_path)
    current = time.monotonic()
    for old, value in list(_TICKETS.items()):
        if value["expires"] <= current:
            _TICKETS.pop(old, None)
    suffix = urlsplit(file_path).path.rsplit("/", 1)[-1].rsplit(".", 1)
    extension = suffix[-1].lower() if len(suffix) == 2 else "mp3"
    if extension not in AUDIO_STREAM_EXTENSIONS:
        extension = "mp3"
    filename = f"audio.{extension}"
    token = secrets.token_urlsafe(40)
    _TICKETS[token] = {
        "player_id": player_id,
        "file": file_path,
        "filename": filename,
        "expires": current + _TICKET_TTL,
    }
    while len(_TICKETS) > _TICKET_LIMIT:
        _TICKETS.popitem(last=False)
    return {
        "path": f"/api/v1/players/{player_id}/local-stream/{token}/{filename}",
        "track_id": track["id"],
        "expires_in": _TICKET_TTL,
        "preferred_transport": matched[0]["kind"] if matched else "kodi",
        "file_extension": extension,
    }


@router.api_route("/{player_id}/local-stream/{ticket}/{filename}", methods=["GET", "HEAD"])
async def stream_local_video(player_id: str, ticket: str, filename: str, request: Request):
    """Proxy byte-ranged Kodi /vfs HTTP without holding the entire movie in RAM."""
    item = _TICKETS.get(ticket)
    if (
        not item
        or item["player_id"] != player_id
        or item["filename"] != filename
        or item["expires"] <= time.monotonic()
    ):
        raise HTTPException(403, "Stream ticket is invalid or expired.")
    player = _lookup(request, player_id)
    start = time.monotonic()
    range_header = request.headers.get("range")
    if range_header and not _RANGE.fullmatch(range_header):
        raise HTTPException(416, "Only single byte ranges are supported.")
    matched = await asyncio.to_thread(_registered_media_location, request, item["file"])
    if matched:
        location, relative = matched
        try:
            response = await stream_location(
                location, relative, range_header, request.app.state.store,
                request.app.state.playback_oauth,
                request.app.state.playback_nfs_mounts, method=request.method,
            )
            if request.method == "HEAD":
                return Response(
                    status_code=response.status_code,
                    headers=dict(response.headers), media_type=response.media_type,
                )
            # Opening SMB/FTP/SFTP may be lazy: metadata/stat succeeds while
            # the actual read fails. Read one chunk *before* HTTP headers are
            # sent so those errors can still fall back to Kodi, and replay it
            # into the same response without losing its byte-range offset.
            source_iterator = response.body_iterator
            first_chunk = await anext(source_iterator)
            if not first_chunk:
                raise OSError("Configured network source returned an empty media range.")

            async def with_first_chunk():
                yield first_chunk
                async for chunk in source_iterator:
                    yield chunk

            response.body_iterator = with_first_chunk()
            return response
        except HTTPException as exc:
            if exc.status_code not in (404, 409, 501, 502, 503, 504):
                raise
            LOG.info(
                "Configured %s connection failed (HTTP %d); trying Kodi VFS",
                location["kind"], exc.status_code,
            )
        except Exception as exc:
            # FTP/SSH libraries may throw protocol-specific exceptions rather
            # than OSError during the first file read.
            LOG.info(
                "Configured %s network read failed (%s); trying Kodi VFS",
                location["kind"], type(exc).__name__,
            )
    base = endpoint(player["config"]).removesuffix("/jsonrpc")
    url = f"{base}/vfs/{quote(item['file'], safe='')}"
    config = player["config"]
    auth = (config["username"], config.get("password") or "") if config.get("username") else None
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=8, read=40, write=8, pool=8),
        auth=auth,
        follow_redirects=False,
        trust_env=False,
    )
    headers = {"Accept-Encoding": "identity"}
    if range_header:
        headers["Range"] = range_header
    # Kodi's VFS does not consistently answer HEAD. A tiny range GET is
    # sufficient to advertise size to AVPlayer without reading the media.
    if request.method == "HEAD":
        headers["Range"] = "bytes=0-0"
    try:
        upstream_request = client.build_request("GET", url, headers=headers)
        upstream = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(502, "Cannot reach Kodi's VFS HTTP endpoint.") from exc
    if upstream.status_code not in (200, 206):
        code = upstream.status_code
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(502, f"Kodi VFS returned HTTP {code}.")
    # Never silently present a full-file response as the requested nonzero
    # range. AVPlayer would interpret these bytes at the wrong offset.
    if range_header and upstream.status_code == 200:
        segment = range_header.removeprefix("bytes=")
        requested_start = int(segment.split("-", 1)[0]) if not segment.startswith("-") else 1
        if requested_start > 0:
            await upstream.aclose()
            await client.aclose()
            raise HTTPException(502, "Kodi VFS does not support seeking (HTTP byte ranges).")
    exposed = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower()
        in {"content-length", "content-range", "accept-ranges", "etag", "last-modified"}
    }
    kodi_header_ms = round((time.monotonic() - start) * 1000, 1)
    LOG.info(
        "Kodi VFS opened player=%s upstream_status=%d response_headers_ms=%.1f range=%s",
        player_id,
        upstream.status_code,
        kodi_header_ms,
        "present" if range_header else "absent",
    )
    exposed["Server-Timing"] = f"kodi_vfs;dur={kodi_header_ms}"
    exposed["Accept-Ranges"] = "bytes" if upstream.status_code == 206 else "none"
    exposed["X-Kodi-VFS-Ranges"] = "supported" if upstream.status_code == 206 else "unconfirmed"
    exposed["Cache-Control"] = "private, no-store"
    exposed["Referrer-Policy"] = "no-referrer"
    media_type = mimetypes.guess_type(item["file"])[0] or upstream.headers.get(
        "content-type", "application/octet-stream"
    )
    if request.method == "HEAD":
        # A range GET is only a metadata probe; advertise the total file size.
        total = upstream.headers.get("content-range", "").rsplit("/", 1)
        if len(total) == 2 and total[1].isdigit():
            exposed["Content-Length"] = total[1]
        await upstream.aclose()
        await client.aclose()
        return Response(status_code=200, headers=exposed, media_type=media_type)

    async def chunks():
        first_chunk = True
        sent = 0
        try:
            async for chunk in upstream.aiter_bytes(chunk_size=128 * 1024):
                if first_chunk:
                    first_chunk = False
                    LOG.info(
                        "Kodi VFS first chunk player=%s first_byte_ms=%.1f",
                        player_id,
                        (time.monotonic() - start) * 1000,
                    )
                sent += len(chunk)
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()
            LOG.debug("Kodi VFS stream finished player=%s bytes=%d", player_id, sent)

    return StreamingResponse(
        chunks(),
        status_code=upstream.status_code,
        headers=exposed,
        media_type=media_type,
    )


# Browser player: track list, chapters, WebVTT subtitles and a seekable,
# browser-compatible stream for the same short-lived ticket. Unlike the raw
# byte-range route above (used by AVPlayer), these also require the session.
async def _ticket_source(request: Request, player_id: str, ticket: str):
    item = _TICKETS.get(ticket)
    if not item or item["player_id"] != player_id or item["expires"] <= time.monotonic():
        raise HTTPException(403, "Stream ticket is invalid or expired.")
    player = _lookup(request, player_id)
    matched = await asyncio.to_thread(_registered_media_location, request, item["file"])
    if matched:
        location, relative = matched
        password = await asyncio.to_thread(
            _credentials,
            request.app.state.store, request.app.state.playback_oauth, location["id"],
        )
        try:
            source = await asyncio.to_thread(
                network_source, location, relative, password,
                request.app.state.playback_nfs_mounts,
            )
            return source, await probe(source)
        except (OSError, HTTPException) as exc:
            LOG.info(
                "Configured %s media source unavailable (%s); trying Kodi VFS",
                location["kind"], type(exc).__name__,
            )
    config = player["config"]
    base = endpoint(config).removesuffix("/jsonrpc")
    url = f"{base}/vfs/{quote(item['file'], safe='')}"
    auth = (config["username"], config.get("password") or "") if config.get("username") else None
    source = await asyncio.to_thread(kodi_source, url, auth, f"kodi:{player_id}:{item['file']}")
    return source, await probe(source)


@router.get("/{player_id}/local-media/{ticket}/probe")
async def local_media_probe(
    player_id: str, ticket: str, request: Request, _=Depends(authenticated)
):
    _, info = await _ticket_source(request, player_id, ticket)
    return info


@router.get("/{player_id}/local-media/{ticket}/subtitles/{stream_index}.vtt")
async def local_media_subtitle(
    player_id: str, ticket: str, stream_index: int, request: Request, _=Depends(authenticated)
):
    source, info = await _ticket_source(request, player_id, ticket)
    path = await subtitle_vtt(source, info, stream_index, request.app.state.data_dir)
    return FileResponse(
        path, media_type="text/vtt", headers={"Cache-Control": "private, max-age=3600"}
    )


@router.get("/{player_id}/local-media/{ticket}/stream.mp4")
async def local_media_stream(
    player_id: str,
    ticket: str,
    request: Request,
    start: float = Query(0, ge=0),
    audio: int | None = Query(None, ge=0),
    subtitle: int | None = Query(None, ge=0),
    copy: str = Query("", max_length=40),
    height: int = Query(1080, ge=240, le=2160),
    _=Depends(authenticated),
):
    source, info = await _ticket_source(request, player_id, ticket)
    return await media_stream(
        source,
        info,
        start=start,
        audio=audio,
        subtitle=subtitle,
        video_copy=parse_video_copy(copy),
        max_height=height,
    )
