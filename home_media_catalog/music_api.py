"""REST API for the music library (``/api/v1/music``), shared by web and iOS."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import time
from typing import Literal
from urllib.parse import quote, unquote

import httpx
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from pydantic import BaseModel, Field

from .artwork import ArtworkError
from .auth import authenticated, writable
from .connectors.base import ConnectorError
from .models import (
    JobView,
    MusicAlbumDetail,
    MusicAlbumPage,
    MusicArtistDetail,
    MusicArtistPage,
    MusicExportPage,
    MusicFacets,
    MusicPlayInput,
    MusicSearchResult,
    MusicStatus,
    MusicTrack,
    MusicTrackPage,
)
from .music import MUSIC_COLLECTION_TYPES, MusicCatalogue
from .music_embedded_artwork import extract_embedded_cover
from .music_public_artwork import exact_musicbrainz_cover
from .players import connection, endpoint
from .sync import BusyError

router = APIRouter(prefix="/api/v1", tags=["Music"])

# Broken Kodi HTTP endpoints must not stall every album tile. The in-memory
# cooldown expires automatically and is scoped to the individual player.
LOG = logging.getLogger(__name__)
_MUSIC_ARTWORK_UNREACHABLE: dict[str, float] = {}
_MUSIC_PUBLIC_ARTWORK_RETRY_AFTER: dict[str, float] = {}
_MUSIC_ARTWORK_SEMAPHORE = asyncio.Semaphore(4)



def _music(request: Request) -> MusicCatalogue:
    return MusicCatalogue(request.app.state.store)


@router.get("/music/status", response_model=MusicStatus, dependencies=[Depends(authenticated)])
def music_status(request: Request):
    """Counts and revision; clients resynchronise their offline mirror when it changes."""
    return _music(request).status()


@router.get("/music/facets", response_model=MusicFacets, dependencies=[Depends(authenticated)])
def music_facets(request: Request):
    return _music(request).facets()


@router.get(
    "/music/search", response_model=MusicSearchResult, dependencies=[Depends(authenticated)]
)
def music_search(
    request: Request,
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(default=20, ge=1, le=100),
):
    return _music(request).search(q, limit)


@router.get("/music/artists", response_model=MusicArtistPage, dependencies=[Depends(authenticated)])
def music_artists(
    request: Request,
    q: str = Query(default="", max_length=200),
    genre: str = "",
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
):
    return _music(request).artists(query=q, genre=genre, offset=offset, limit=limit)


@router.get(
    "/music/artists/{artist_id}",
    response_model=MusicArtistDetail,
    dependencies=[Depends(authenticated)],
)
def music_artist(request: Request, artist_id: str):
    value = _music(request).artist(artist_id)
    if value is None:
        raise HTTPException(404, "Unknown artist.")
    return value


@router.get("/music/albums", response_model=MusicAlbumPage, dependencies=[Depends(authenticated)])
def music_albums(
    request: Request,
    q: str = Query(default="", max_length=200),
    artist_id: str = "",
    genre: str = "",
    format: str = "",
    source_type: str = "",
    playable: bool | None = None,
    physical: bool | None = None,
    year: int | None = None,
    sort: Literal["artist", "title", "year", "date_added", "last_played", "rating"] = "artist",
    order: Literal["asc", "desc"] = "asc",
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
):
    return _music(request).albums(
        query=q,
        artist_id=artist_id,
        genre=genre,
        fmt=format,
        source_type=source_type,
        playable=playable,
        physical=physical,
        year=year,
        sort=sort,
        order=order,
        offset=offset,
        limit=limit,
    )


@router.get(
    "/music/albums/{album_id}",
    response_model=MusicAlbumDetail,
    dependencies=[Depends(authenticated)],
)
def music_album(request: Request, album_id: str):
    value = _music(request).album(album_id)
    if value is None:
        raise HTTPException(404, "Unknown album.")
    return value


@router.get("/music/tracks", response_model=MusicTrackPage, dependencies=[Depends(authenticated)])
def music_tracks(
    request: Request,
    q: str = Query(default="", max_length=200),
    album_id: str = "",
    artist_id: str = "",
    genre: str = "",
    playable: bool | None = None,
    sort: Literal["title", "artist", "album", "date_added", "last_played", "play_count"] = "title",
    order: Literal["asc", "desc"] = "asc",
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
):
    return _music(request).tracks(
        query=q,
        album_id=album_id,
        artist_id=artist_id,
        genre=genre,
        playable=playable,
        sort=sort,
        order=order,
        offset=offset,
        limit=limit,
    )


@router.get(
    "/music/tracks/{track_id}", response_model=MusicTrack, dependencies=[Depends(authenticated)]
)
def music_track(request: Request, track_id: str):
    value = _music(request).track(track_id)
    if value is None:
        raise HTTPException(404, "Unknown track.")
    return value


@router.get(
    "/music/export/{collection}",
    response_model=MusicExportPage,
    dependencies=[Depends(authenticated)],
)
def music_export(
    request: Request,
    collection: Literal["artists", "albums", "tracks"],
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=2000, ge=1, le=5000),
):
    """Keyset-paginated full export for native offline mirrors.

    Pass ``next`` back as ``after`` until it is ``null``. If ``revision``
    changes between pages, restart: a synchronisation was published meanwhile.
    """
    return _music(request).export(collection, after=after, limit=limit)


class MusicPlaylistCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)


class MusicPlaylistTracks(BaseModel):
    track_ids: list[str] = Field(default_factory=list, max_length=5000)


@router.get("/music/playlists", dependencies=[Depends(authenticated)])
def music_playlists(request: Request):
    return _music(request).playlists()


@router.post("/music/playlists", status_code=201, dependencies=[Depends(writable)])
def music_create_playlist(request: Request, data: MusicPlaylistCreate):
    try:
        return _music(request).create_playlist(data.name)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/music/playlists/{playlist_id}", dependencies=[Depends(authenticated)])
def music_playlist_detail(request: Request, playlist_id: str):
    value = _music(request).playlist(playlist_id)
    if value is None:
        raise HTTPException(404, "Unknown music playlist.")
    return value


@router.patch("/music/playlists/{playlist_id}", dependencies=[Depends(writable)])
def music_rename_playlist(request: Request, playlist_id: str, data: MusicPlaylistCreate):
    try:
        renamed = _music(request).rename_playlist(playlist_id, data.name)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not renamed:
        raise HTTPException(404, "Unknown music playlist.")
    return _music(request).playlist(playlist_id)


@router.delete("/music/playlists/{playlist_id}", status_code=204, dependencies=[Depends(writable)])
def music_delete_playlist(request: Request, playlist_id: str):
    if not _music(request).delete_playlist(playlist_id):
        raise HTTPException(404, "Unknown music playlist.")
    return Response(status_code=204)


@router.post("/music/playlists/{playlist_id}/tracks", dependencies=[Depends(writable)])
def music_add_playlist_tracks(request: Request, playlist_id: str, data: MusicPlaylistTracks):
    try:
        value = _music(request).add_playlist_tracks(playlist_id, data.track_ids)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if value is None:
        raise HTTPException(404, "Unknown music playlist.")
    return value


@router.put("/music/playlists/{playlist_id}/tracks", dependencies=[Depends(writable)])
def music_replace_playlist_tracks(request: Request, playlist_id: str, data: MusicPlaylistTracks):
    try:
        value = _music(request).replace_playlist_tracks(playlist_id, data.track_ids)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if value is None:
        raise HTTPException(404, "Unknown music playlist.")
    return value


@router.delete("/music/playlists/{playlist_id}/tracks/{position}",
               status_code=204, dependencies=[Depends(writable)])
def music_remove_playlist_track(request: Request, playlist_id: str,
                                position: int):
    if position < 0 or not _music(request).remove_playlist_item(playlist_id, position):
        raise HTTPException(404, "Unknown music playlist entry.")
    return Response(status_code=204)


def _unwrapped_music_artwork(reference: str) -> str:
    """Decode Kodi's image:// wrapper, as the iOS artwork loader does."""
    if not reference.startswith("image://"):
        return reference
    wrapped = reference[len("image://"):].strip("/")
    wrapped = wrapped.split("|", 1)[0]
    value = unquote(wrapped)
    # An older Kodi scanner may have encoded the nested URL twice.
    if value.startswith(("music%40", "video%40")):
        value = unquote(value)
    return value


