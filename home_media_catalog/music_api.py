"""REST API for the music library (``/api/v1/music``), shared by web and iOS."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Literal
from urllib.parse import quote

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
from .players import connection, endpoint
from .sync import BusyError

router = APIRouter(prefix="/api/v1", tags=["Music"])


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
    direct = [c for c in candidates if not c.startswith(("music@", "video@"))]
    try:
        data, content_type = await artwork.get(direct)
        return Response(data, media_type=content_type)
    except ArtworkError:
        pass
    # Kodi-generated art ("embedded cover in the FLAC", "folder.jpg on an SMB
    # share") is only reachable through Kodi's own /image/ endpoint.
    players = await asyncio.to_thread(store.players)
    for candidate in candidates:
        original = "image://" + quote(candidate, safe="") + "/"
        key = hashlib.sha256(original.encode()).hexdigest()
        if cached := await asyncio.to_thread(artwork._cached, key):
            return Response(cached[0], media_type=cached[1])
        for summary in players:
            player = await asyncio.to_thread(store.player, summary["id"])
            if player is None:
                continue
            config = player["config"]
            base = endpoint(config).removesuffix("/jsonrpc")
            auth = (
                (config["username"], config.get("password") or "")
                if config.get("username")
                else None
            )
            try:
                async with httpx.AsyncClient(
                    timeout=8, auth=auth, trust_env=False, follow_redirects=False
                ) as client:
                    response = await client.get(f"{base}/image/{quote(original, safe='')}")
                if response.status_code != 200 or len(response.content) > 8 * 1024 * 1024:
                    continue
                content_type = artwork._image_type(response.content)
                await asyncio.to_thread(artwork._save, key, response.content, content_type)
                return Response(response.content, media_type=content_type)
            except (httpx.HTTPError, ArtworkError, ValueError):
                continue
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