def _music_network_cover(request: Request, reference: str) -> bytes | None:
    """Read a catalogue-owned NAS image via the same share as iOS playback.

    A Kodi thumbnail commonly references folder.jpg / cover.jpg on an SMB
    share, rather than art embedded inside FLAC. Limit bytes in memory and
    never create a server-side cached copy.
    """
    from .local_stream import _registered_media_location
    from .playback_browser import _credentials
    from .playback_media import network_source

    matched = _registered_media_location(request, reference)
    if not matched:
        return None
    location, relative = matched
    password = _credentials(
        request.app.state.store, request.app.state.playback_oauth,
        location["id"],
    )
    source = network_source(
        location, relative, password,
        request.app.state.playback_nfs_mounts,
    )
    if source.length <= 0 or source.length > 8 * 1024 * 1024:
        return None
    parts = []
    total = 0
    for part in source.open(0, source.length - 1):
        total += len(part)
        if total > 8 * 1024 * 1024:
            return None
        parts.append(part)
    return b"".join(parts)


def _music_image_response(artwork, data: bytes) -> Response:
    if not data or len(data) > 8 * 1024 * 1024:
        raise ArtworkError("Music image exceeds the allowed size.")
    return Response(
        data,
        media_type=artwork._image_type(data),
        headers={"Cache-Control": "private, max-age=1800"},
    )


@router.get(
    "/music/{entity}/{entity_id}/artwork/{aspect}",
    dependencies=[Depends(authenticated)],
    responses={200: {"content": {"image/jpeg": {}, "image/png": {}, "image/webp": {}}}},
)
async def music_artwork(
    request: Request,
    entity: Literal["albums", "artists"],
    entity_id: str,
    aspect: Literal["thumb", "fanart"],
):
    store = request.app.state.store
    artwork = request.app.state.artwork
    candidates = await asyncio.to_thread(
        _music(request).artwork, entity.removesuffix("s"), entity_id, aspect
    )
    decoded = [_unwrapped_music_artwork(candidate) for candidate in candidates]
    # Public artwork URLs/mapped server mounts are retrieved transiently.
    # The video artwork endpoint keeps its usual persistent cache policy.
    direct = [
        item for item in decoded
        if not item.startswith(("music@", "video@")) and
        item.startswith(("http://", "https://", "smb://", "nfs://", "ftp://", "sftp://"))
    ]
    try:
        data, _ = await artwork.get(direct, persist=False)
        return _music_image_response(artwork, data)
    except ArtworkError:
        pass

    players = await asyncio.to_thread(store.players)
    async with _MUSIC_ARTWORK_SEMAPHORE:
        for candidate in decoded:
            if not candidate.startswith(("smb://", "nfs://", "ftp://", "sftp://")):
                continue
            try:
                data = await asyncio.to_thread(
                    _music_network_cover, request, candidate
                )
                if data:
                    return _music_image_response(artwork, data)
            except (HTTPException, ArtworkError, OSError, ValueError, ImportError) as exc:
                LOG.debug("Direct music cover read failed (%s)", type(exc).__name__)

        # Both raw and image://-wrapped Kodi music@ references can point
        # to artwork embedded in the track itself. Reuse the iOS-style
        # unwrap before probing the configured server SMB/NFS/FTP/SFTP source.
        for candidate in decoded:
            if not candidate.startswith("music@"):
                continue
            try:
                data = await asyncio.to_thread(
                    extract_embedded_cover, request, candidate
                )
                if data:
                    return _music_image_response(artwork, data)
            except Exception as exc:
                LOG.debug("Music artwork embedded reference unavailable (%s)",
                          type(exc).__name__)

        # Albums may have no valid Kodi thumbnail but still contain a FLAC,
        # MP4 or M4A cover. Try up to three tracks from this album.
        if entity == "albums" and aspect == "thumb":
            def saved_album_tracks():
                with store.connect() as conn:
                    return [
                        row[0] for row in conn.execute(
                            "SELECT DISTINCT file FROM music_tracks "
                            "WHERE album_id=? AND playable=1 AND file IS NOT NULL "
                            "AND file<>'' ORDER BY COALESCE(disc,1),"
                            "COALESCE(track,0) LIMIT 3",
                            (entity_id,),
                        )
                    ]

            for path in await asyncio.to_thread(saved_album_tracks):
                try:
                    data = await asyncio.to_thread(
                        extract_embedded_cover, request, "music@" + path
                    )
                    if data:
                        return _music_image_response(artwork, data)
                except Exception as exc:
                    LOG.debug("Music embedded track artwork unavailable (%s)",
                              type(exc).__name__)

            # An exact MusicBrainz release-group cover is preferable to a
            # guessed match based only on artist/title; never write it to disk.
            def album_mbid():
                with store.connect() as conn:
                    row = conn.execute(
                        "SELECT release_group_mbid FROM music_albums WHERE id=?",
                        (entity_id,),
                    ).fetchone()
                return row[0] if row else None

            release_group = await asyncio.to_thread(album_mbid)
            if release_group:
                cache_key = hashlib.sha256(
                    ("musicbrainz:" + str(release_group)).encode()
                ).hexdigest()
                if time.monotonic() >= _MUSIC_PUBLIC_ARTWORK_RETRY_AFTER.get(cache_key, 0):
                    data = await exact_musicbrainz_cover(release_group)
                    if data:
                        return _music_image_response(artwork, data)
                    _MUSIC_PUBLIC_ARTWORK_RETRY_AFTER[cache_key] = time.monotonic() + 3600

        # Kodi's /image route is the last fallback. The original code
        # generated an image:// wrapper around an already wrapped image://
        # reference and then encoded it again, producing an invalid URL.
        # Use exactly one canonical wrapper for each stored reference.
        for candidate, unwrapped in zip(candidates, decoded, strict=True):
            if not unwrapped:
                continue
            original = (
                candidate if candidate.startswith("image://") else
                "image://" + quote(unwrapped, safe="") + "/"
            )
            for summary in players:
                player_id = summary["id"]
                if time.monotonic() < _MUSIC_ARTWORK_UNREACHABLE.get(player_id, 0):
                    continue
                player = await asyncio.to_thread(store.player, player_id)
                if not player:
                    continue
                config = player["config"]
                try:
                    base = endpoint(config).removesuffix("/jsonrpc")
                    auth = (
                        (config["username"], config.get("password") or "")
                        if config.get("username") else None
                    )
                    async with httpx.AsyncClient(
                        timeout=3.5, auth=auth, trust_env=False,
                        follow_redirects=False,
                    ) as client:
                        response = await client.get(
                            f"{base}/image/{quote(original, safe='')}"
                        )
                    if response.status_code != 200:
                        if response.status_code in (401, 403, 502, 503):
                            _MUSIC_ARTWORK_UNREACHABLE[player_id] = (
                                time.monotonic() + 90
                            )
                        continue
                    return _music_image_response(artwork, response.content)
                except (httpx.HTTPError, ArtworkError, ValueError):
                    _MUSIC_ARTWORK_UNREACHABLE[player_id] = (
                        time.monotonic() + 90
                    )
    raise HTTPException(404, "No accessible artwork.")


@router.post(
    "/sources/{source_id}/music/sync",
    response_model=JobView,
    status_code=202,
    tags=["Sources"],
    dependencies=[Depends(writable)],
)
async def music_sync(request: Request, source_id: str, mariadb_only: bool = False):
    """Refresh the music library of a Kodi source (AudioLibrary, else MyMusic SQL)."""
    store, sync = request.app.state.store, request.app.state.sync
    source = await asyncio.to_thread(store.source, source_id)
    if not source:
        raise HTTPException(404, "Unknown source.")
    try:
        return await sync.submit_music(source_id, mariadb_only=mariadb_only)
    except BusyError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post(
    "/imports/music/{collection_type}",
    response_model=JobView,
    status_code=202,
    tags=["Imports"],
    dependencies=[Depends(writable)],
)
async def music_import(
    request: Request, collection_type: str, file: UploadFile = File(...), source_id: str = Form(...)
):
    """Upload a Discogs CSV, a CLZ Music XML or a generic CSV/JSON album list."""
    store, sync = request.app.state.store, request.app.state.sync
    if collection_type not in MUSIC_COLLECTION_TYPES:
        raise HTTPException(404, "Unknown music collection type.")
    source = await asyncio.to_thread(store.source, source_id)
    if not source:
        raise HTTPException(404, "Unknown source.")
    if source["type"] != collection_type:
        raise HTTPException(422, "The export type must match the selected music source.")
    limit = 50 * 1024 * 1024
    chunks, length = [], 0
    while chunk := await file.read(65536):
        length += len(chunk)
        if length > limit:
            await file.close()
            raise HTTPException(413, "The export exceeds the 50 MiB import limit.")
        chunks.append(chunk)
    await file.close()
    if not length:
        raise HTTPException(422, "Upload a non-empty file.")
    try:
        return await sync.submit_music(source_id, b"".join(chunks))
    except BusyError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc




class MusicJSONImport(BaseModel):
    """JSON transport for native clients that cannot post multipart forms."""
    source_id: str = Field(min_length=1)
    content_base64: str = Field(min_length=1)


@router.post(
    "/imports/music/{collection_type}/json",
    response_model=JobView,
    status_code=202,
    tags=["Imports"],
    dependencies=[Depends(writable)],
)
async def music_import_json(request: Request, collection_type: str, data: MusicJSONImport):
    """Import the same physical exports as the multipart endpoint, atomically.

    The base64 envelope avoids URL-escaping XML, CSV dialects and Unicode data.
    The complete snapshot is validated by the existing music importer before
    publication; empty/oversized/invalid uploads cannot replace a collection.
    """
    if collection_type not in MUSIC_COLLECTION_TYPES:
        raise HTTPException(404, "Unknown music collection type.")
    if len(data.content_base64) > (50 * 1024 * 1024 * 4 // 3 + 8):
        raise HTTPException(413, "The export exceeds the 50 MiB import limit.")
    try:
        content = base64.b64decode(data.content_base64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(422, "Invalid base64 collection payload.") from exc
    if not content:
        raise HTTPException(422, "Upload a non-empty file.")
    if len(content) > 50 * 1024 * 1024:
        raise HTTPException(413, "The export exceeds the 50 MiB import limit.")
    store, sync = request.app.state.store, request.app.state.sync
    source = await asyncio.to_thread(store.source, data.source_id)
    if not source:
        raise HTTPException(404, "Unknown source.")
    if source["type"] != collection_type:
        raise HTTPException(422, "The export type must match the selected music source.")
    try:
        return await sync.submit_music(data.source_id, content)
    except BusyError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

@router.post("/players/{player_id}/music/play", tags=["Players"], dependencies=[Depends(writable)])
async def music_play_on_kodi(request: Request, player_id: str, data: MusicPlayInput):
    """Play a song, an album or an artist on a Kodi instance of the same source."""
    store = request.app.state.store
    player = await asyncio.to_thread(store.player, player_id)
    if not player:
        raise HTTPException(404, "Unknown player.")
    if sum(bool(x) for x in (data.track_id, data.album_id, data.artist_id)) != 1:
        raise HTTPException(422, "Give exactly one of track_id, album_id or artist_id.")

    def kodi_ids() -> dict:
        with store.connect() as conn:
            if data.track_id:
                row = conn.execute(
                    "SELECT external_key FROM music_tracks WHERE id=? AND source_id=?",
                    (data.track_id, player["source_id"]),
                ).fetchone()
                return {"songid": int(row[0].split(":")[1])} if row else {}
            if data.album_id:
                row = conn.execute(
                    "SELECT external_key FROM music_album_editions WHERE album_id=? AND "
                    "source_id=? LIMIT 1",
                    (data.album_id, player["source_id"]),
                ).fetchone()
                return {"albumid": int(row[0].split(":")[1])} if row else {}
            row = conn.execute(
                "SELECT external_key FROM music_artist_sources WHERE artist_id=? AND source_id=? "
                "LIMIT 1",
                (data.artist_id, player["source_id"]),
            ).fetchone()
            return {"artistid": int(row[0].split(":")[1])} if row else {}

    item = await asyncio.to_thread(kodi_ids)
    if not item:
        raise HTTPException(422, "This item is not in the Kodi library linked to this player.")
    try:
        async with connection(player["config"]) as rpc:
            await rpc("Player.Open", {"item": item, "options": {"shuffled": data.shuffle}})
    except ConnectorError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"player_id": player_id, "item": item, "status": "started"}
