"""Versioned REST API consumed identically by the web UI and native clients."""

import asyncio
import json
import logging
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import httpx
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware

from . import DATA_MODEL_VERSION, __version__
from .ai import (
    AIConfiguration,
    AISearchInput,
    AISettingsInput,
    interpret_prompt,
    interpret_result_refinement,
)
from .ai_hybrid import hybrid_page
from .ai_single_model import single_model_page
from .ai_usage import (
    AIBudgetExceeded,
    AIPriceUnavailable,
    AIUsageLedger,
    usage_session,
)
from .ai_vectors import SemanticIndex, embed
from .artwork import Artwork, ArtworkError
from .auth import COOKIE, Auth, Principal, authenticated, secure_equal, writable
from .backup import MAX_BACKUP_BYTES, export_backup, import_backup
from .compatibility import mobile_compatibility
from .connectors.base import ConnectorError
from .connectors.kodi import KodiConnector
from .enrichment import enrich_tmdb, probe_tmdb
from .list_thumbnails import ListThumbnails
from .models import (
    CatalogueStatus,
    ConnectionResult,
    EpisodeWatchInput,
    EpisodeWatchResult,
    ErrorResponse,
    Facets,
    JobView,
    Kind,
    KodiConnectionInput,
    ListBulkAddInput,
    ListBulkAddResult,
    ListInput,
    ListItemInput,
    ListOrderInput,
    ListPage,
    ListView,
    MariaDBConnectionInput,
    MariaDBDatabasesResult,
    MediaDetail,
    MediaPage,
    PairingRedeem,
    PairingRequest,
    PersonalInput,
    PlaybackLocationInput,
    PlaybackLocationView,
    PlaybackResumeInput,
    PlayerInput,
    PlayerView,
    PlayInput,
    PlayResult,
    ReplayResponse,
    SeasonSummary,
    SessionInput,
    SessionView,
    SortField,
    SourceInput,
    SourceView,
    WatchStatus,
    WritebackStatus,
)
from .playback_browser import _credentials, browse_location, safe_path, stream_location
from .playback_cloud import CloudOAuth, CloudOAuthError
from .playback_discovery import discover_upnp
from .playback_media import (
    media_stream,
    network_source,
    onedrive_source,
    parse_video_copy,
    probe,
    subtitle_vtt,
)
from .playback_operations import mutate_location, search_location
from .playback_thumbnails import thumbnail_location
from .playback_transcode import browser_transcode
from .players import play
from .players import probe as probe_player
from .settings import Settings
from .store import Store
from .sync import BusyError, SyncManager
from .writeback import propagate_personal

_LOG = logging.getLogger(__name__)

API = "/api/v1"
STATIC = Path(__file__).parent / "static"


def unsecret(model) -> dict:
    value = model.model_dump()
    config = value.get("config", value)
    for secret in ("password", "remote_api_key"):
        if config.get(secret) is not None:
            config[secret] = config[secret].get_secret_value()
    return value


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate()
    store = Store(settings.data_dir / "catalogue.db")
    sync = SyncManager(store, settings)
    auth = Auth(settings, store)
    ai_config = AIConfiguration(settings.data_dir)
    ai_usage = AIUsageLedger(settings.data_dir)
    ai_index = SemanticIndex(settings.data_dir)
    ai_index_lock = asyncio.Lock()
    ai_index_task: asyncio.Task | None = None
    ai_index_error: str | None = None
    ai_index_dirty = asyncio.Event()
    ai_limit = asyncio.Semaphore(3)
    ai_rate_lock = asyncio.Lock()
    ai_attempts: dict[str, deque[float]] = defaultdict(deque)
    personal_edit_lock = asyncio.Lock()
    cloud_auth = CloudOAuth(settings, store)

    @asynccontextmanager
    async def lifespan(app):
        await asyncio.to_thread(store.initialize)
        await asyncio.to_thread(auth.load_devices)
        await sync.bootstrap_empty_catalogue()
        # Reconcile the persisted vector index on every server start. Scheduling
        # is immediate and non-blocking; unchanged documents cost no API calls.
        start_ai_index(immediate=True)
        discovery = None
        if settings.discovery_enabled:
            from .discovery import BonjourPublisher

            discovery = BonjourPublisher(settings.discovery_name, settings.discovery_port)
            await asyncio.to_thread(discovery.start)
        try:
            yield
        finally:
            if discovery:
                await asyncio.to_thread(discovery.stop)
            await sync.close()
            if ai_index_task and not ai_index_task.done():
                ai_index_task.cancel()
                try:
                    await ai_index_task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(
        title="Home Media Catalog API",
        version=__version__,
        lifespan=lifespan,
        description="API v1 for Kodi MariaDB and physical collections. Bearer authentication for native clients; the web interface uses the same endpoints. Watch status unknown is distinct from unwatched.",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        responses={
            code: {"model": ErrorResponse} for code in (401, 403, 404, 409, 413, 422, 429, 502)
        },
    )
    app.state.store, app.state.auth, app.state.sync = store, auth, sync
    app.state.playback_oauth = cloud_auth
    app.state.playback_nfs_mounts = settings.playback_nfs_mounts
    app.state.artwork = Artwork(settings)
    app.state.data_dir = settings.data_dir
    thumbnails = ListThumbnails(store, app.state.artwork, settings.data_dir)
    app.state.list_thumbnails = thumbnails
    enrichment_lock = asyncio.Lock()
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-CSRF-Token"],
        )

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        # SAMEORIGIN preserves embedding under Home Assistant ingress.
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Cache-Control"] = (
            "private, max-age=31536000, immutable"
            if "/lists/" in request.url.path
            and "/thumbnails/" in request.url.path
            and response.status_code == 200
            else "private, no-store"
        )
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        codes = {
            401: "unauthorized",
            403: "forbidden",
            404: "not_found",
            409: "conflict",
            413: "payload_too_large",
            422: "invalid_request",
            429: "rate_limited",
            502: "upstream_error",
        }
        return JSONResponse(
            {
                "error": {
                    "code": codes.get(exc.status_code, "request_error"),
                    "message": str(exc.detail),
                }
            },
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Do not echo input values: a malformed request may include passwords.
        details = [
            {"location": list(e["loc"]), "message": e["msg"], "type": e["type"]}
            for e in exc.errors()
        ]
        return JSONResponse(
            {
                "error": {
                    "code": "invalid_request",
                    "message": "Invalid request parameters.",
                    "details": details,
                }
            },
            status_code=422,
        )

    @app.get("/healthz", include_in_schema=False)
    def health():
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/service-worker.js", include_in_schema=False)
    def service_worker():
        # Serve from the site root so it can recover the application shell
        # after a reload while the server is temporarily offline.
        return FileResponse(STATIC / "service-worker.js", media_type="text/javascript")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/docs", include_in_schema=False, dependencies=[Depends(authenticated)])
    def docs():
        return get_swagger_ui_html(
            openapi_url="api/v1/openapi.json", title="Home Media Catalog API v1"
        )

    @app.get(API + "/openapi.json", include_in_schema=False, dependencies=[Depends(authenticated)])
    def schema():
        return app.openapi()

    @app.get(API + "/discovery", include_in_schema=False)
    def discovery_info():
        # Deliberately public, with no credentials, file paths or catalogue data.
        return {
            "service": "home-kodi-library",
            "api_version": "v1",
            "server_version": __version__,
            "data_model_version": DATA_MODEL_VERSION,
            "pairing": True,
            "mobile_compatibility": mobile_compatibility(),
            # Additive capabilities allow newer mobile clients to avoid calling
            # routes missing on an older, otherwise contract-compatible NAS.
            "capabilities": [
                "ai_search",
                "bulk_list_add",
                "ai_refine_results",
                "kodi_local_stream",
                "playback_locations",
                "playback_discovery",
                "playback_cloud_oauth",
                "kodi_clean",
                "media_details_batch",
            ],
        }

    @app.post(
        API + "/pairing/start",
        dependencies=[Depends(writable)],
        tags=["Authentication"],
    )
    def start_pairing(request: PairingRequest, http_request: Request):
        """Bootstrap an authenticated TLS pin through a physical QR scan."""
        import base64
        import io
        import os
        from urllib.parse import urlencode

        import qrcode

        from .tls import fingerprint

        result = auth.start_pairing(request.role)
        host = http_request.url.hostname or ""
        if not host:
            raise HTTPException(422, "Server hostname unavailable for pairing.")
        # The QR transports the certificate fingerprint out-of-band.
        # The iPhone must not fetch this fingerprint from an untrusted URL.
        port = int(os.getenv("TLS_PUBLIC_PORT", "8443"))
        hostname = "[" + host + "]" if ":" in host and not host.startswith("[") else host
        address = f"https://{hostname}:{port}/"
        payload = "home-media-catalog://pair?" + urlencode({
            "url": address, "code": result["code"],
            "pin": fingerprint(settings.data_dir),
        })
        image = qrcode.make(payload)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return {**result, "https_url": address,
                "fingerprint": fingerprint(settings.data_dir),
                "qr_png": base64.b64encode(buffer.getvalue()).decode("ascii")}

    @app.post(API + "/pairing/redeem", tags=["Authentication"])
    def redeem_pairing(request: PairingRedeem, http_request: Request):
        # The sole unauthenticated mutation; bounded lifetime, one use,
        # per-peer and global throttles, with five failures per issued code.
        peer = http_request.client.host if http_request.client else "unknown"
        return auth.redeem_pairing(request.code, request.device_name, peer)

    @app.get(
        API + "/pairing/devices",
        dependencies=[Depends(writable)],
        tags=["Authentication"],
    )
    def paired_devices():
        return store.device_tokens()

    @app.delete(
        API + "/pairing/devices/{token_hash}",
        status_code=204,
        dependencies=[Depends(writable)],
        tags=["Authentication"],
    )
    def revoke_paired_device(token_hash: str):
        if len(token_hash) != 64 or any(char not in "0123456789abcdef" for char in token_hash):
            raise HTTPException(404, "Unknown paired device.")
        if not auth.revoke_device(token_hash):
            raise HTTPException(404, "Unknown paired device.")

    @app.post(API + "/auth/session", response_model=SessionView, tags=["Authentication"])
    def login(data: SessionInput, request: Request, response: Response):
        address = request.client.host if request.client else "unknown"
        session_id, principal = app.state.auth.login(data.token.get_secret_value(), address)
        response.set_cookie(
            COOKIE,
            session_id,
            max_age=12 * 3600,
            httponly=True,
            secure=settings.secure_cookie or request.url.scheme == "https",
            samesite="lax",
            path="/",
        )
        return {"role": principal.role, "csrf_token": principal.csrf}

    @app.get(API + "/auth/session", response_model=SessionView, tags=["Authentication"])
    def session(principal: Principal = Depends(authenticated)):
        return {
            "role": principal.role,
            "csrf_token": principal.csrf,
            "access_method": principal.via,
        }

    @app.delete(API + "/auth/session", status_code=204, tags=["Authentication"])
    def logout(request: Request, response: Response, principal: Principal = Depends(authenticated)):
        if principal.via == "session" and not secure_equal(
            request.headers.get("X-CSRF-Token", ""), principal.csrf or ""
        ):
            raise HTTPException(403, "Invalid CSRF token.")
        app.state.auth.sessions.pop(request.cookies.get(COOKIE, ""), None)
        response.delete_cookie(COOKIE, path="/")

    @app.get(
        API + "/status",
        response_model=CatalogueStatus,
        tags=["Catalogue"],
        dependencies=[Depends(authenticated)],
    )
    def status():
        return {
            "version": __version__,
            "data_model_version": DATA_MODEL_VERSION,
            "api_version": "v1",
            "mobile_compatibility": mobile_compatibility(),
            **store.status(),
            "tmdb_available": bool(settings.tmdb_api_token.strip()),
        }

    def ai_index_status():
        if ai_config.status()["provider"] != "openai":
            return None
        docs = store.ai_documents()
        model = ai_config.status()["embedding_model"]
        return {
            **ai_index.status(docs, model),
            "running": bool(ai_index_task and not ai_index_task.done()),
            "error": ai_index_error,
        }

    async def run_ai_index():
        nonlocal ai_index_error
        async with ai_index_lock:
            if ai_config.status()["provider"] != "openai":
                return
            credentials = ai_config.credentials()
            if not credentials:
                return
            ai_index_error = None
            try:
                documents = await asyncio.to_thread(store.ai_documents)
                with usage_session(ai_usage, ai_config.status()["monthly_budget_usd"], "indexing"):
                    await ai_index.refresh(
                        documents, ai_config.status()["embedding_model"], credentials[1]
                    )
            except (AIBudgetExceeded, AIPriceUnavailable) as exc:
                ai_index_error = str(exc)
            except (httpx.HTTPError, ValueError, OSError) as exc:
                _LOG.warning("AI indexing failed: %s", type(exc).__name__)
                ai_index_error = "Could not build vector index. Check API key and embedding model."

    async def run_ai_index_debounced(*, immediate: bool = False):
        # Startup reconciliation begins immediately. Later catalogue changes
        # retain the debounce to coalesce imports and Kodi synchronizations.
        while ai_index_dirty.is_set():
            ai_index_dirty.clear()
            if not immediate:
                await asyncio.sleep(3)
            immediate = False
            await run_ai_index()

    def start_ai_index(*, immediate: bool = False):
        nonlocal ai_index_task
        if ai_config.status()["provider"] != "openai" or not ai_config.credentials():
            return
        ai_index_dirty.set()
        if ai_index_task is None or ai_index_task.done():
            ai_index_task = asyncio.create_task(run_ai_index_debounced(immediate=immediate))

    sync.on_catalog_changed = start_ai_index

    @app.get(API + "/ai/status", tags=["AI"], dependencies=[Depends(authenticated)])
    def ai_status():
        return {**ai_config.status(), "index": ai_index_status()}

    @app.get(API + "/ai/settings", tags=["AI"], dependencies=[Depends(writable)])
    def ai_settings():
        return {
            **ai_config.status(),
            "index": ai_index_status(),
            "usage": ai_usage.status(ai_config.status()["monthly_budget_usd"]),
        }

    @app.put(API + "/ai/settings", tags=["AI"], dependencies=[Depends(writable)])
    async def update_ai_settings(value: AISettingsInput):
        previous = ai_config.status()["provider"]
        try:
            status = ai_config.update(value)
            if previous == "openai" and status["provider"] != "openai":
                if ai_index_task and not ai_index_task.done():
                    ai_index_task.cancel()
            elif status["provider"] == "openai" and status["configured"]:
                start_ai_index()
            return {
                **status,
                "index": ai_index_status(),
                "usage": ai_usage.status(status["monthly_budget_usd"]),
            }
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get(API + "/ai/usage", tags=["AI"], dependencies=[Depends(writable)])
    def ai_usage_report():
        return ai_usage.status(ai_config.status()["monthly_budget_usd"])

    def ai_probe_failure(exc: Exception) -> str:
        """Do not echo third-party error bodies or secrets to API clients."""
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            if status == 401:
                return "invalid_key"
            if status == 403:
                return "access_denied"
            if status == 404:
                return "model_unavailable"
            if status == 429:
                return "quota_or_rate_limit"
            if status == 400:
                return "model_or_request_incompatible"
            if status >= 500:
                return "provider_unavailable"
            return "provider_error"
        if isinstance(exc, httpx.TimeoutException):
            return "timeout"
        if isinstance(exc, httpx.RequestError):
            return "network_error"
        return "invalid_response"

    @app.post(API + "/ai/connection/test", tags=["AI"], dependencies=[Depends(writable)])
    async def test_ai_connection():
        """Test the active provider and, for OpenAI only, its vector embeddings."""
        credentials = ai_config.credentials()
        if not credentials:
            raise HTTPException(503, "Configure and save the provider API key first.")
        model, key = credentials
        provider = ai_config.status()["provider"]
        results = {}
        async with ai_limit:
            try:
                with usage_session(
                    ai_usage, ai_config.status()["monthly_budget_usd"], "connection_test"
                ):
                    provider_kwargs = {"provider": provider} if provider != "openai" else {}
                    await interpret_prompt(
                        "Trouve les films après 2015.", model, key,
                        {"genres": [], "formats": []}, **provider_kwargs
                    )
                results["language"] = {"ok": True, "model": model}
            except (AIBudgetExceeded, AIPriceUnavailable):
                raise HTTPException(429, "AI budget limit reached or model pricing unknown.") from None
            except (httpx.HTTPError, ValueError) as exc:
                _LOG.warning("AI language model connection test failed: %s", type(exc).__name__)
                results["language"] = {
                    "ok": False, "model": model, "reason": ai_probe_failure(exc)
                }
            if provider == "openai":
                embedding_model = ai_config.status()["embedding_model"]
                try:
                    with usage_session(
                        ai_usage, ai_config.status()["monthly_budget_usd"], "connection_test"
                    ):
                        vectors = await embed(["Test de recherche sémantique."], embedding_model, key)
                    if not vectors or not vectors[0]:
                        raise ValueError("Empty embedding vector")
                    results["embedding"] = {"ok": True, "model": embedding_model}
                except (AIBudgetExceeded, AIPriceUnavailable):
                    raise HTTPException(
                        429, "AI budget limit reached or model pricing unknown."
                    ) from None
                except (httpx.HTTPError, ValueError) as exc:
                    _LOG.warning("OpenAI embedding connection test failed: %s", type(exc).__name__)
                    results["embedding"] = {
                        "ok": False, "model": embedding_model, "reason": ai_probe_failure(exc)
                    }
        return {"ok": all(item["ok"] for item in results.values()), **results}

    @app.post(API + "/ai/index/refresh", tags=["AI"], dependencies=[Depends(writable)])
    async def refresh_ai_index():
        if ai_config.status()["provider"] != "openai":
            return {"status": "not_required", "index": None}
        if not ai_config.credentials():
            raise HTTPException(503, "AI is not configured.")
        start_ai_index()
        return {"status": "started", "index": await asyncio.to_thread(ai_index_status)}

    @app.post(API + "/ai/search", tags=["AI"], dependencies=[Depends(authenticated)])
    async def search_with_ai(value: AISearchInput, request: Request):
        credentials = ai_config.credentials()
        if not credentials:
            raise HTTPException(503, "AI search is not configured. Use standard search.")
        provider = ai_config.status()["provider"]

        async def retrieve(plan, *, prompt="", exclude_ids=None):
            if provider == "openai":
                return await hybrid_page(
                    store, plan, ai_index, ai_config.status()["embedding_model"],
                    credentials[1], prompt=prompt,
                    language_model=credentials[0] if prompt else "",
                    exclude_ids=exclude_ids,
                )
            return await single_model_page(
                store, plan, prompt=prompt or plan.semantic_query,
                model=credentials[0], key=credentials[1],
                provider=provider, exclude_ids=exclude_ids,
            )

        client_ip = request.client.host if request.client else "unknown"
        async with ai_rate_lock:
            attempts = ai_attempts[client_ip]
            current = time.monotonic()
            while attempts and attempts[0] < current - 60:
                attempts.popleft()
            if len(attempts) >= 10:
                raise HTTPException(429, "AI search limit reached. Try again in one minute.")
            attempts.append(current)
        if ai_limit.locked():
            raise HTTPException(429, "Too many AI searches in progress.")
        async with ai_limit:
            try:
                with usage_session(ai_usage, ai_config.status()["monthly_budget_usd"], "search"):
                    facets = await asyncio.to_thread(store.facets)
                    if value.previous_plan is not None and value.previous_ids:
                        # Source metadata from the server, never trust client-supplied titles.
                        previous_ids = list(dict.fromkeys(value.previous_ids))
                        documents = await asyncio.to_thread(store.ai_documents)
                        by_id = {doc["id"]: doc for doc in documents}
                        candidates = [
                            {
                                "id": media_id,
                                "title": by_id[media_id].get("title", ""),
                                "year": by_id[media_id].get("year"),
                                "watch_status": by_id[media_id].get("watch_status"),
                                "kind": by_id[media_id].get("kind"),
                                "plot": (by_id[media_id].get("plot") or "")[:350],
                            }
                            for media_id in previous_ids
                            if media_id in by_id
                        ]
                        if candidates:
                            refinement = await interpret_result_refinement(
                                value.prompt, credentials[0], credentials[1], candidates,
                                **({"provider": provider} if provider != "openai" else {}),
                            )
                            if refinement["operation"] == "expand":
                                # Reuse the original criteria, not the phrase "add more"
                                # as a new semantic or lexical query.
                                more = await retrieve(
                                    value.previous_plan, exclude_ids=set(previous_ids)
                                )
                                existing = await asyncio.to_thread(
                                    store.media, ids=previous_ids, limit=60
                                )
                                positions = {media_id: i for i, media_id in enumerate(previous_ids)}
                                existing["items"].sort(
                                    key=lambda item: positions.get(item["id"], len(positions))
                                )
                                merged = existing["items"] + more["items"]
                                merged = merged[:60]
                                return {
                                    "items": merged,
                                    "total": len(merged),
                                    "has_more": more["has_more"]
                                    or len(existing["items"] + more["items"]) > 60,
                                    "plan": value.previous_plan.model_dump(),
                                    "mode": "expand",
                                    "episode_matches": more.get("episode_matches", {}),
                                    "relevance_reasons": more.get("relevance_reasons", {}),
                                    "assistant_message": (
                                        f"{len(merged) - len(existing['items'])} "
                                        "additional titles found."
                                    ),
                                    "note": more.get("note"),
                                    "index": await asyncio.to_thread(ai_index_status),
                                }
                            if refinement["operation"] == "filter":
                                retained_ids = refinement["keep_ids"]
                                if retained_ids:
                                    page = await asyncio.to_thread(
                                        store.media, ids=retained_ids, limit=60
                                    )
                                    order = {media_id: i for i, media_id in enumerate(retained_ids)}
                                    page["items"].sort(
                                        key=lambda item: order.get(item["id"], len(order))
                                    )
                                else:
                                    page = {
                                        "items": [],
                                        "total": 0,
                                        "limit": 60,
                                        "offset": 0,
                                        "has_more": False,
                                    }
                                page["total"] = len(page["items"])
                                page["has_more"] = False
                                return {
                                    **page,
                                    "plan": value.previous_plan.model_dump(),
                                    "mode": "refine",
                                    "episode_matches": {},
                                    "relevance_reasons": {},
                                    "assistant_message": refinement["assistant_message"],
                                    "note": None,
                                    "index": await asyncio.to_thread(ai_index_status),
                                }
                    if value.previous_plan is None:
                        plan = await interpret_prompt(
                            value.prompt, *credentials, facets,
                            **({"provider": provider} if provider != "openai" else {}),
                        )
                    else:
                        plan = await interpret_prompt(
                            value.prompt, *credentials, facets, value.previous_plan,
                            **({"provider": provider} if provider != "openai" else {}),
                        )
                    output = await retrieve(plan, prompt=value.prompt)
            except AIBudgetExceeded as exc:
                raise HTTPException(429, str(exc)) from None
            except AIPriceUnavailable as exc:
                raise HTTPException(422, str(exc)) from None
            except (httpx.HTTPError, ValueError) as exc:
                _LOG.warning("AI search failed: %s", type(exc).__name__)
                raise HTTPException(502, "AI provider unavailable or invalid model.") from None
        if provider == "openai":
            if plan.semantic_query and not output["index"]["complete"]:
                start_ai_index()
            output["index"] = await asyncio.to_thread(ai_index_status)
        else:
            output.pop("index", None)
        return output

    @app.get(
        API + "/media",
        response_model=MediaPage,
        tags=["Catalogue"],
        dependencies=[Depends(authenticated)],
    )
    def list_media(
        q: str = Query("", max_length=300),
        deep_search: bool = False,
        person_tmdb_id: str | None = Query(None, pattern=r"^[0-9]+$", max_length=24),
        person_imdb_id: str | None = Query(None, pattern=r"^nm[0-9]+$", max_length=24),
        person_name: str | None = Query(None, max_length=200),
        kind: Kind | None = None,
        watch_status: WatchStatus | None = None,
        genre: str | None = Query(None, max_length=120),
        format: list[str] | None = Query(None),
        collection: str | None = Query(None, max_length=200),
        source_type: Literal["kodi", "clz", "mymovies", "filmotech"] | None = None,
        source_id: str | None = None,
        parent_id: str | None = None,
        season: int | None = Query(None, ge=0, le=10000),
        year: int | None = Query(None, ge=1800, le=2200),
        recent_days: int | None = Query(None, ge=1, le=3650),
        sort: SortField = SortField.DATE_ADDED,
        order: Literal["asc", "desc"] = "desc",
        limit: int = Query(60, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        return store.media(
            q=q,
            deep_search=deep_search,
            person_tmdb_id=person_tmdb_id,
            person_imdb_id=person_imdb_id,
            person_name=person_name,
            kind=kind,
            watch_status=watch_status,
            genre=genre,
            format=format,
            collection=collection,
            source_type=source_type,
            source_id=source_id,
            parent_id=parent_id,
            season=season,
            year=year,
            recent_days=recent_days,
            sort=sort,
            order=order,
            limit=limit,
            offset=offset,
        )

    @app.get(
        API + "/media/{media_id}/seasons",
        response_model=list[SeasonSummary],
        tags=["Catalogue"],
        dependencies=[Depends(authenticated)],
    )
    def media_seasons(media_id: str):
        """Navigable season list; empty for shows without imported episodes."""
        result = store.seasons(media_id)
        if result is None:
            raise HTTPException(404, "Unknown TV show.")
        return result

    @app.get(
        API + "/media/{media_id}/seasons/{season}/episodes",
        response_model=MediaPage,
        tags=["Catalogue"],
        dependencies=[Depends(authenticated)],
    )
    def season_episodes(
        media_id: str,
        season: int,
        limit: int = Query(100, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        if season < 0 or season > 10000:
            raise HTTPException(422, "Invalid season.")
        if store.seasons(media_id) is None:
            raise HTTPException(404, "Unknown TV show.")
        return store.media(
            kind="episode",
            parent_id=media_id,
            season=season,
            sort="episode",
            order="asc",
            limit=limit,
            offset=offset,
        )

    async def finish_episode_watch_writebacks(episode_ids: list[str], watched: bool):
        # Changes and their pending writebacks are saved before responding.
        # Subsequent contradictory edits supersede older queued values.
        async with personal_edit_lock:
            players = [
                await asyncio.to_thread(store.player, player["id"])
                for player in await asyncio.to_thread(store.players)
            ]
            source_cache = {}
            for episode_id in episode_ids:
                try:
                    detail = await asyncio.to_thread(store.detail, episode_id)
                    if not detail or detail.get("personal", {}).get("watched") != watched:
                        continue
                    sources = []
                    for source_id in {e["source_id"] for e in detail["editions"]}:
                        if source_id not in source_cache:
                            source_cache[source_id] = await asyncio.to_thread(
                                store.source, source_id, private=True
                            )
                        if source_cache[source_id]:
                            sources.append(source_cache[source_id])
                    changes = {"watched": watched}
                    results = await propagate_personal(detail, changes, sources, players)
                    if results:
                        await asyncio.to_thread(
                            store.record_writebacks, episode_id, changes, results
                        )
                except Exception:
                    # The per-episode retry record remains durable.
                    _LOG.exception("Bulk watch writeback failed for %s", episode_id)

    @app.patch(
        API + "/media/{media_id}/episodes/watch",
        response_model=EpisodeWatchResult,
        tags=["Personal"],
        dependencies=[Depends(writable)],
    )
    async def edit_episode_watches(
        media_id: str, data: EpisodeWatchInput, background_tasks: BackgroundTasks
    ):
        """Mark a season, all episodes, or those through an inclusive cutoff."""
        if data.up_to_episode is not None and data.season is None:
            raise HTTPException(422, "up_to_episode requires a season.")
        async with personal_edit_lock:
            result = await asyncio.to_thread(
                store.set_episode_watch_status,
                media_id,
                data.watched,
                data.season,
                data.up_to_episode,
            )
            if result is None:
                raise HTTPException(404, "Unknown TV show.")
            # Persist retryable per-episode upstream writes before acknowledging.
            source_cache = {}
            for episode_id in result["ids"]:
                detail = await asyncio.to_thread(store.detail, episode_id)
                sources = []
                for source_id in {e["source_id"] for e in detail["editions"]}:
                    if source_id not in source_cache:
                        source_cache[source_id] = await asyncio.to_thread(
                            store.source, source_id, private=True
                        )
                    if source_cache[source_id]:
                        sources.append(source_cache[source_id])
                await asyncio.to_thread(
                    store.stage_writebacks, episode_id, detail, {"watched": data.watched}, sources
                )
            if result["ids"]:
                background_tasks.add_task(
                    finish_episode_watch_writebacks, result["ids"], data.watched
                )
            return {key: value for key, value in result.items() if key != "ids"}

    @app.get(
        API + "/facets",
        response_model=Facets,
        tags=["Catalogue"],
        dependencies=[Depends(authenticated)],
    )
    def facets():
        return store.facets()

    @app.post(
        API + "/media/details/batch",
        tags=["Catalogue"],
        dependencies=[Depends(authenticated)],
    )
    def media_details_batch(data: dict):
        """Bounded rich-detail endpoint for mobile catalogue synchronization."""
        ids = data.get("ids")
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 100
                or any(not isinstance(value, str) or not value or len(value) > 128
                       for value in ids)):
            raise HTTPException(422, "Supply 1 to 100 valid media identifiers.")
        details = store.details_bulk(ids)
        tmdb_available = bool(settings.tmdb_api_token.strip())
        # Use exactly the same DTO validation/defaults as GET /media/{id}.
        return {
            "items": [
                MediaDetail.model_validate(
                    {**detail, "tmdb_available": tmdb_available}
                ).model_dump(mode="json")
                for detail in details
            ]
        }

    @app.get(
        API + "/media/{media_id}",
        response_model=MediaDetail,
        tags=["Catalogue"],
        dependencies=[Depends(authenticated)],
    )
    def media_detail(media_id: str):
        if value := store.detail(media_id):
            return {
                **value,
                "tmdb_available": bool(settings.tmdb_api_token.strip()),
                "writeback_state": store.writeback_state(media_id),
            }
        raise HTTPException(404, "Unknown media.")

    @app.get(
        API + "/media/{media_id}/seasons/{season}/poster",
        tags=["Catalogue"],
        dependencies=[Depends(authenticated)],
        responses={200: {"content": {"image/jpeg": {}, "image/png": {}, "image/webp": {}}}},
    )
    async def tv_season_poster(media_id: str, season: int):
        candidates = await asyncio.to_thread(store.season_poster, media_id, season)
        if not candidates:
            raise HTTPException(404, "No season-specific poster.")
        try:
            data, content_type = await app.state.artwork.get(candidates)
            return Response(data, media_type=content_type)
        except ArtworkError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get(
        API + "/media/{media_id}/diagnostics",
        tags=["Catalogue"],
        dependencies=[Depends(writable)],
    )
    async def diagnose_media(media_id: str):
        """Inspect Kodi's movie-navigation SQL using MariaDB only."""
        detail = await asyncio.to_thread(store.detail, media_id)
        if not detail:
            raise HTTPException(404, "Unknown media.")
        by_source = {}
        for edition in detail["editions"]:
            sid = edition["source_id"]
            report = by_source.setdefault(
                sid,
                {
                    "source_id": sid,
                    "source_name": edition["source_name"],
                    "source_type": edition["source_type"],
                    "editions": [],
                },
            )
            metadata = edition["metadata"]
            report["editions"].append(
                {
                    "external_key": edition["external_key"],
                    "kodi_id": metadata.get("kodi_id"),
                    "file_id": metadata.get("file_id"),
                    "file": metadata.get("file"),
                    "database_at_import": metadata.get("database"),
                    "exists_in_movie": None,
                    "in_library": None,
                }
            )

        reports = []
        for sid, report in by_source.items():
            if report["source_type"] == "kodi":
                source = await asyncio.to_thread(store.source, sid, private=True)
                if not source:
                    report["error"] = "Source no longer exists."
                else:
                    report["configured_database"] = source["config"].get("database") or None
                    movie_ids = [
                        edition["kodi_id"]
                        for edition in report["editions"]
                        if isinstance(edition["kodi_id"], int)
                    ]
                    if detail["kind"] == "movie" and movie_ids:
                        try:
                            inspected = await asyncio.to_thread(
                                KodiConnector(source["config"]).inspect_movie_links, movie_ids
                            )
                            report["queried_database"] = inspected["database"]
                            report["library_rule"] = inspected["library_rule"]
                            report["library_verified"] = inspected["library_verified"]
                            listed = set(inspected["library_visible_movie_ids"])
                            movie_rows = {int(k): v for k, v in inspected["movies"].items()}
                            for edition in report["editions"]:
                                movie_id = edition["kodi_id"]
                                if isinstance(movie_id, int):
                                    edition["exists_in_movie"] = movie_id in movie_rows
                                    edition["sql_links"] = movie_rows.get(movie_id)
                                    edition["in_library"] = movie_id in listed
                        except ConnectorError as exc:
                            report["error"] = str(exc)
                    else:
                        report["note"] = "Movie navigation applies to films only."
            reports.append(report)
        return {
            "media_id": detail["id"],
            "title": detail["title"],
            "sources": reports,
            "has_clz": any(report["source_type"] == "clz" for report in reports),
        }

    @app.patch(
        API + "/media/{media_id}/personal",
        response_model=MediaDetail,
        tags=["Personal"],
        dependencies=[Depends(writable)],
    )
    async def edit_personal(media_id: str, data: PersonalInput):
        # Serialize edits so two concurrent PATCH requests cannot reach Kodi out
        # of order. The user's local value remains saved if Kodi is offline;
        # the response explicitly reports each upstream outcome.
        async with personal_edit_lock:
            detail = await asyncio.to_thread(store.detail, media_id)
            if detail is None:
                raise HTTPException(404, "Unknown media.")
            # PATCH is the sole outbound-write trigger. Unchanged values must
            # not retry a failed propagation or overwrite a third-party edit.
            changes = {
                key: value
                for key, value in data.model_dump(exclude_unset=True).items()
                if detail.get("personal", {}).get(key) != value
            }
            if changes:
                await asyncio.to_thread(store.update_personal, media_id, changes)
                if "user_rating" in changes:
                    # Personal ratings are embedded; watch-state-only edits are not.
                    pass  # No vector index in single-model mode.
            relevant_source_ids = {edition["source_id"] for edition in detail["editions"]}
            sources = [
                await asyncio.to_thread(store.source, source_id, private=True)
                for source_id in relevant_source_ids
            ]
            await asyncio.to_thread(store.stage_writebacks, media_id, detail, changes, sources)
            public_players = await asyncio.to_thread(store.players)
            players = [
                await asyncio.to_thread(store.player, player["id"])
                for player in public_players
                if any(
                    edition["source_id"] == player["source_id"] and edition["source_type"] == "kodi"
                    for edition in detail["editions"]
                )
            ]
            writeback = await propagate_personal(detail, changes, sources, players)
            if writeback:
                await asyncio.to_thread(store.record_writebacks, media_id, changes, writeback)
            updated = await asyncio.to_thread(store.detail, media_id)
            updated["writeback"] = writeback
            updated["writeback_state"] = await asyncio.to_thread(store.writeback_state, media_id)
            return updated

    @app.patch(
        API + "/media/{media_id}/playback",
        tags=["Personal"],
        dependencies=[Depends(writable)],
    )
    async def save_media_playback(media_id: str, data: PlaybackResumeInput):
        """Persist per-edition resume without mutating imported Kodi records.

        Older offline retries cannot overwrite a newer position already held
        by this server. Kodi JSON-RPC writeback is performed by the iPhone
        after receiving the persisted server acknowledgement.
        """
        try:
            stamp = datetime.fromisoformat(data.updated_at.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError("Missing timezone")
            stamp = stamp.astimezone(UTC)
            if stamp > datetime.now(UTC) + timedelta(minutes=10):
                raise ValueError("Playback timestamp is too far in the future")
            if data.total_seconds > 0 and data.position_seconds > data.total_seconds:
                raise ValueError("Playback position exceeds the video duration")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        async with personal_edit_lock:
            saved = await asyncio.to_thread(
                store.save_playback_resume,
                media_id, data.edition_id,
                data.position_seconds, data.total_seconds,
                stamp.isoformat(timespec="milliseconds"),
            )
            if saved is None:
                raise HTTPException(404, "Unknown media edition.")
            return saved

    @app.get(
        API + "/media/{media_id}/writeback",
        tags=["Personal"],
        dependencies=[Depends(authenticated)],
    )
    def media_writeback_history(media_id: str, limit: int = Query(default=50, ge=1, le=200)):
        if not store.detail(media_id):
            raise HTTPException(404, "Unknown media.")
        return store.writeback_history(work_id=media_id, limit=limit)

    @app.get(
        API + "/writebacks/status",
        response_model=WritebackStatus,
        tags=["Personal"],
        dependencies=[Depends(authenticated)],
    )
    def writeback_status():
        """Current outgoing synchronization indicators for web and iOS."""
        return {
            "pending_count": store.writeback_pending_count(),
            "pending": store.pending_writebacks(),
            "recent": store.writeback_state(limit=30),
        }

    @app.get(
        API + "/writebacks/pending",
        tags=["Personal"],
        dependencies=[Depends(authenticated)],
    )
    def pending_writebacks():
        return store.pending_writebacks()

    async def replay_writeback_item(pending_id: str) -> dict:
        row = await asyncio.to_thread(store.pending_writebacks, pending_id=pending_id)
        if not row:
            raise HTTPException(404, "Pending synchronization not found.")
        pending = row[0]
        media_id = pending["media_id"]
        detail = await asyncio.to_thread(store.detail, media_id)
        if detail is None:
            raise HTTPException(409, "The media no longer exists.")
        if not any(e["source_id"] == pending["source_id"] for e in detail["editions"]):
            raise HTTPException(409, "The original source edition is no longer associated.")
        changes = pending["changes"]
        # Reject stale data: never overwrite a third-party service with values
        # which are no longer the user's explicit local preferences.
        if any(detail.get("personal", {}).get(k) != v for k, v in changes.items()):
            raise HTTPException(
                409, "Pending values do not match the current personal preferences."
            )
        source = await asyncio.to_thread(store.source, pending["source_id"], private=True)
        if not source:
            raise HTTPException(409, "The original source no longer exists.")
        if source["type"] == "mymovies" and not source["config"].get("remote_enabled"):
            raise HTTPException(409, "My Movies writeback is not enabled.")
        players = (
            [
                await asyncio.to_thread(store.player, player["id"])
                for player in await asyncio.to_thread(store.players)
                if player["source_id"] == pending["source_id"]
            ]
            if source["type"] == "kodi"
            else []
        )
        scoped = {
            **detail,
            "editions": [
                edition
                for edition in detail["editions"]
                if edition["source_id"] == pending["source_id"]
            ],
        }
        result = await propagate_personal(scoped, changes, [source], players)
        if not result:
            raise HTTPException(409, "The source no longer supports these fields.")
        await asyncio.to_thread(store.record_writebacks, media_id, changes, result, replay=True)
        return {"pending_id": pending_id, "media_id": media_id, "result": result[0]}

    async def retry_pending_source_writebacks(source_id: str) -> None:
        """Retry only this source's pending records after its SQL settings change."""
        async with personal_edit_lock:
            pending = await asyncio.to_thread(store.pending_writebacks, limit=500)
            for item in pending:
                if item["source_id"] != source_id:
                    continue
                try:
                    await replay_writeback_item(item["id"])
                except HTTPException as exc:
                    _LOG.warning(
                        "Pending Kodi writeback %s cannot be replayed: %s",
                        item["id"], exc.detail,
                    )
                except Exception:
                    _LOG.exception(
                        "Unexpected source writeback retry error for %s", item["id"]
                    )

    @app.post(
        API + "/writebacks/pending/{pending_id}/retry",
        response_model=ReplayResponse,
        tags=["Personal"],
        dependencies=[Depends(writable)],
    )
    async def retry_writeback(pending_id: str):
        async with personal_edit_lock:
            result = await replay_writeback_item(pending_id)
            return {
                "results": [result],
                "pending_count": await asyncio.to_thread(store.writeback_pending_count),
            }

    @app.post(
        API + "/writebacks/retry",
        response_model=ReplayResponse,
        tags=["Personal"],
        dependencies=[Depends(writable)],
    )
    async def retry_all_writebacks():
        """User-initiated only; partial failures stay queued for another replay."""
        async with personal_edit_lock:
            pending = await asyncio.to_thread(store.pending_writebacks)
            results = []
            for item in pending:
                try:
                    result = await replay_writeback_item(item["id"])
                    results.append(result)
                except HTTPException as exc:
                    # A missing/obsolete entry stays cached; do not replay or
                    # delete it silently, and continue with independent items.
                    results.append(
                        {
                            "pending_id": item["id"],
                            "media_id": item["media_id"],
                            "result": {
                                "source_id": item["source_id"],
                                "source_name": item["source_name"],
                                "source_type": item["source_type"],
                                "status": "failed",
                                "message": str(exc.detail),
                            },
                        }
                    )
            return {
                "results": results,
                "pending_count": await asyncio.to_thread(store.writeback_pending_count),
            }

    @app.post(
        API + "/media/{media_id}/enrich",
        response_model=MediaDetail,
        tags=["Enrichment"],
        dependencies=[Depends(writable)],
    )
    async def enrich_media(media_id: str, refresh: bool = False):
        async with enrichment_lock:
            detail = await asyncio.to_thread(store.detail, media_id)
            if not detail:
                raise HTTPException(404, "Unknown media.")
            updated = detail.get("enriched_at")
            if (
                not refresh
                and updated
                and datetime.fromisoformat(updated) > datetime.now(UTC) - timedelta(days=30)
                and (detail["kind"] != "tvshow" or detail.get("season_posters_cached"))
            ):
                return {**detail, "tmdb_available": bool(settings.tmdb_api_token.strip())}
            try:
                payload = await enrich_tmdb(detail, settings.tmdb_api_token)
            except ConnectorError as exc:
                raise HTTPException(502, str(exc)) from exc
            if not await asyncio.to_thread(store.save_enrichment, media_id, payload):
                raise HTTPException(404, "Media was removed during enrichment.")
            pass  # No vector index in single-model mode.
            if refresh:
                photos = [
                    url
                    for member in [*detail["people"], *payload.get("people", [])]
                    for url in member.get("photos") or []
                ]
                await asyncio.to_thread(app.state.artwork.invalidate, photos)
            return media_detail(media_id)

    @app.get(
        API + "/media/{media_id}/people/{index}/artwork",
        tags=["Enrichment"],
        dependencies=[Depends(authenticated)],
    )
    async def person_artwork(media_id: str, index: int):
        detail = await asyncio.to_thread(store.detail, media_id)
        if not detail or not 0 <= index < len(detail["people"]):
            raise HTTPException(404, "Unknown credit.")
        try:
            data, content_type = await app.state.artwork.get(
                detail["people"][index].get("photos") or []
            )
            return Response(data, media_type=content_type)
        except ArtworkError as exc:
            raise HTTPException(404, str(exc)) from exc

    def list_error(operation):
        try:
            return operation()
        except KeyError as exc:
            raise HTTPException(404, str(exc.args[0])) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get(
        API + "/catalogue/maintenance/preview",
        tags=["Maintenance"],
        dependencies=[Depends(writable)],
    )
    def catalogue_maintenance_preview():
        return store.catalogue_maintenance_preview()

    @app.post(
        API + "/catalogue/maintenance/{action}",
        tags=["Maintenance"],
        dependencies=[Depends(writable)],
    )
    async def catalogue_maintenance(action: Literal["clean", "reset", "reset_content"], request: Request):
        if request.headers.get("X-Confirm-Catalogue-Maintenance") != action:
            raise HTTPException(422, "Explicit maintenance confirmation is required.")
        if sync.active:
            raise HTTPException(409, "Wait for source synchronization jobs to finish.")
        async with personal_edit_lock:
            try:
                return await asyncio.to_thread(
                    store.maintain_catalogue,
                    reset=action == "reset",
                    reset_content=action == "reset_content",
                )
            except (RuntimeError, ValueError) as exc:
                raise HTTPException(409, str(exc)) from exc

    @app.get(
        API + "/backup",
        tags=["Backup"],
        dependencies=[Depends(authenticated)],
    )
    def download_backup():
        return export_backup(store)

    @app.post(
        API + "/backup",
        tags=["Backup"],
        dependencies=[Depends(writable)],
    )
    async def restore_backup(request: Request, background_tasks: BackgroundTasks):
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            raise HTTPException(422, "A JSON backup is required.")
        if (
            request.headers.get("content-length", "").isdigit()
            and int(request.headers["content-length"]) > MAX_BACKUP_BYTES
        ):
            raise HTTPException(413, "Backup exceeds 100 MiB.")
        payload = await request.body()
        if len(payload) > MAX_BACKUP_BYTES:
            raise HTTPException(413, "Backup exceeds 100 MiB.")
        try:
            document = json.loads(payload)
            async with personal_edit_lock:
                previous, restored = await asyncio.to_thread(import_backup, store, document)
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

        async def rebuild_thumbnails():
            # Purge cached mosaics even if a restored list reuses the same revision.
            ids = set(previous) | set(restored)
            await asyncio.gather(*(thumbnails.delete(item) for item in ids))
            await asyncio.gather(*(thumbnails.warm(item) for item in restored))

        background_tasks.add_task(rebuild_thumbnails)
        return {
            "status": "restored",
            "lists": len(restored),
            "media": len(document["tables"]["works"]),
        }

    # AVPlayer cannot attach the app's bearer token to every Range request.
    # Issue per-file 4-hour, high-entropy tickets without exposing NAS secrets.
    playback_tickets: dict[str, dict] = {}

    @app.post(
        API + "/playback/tickets/{location_id}",
        dependencies=[Depends(authenticated)],
        tags=["Playback"],
    )
    async def create_playback_ticket(location_id: str, payload: dict):
        import secrets
        import time
        from pathlib import PurePosixPath

        from .playback_browser import safe_path

        location = await asyncio.to_thread(store.playback_location, location_id)
        if not location:
            raise HTTPException(404, "Unknown playback location.")
        if location["kind"] not in {
            "smb",
            "ftp",
            "sftp",
            "nfs",
            "onedrive",
            "google_drive",
            "dropbox",
            "box",
            "pcloud",
        }:
            raise HTTPException(501, "Native streaming is unavailable for this protocol.")
        relative = safe_path(payload.get("path", ""))
        if not relative:
            raise HTTPException(422, "A file path is required.")
        # Cloud paths often contain opaque IDs without an extension. iOS also
        # supplies the visible filename to select a safe media content type.
        filename = (
            payload.get("filename", "")
            if location["kind"] in {"onedrive", "google_drive", "dropbox", "box", "pcloud"}
            else relative
        )
        if not isinstance(filename, str) or len(filename) > 1024:
            raise HTTPException(422, "Invalid media filename.")
        extension = PurePosixPath(filename).suffix.lower().lstrip(".")
        if extension not in {
            "mp4",
            "m4v",
            "mov",
            "mkv",
            "avi",
            "ts",
            "m2ts",
            "webm",
            "mp3",
            "m4a",
            "wav",
        }:
            raise HTTPException(422, "File type is not supported by the media player.")
        now = time.monotonic()
        for key, value in list(playback_tickets.items()):
            if value["expires"] <= now:
                del playback_tickets[key]
        ticket = secrets.token_urlsafe(40)
        playback_tickets[ticket] = {
            "location": location_id,
            "path": relative,
            "extension": extension,
            "expires": now + 4 * 3600,
        }
        if len(playback_tickets) > 128:
            oldest = next(iter(playback_tickets))
            del playback_tickets[oldest]
        return {"path": f"/api/v1/playback/ticket/{ticket}/video.{extension}", "expires_in": 14400}

    @app.api_route(
        API + "/playback/ticket/{ticket}/{filename}",
        methods=["GET", "HEAD"],
        include_in_schema=False,
    )
    async def read_playback_ticket(ticket: str, filename: str, request: Request):
        import time

        data = playback_tickets.get(ticket)
        if (
            not data
            or data["expires"] <= time.monotonic()
            or filename != "video." + data["extension"]
        ):
            raise HTTPException(403, "Playback ticket expired or invalid.")
        location = await asyncio.to_thread(store.playback_location, data["location"])
        if not location:
            raise HTTPException(404, "Playback location no longer exists.")
        response = await stream_location(
            location,
            data["path"],
            request.headers.get("Range"),
            store,
            cloud_auth,
            settings.playback_nfs_mounts,
            method=request.method,
        )
        if request.method == "HEAD":
            # File size is known without starting the streaming iterator.
            return Response(status_code=response.status_code, headers=dict(response.headers))
        return response

    @app.get(
        API + "/playback/browse/{location_id}",
        dependencies=[Depends(authenticated)],
        tags=["Playback"],
    )
    async def playback_browse(location_id: str, path: str = ""):
        location = await asyncio.to_thread(store.playback_location, location_id)
        if not location:
            raise HTTPException(404, "Unknown playback location.")
        return await browse_location(
            location, path, store, cloud_auth, settings.playback_nfs_mounts
        )

    @app.get(
        API + "/playback/search/{location_id}",
        dependencies=[Depends(authenticated)],
        tags=["Playback"],
    )
    async def playback_search(
        location_id: str, q: str, path: str = "", offset: int = 0,
        limit: int = 80,
    ):
        location = await asyncio.to_thread(store.playback_location, location_id)
        if not location:
            raise HTTPException(404, "Unknown playback location.")
        return await search_location(
            location, path, q, offset, limit, store, cloud_auth,
            settings.playback_nfs_mounts,
        )

    @app.post(API + "/playback/files/{location_id}", tags=["Playback"])
    async def playback_file_operation(
        location_id: str, payload: dict,
        principal: Principal = Depends(authenticated),
    ):
        # Local-network requests are implicitly admin for legacy read/config
        # paths. They must NOT authorize filesystem writes, especially in the
        # unauthenticated web UI. Only an explicit admin bearer token qualifies.
        if principal.role != "admin" or principal.via != "bearer":
            raise HTTPException(403, "Explicit administrator bearer token required.")
        location = await asyncio.to_thread(store.playback_location, location_id)
        if not location:
            raise HTTPException(404, "Unknown playback location.")
        if not isinstance(payload, dict):
            raise HTTPException(422, "Invalid file operation payload.")
        return await mutate_location(
            location, payload.get("operation"), payload.get("path"),
            payload.get("destination"), store, cloud_auth,
            settings.playback_nfs_mounts,
        )

    @app.get(
        API + "/playback/browser-video/{location_id}",
        dependencies=[Depends(authenticated)],
        tags=["Playback"],
    )
    async def playback_browser_video(location_id: str, path: str):
        """Progressive FFmpeg fragmented MP4 for browsers that cannot open MKV."""
        location = await asyncio.to_thread(store.playback_location, location_id)
        if not location:
            raise HTTPException(404, "Unknown playback location.")
        return await browser_transcode(
            location, path, store, cloud_auth, settings.playback_nfs_mounts
        )

    async def network_media(location_id: str, path: str):
        location = await asyncio.to_thread(store.playback_location, location_id)
        if not location:
            raise HTTPException(404, "Unknown playback location.")
        if location["kind"] not in {"smb", "nfs", "ftp", "sftp", "onedrive"}:
            raise HTTPException(501, "Track-aware playback is not available for this source.")
        relative = safe_path(path)
        if location["kind"] == "onedrive":
            from urllib.parse import quote

            if not relative or "/" in relative:
                raise HTTPException(422, "Invalid OneDrive item identifier.")
            try:
                credentials = await cloud_auth.credentials(location["id"], "onedrive")
            except CloudOAuthError as exc:
                raise HTTPException(409, str(exc)) from exc
            try:
                async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                    response = await client.get(
                        "https://graph.microsoft.com/v1.0/me/drive/items/"
                        + quote(relative, safe=""),
                        headers={"Authorization": "Bearer " + credentials["access_token"]},
                    )
                    response.raise_for_status()
                    signed = response.json().get("@microsoft.graph.downloadUrl")
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                raise HTTPException(502, "OneDrive file metadata unavailable.") from exc
            if not isinstance(signed, str) or not signed:
                raise HTTPException(404, "OneDrive did not provide a media download URL.")
            source = await asyncio.wait_for(
                asyncio.to_thread(
                    onedrive_source, signed, f"onedrive:{location['id']}:{relative}"
                ),
                timeout=18,
            )
        else:
            secret = await asyncio.to_thread(_credentials, store, cloud_auth, location["id"])
            try:
                source = await asyncio.wait_for(
                    asyncio.to_thread(
                        network_source, location, relative, secret, settings.playback_nfs_mounts
                    ),
                    timeout=18,
                )
            except TimeoutError as exc:
                raise HTTPException(504, "Timed out opening the remote media.") from exc
        return source, await probe(source)

    @app.get(
        API + "/playback/media/{location_id}/probe",
        dependencies=[Depends(authenticated)],
        tags=["Playback"],
    )
    async def playback_media_probe(location_id: str, path: str):
        """Audio/subtitle tracks, chapters and video details for the web player."""
        return (await network_media(location_id, path))[1]

    @app.get(
        API + "/playback/media/{location_id}/subtitles/{stream_index}.vtt",
        dependencies=[Depends(authenticated)],
        tags=["Playback"],
    )
    async def playback_media_subtitle(location_id: str, stream_index: int, path: str):
        source, info = await network_media(location_id, path)
        file = await subtitle_vtt(source, info, stream_index, settings.data_dir)
        return FileResponse(
            file, media_type="text/vtt", headers={"Cache-Control": "private, max-age=3600"}
        )

    @app.get(
        API + "/playback/media/{location_id}/stream.mp4",
        dependencies=[Depends(authenticated)],
        tags=["Playback"],
    )
    async def playback_media_stream(
        location_id: str,
        path: str,
        start: float = Query(0, ge=0),
        audio: int | None = Query(None, ge=0),
        subtitle: int | None = Query(None, ge=0),
        copy: str = Query("", max_length=40),
        height: int = Query(1080, ge=240, le=2160),
    ):
        """Seekable fragmented MP4 with the selected audio track."""
        source, info = await network_media(location_id, path)
        return await media_stream(
            source,
            info,
            start=start,
            audio=audio,
            subtitle=subtitle,
            video_copy=parse_video_copy(copy),
            max_height=height,
        )

    @app.get(
        API + "/playback/thumbnail/{location_id}",
        dependencies=[Depends(authenticated)],
        tags=["Playback"],
    )
    async def playback_thumbnail(location_id: str, path: str, filename: str = ""):
        """On-demand artwork; filename carries the suffix for opaque cloud IDs."""
        location = await asyncio.to_thread(store.playback_location, location_id)
        if not location:
            raise HTTPException(404, "Unknown playback location.")
        binary = await thumbnail_location(
            location,
            path,
            store,
            cloud_auth,
            settings.playback_nfs_mounts,
            settings.data_dir,
            filename=filename,
        )
        return Response(
            content=binary,
            media_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=86400"},
        )

    @app.get(
        API + "/playback/stream/{location_id}",
        dependencies=[Depends(authenticated)],
        tags=["Playback"],
    )
    async def playback_stream(location_id: str, request: Request, path: str = ""):
        location = await asyncio.to_thread(store.playback_location, location_id)
        if not location:
            raise HTTPException(404, "Unknown playback location.")
        return await stream_location(
            location,
            path,
            request.headers.get("Range"),
            store,
            cloud_auth,
            settings.playback_nfs_mounts,
        )

    @app.get(
        API + "/playback/locations",
        response_model=list[PlaybackLocationView],
        dependencies=[Depends(authenticated)],
        tags=["Playback"],
    )
    def playback_locations():
        return store.playback_locations()

    @app.post(
        API + "/playback/locations",
        response_model=PlaybackLocationView,
        status_code=201,
        dependencies=[Depends(writable)],
        tags=["Playback"],
    )
    def create_playback_location(data: PlaybackLocationInput):
        payload = data.model_dump(exclude={"password"})
        encrypted = (
            cloud_auth.encrypt_password(data.password.get_secret_value()) if data.password else None
        )
        return store.save_playback_location(payload, password_cipher=encrypted)

    @app.put(
        API + "/playback/locations/{location_id}",
        response_model=PlaybackLocationView,
        dependencies=[Depends(writable)],
        tags=["Playback"],
    )
    def update_playback_location(location_id: str, data: PlaybackLocationInput):
        try:
            payload = data.model_dump(exclude={"password"})
            encrypted = (
                cloud_auth.encrypt_password(data.password.get_secret_value())
                if data.password
                else None
            )
            return store.save_playback_location(payload, location_id, encrypted)
        except KeyError:
            raise HTTPException(404, "Unknown playback location.") from None

    @app.delete(
        API + "/playback/locations/{location_id}",
        status_code=204,
        dependencies=[Depends(writable)],
        tags=["Playback"],
    )
    def delete_playback_location(location_id: str):
        try:
            store.delete_playback_location(location_id)
        except KeyError:
            raise HTTPException(404, "Unknown playback location.") from None

    @app.get(
        API + "/playback/locations/{location_id}/device-credentials",
        dependencies=[Depends(writable)],
        tags=["Playback"],
    )
    def playback_device_credentials(location_id: str, response: Response):
        """Admin-only provisioning for a paired native device's Keychain."""
        location = store.playback_location(location_id)
        if not location:
            raise HTTPException(404, "Unknown playback location.")
        if location["kind"] not in {"smb", "ftp", "sftp"}:
            raise HTTPException(422, "This protocol has no network password.")
        password = _credentials(store, cloud_auth, location_id)
        response.headers["Cache-Control"] = "no-store"
        return {"id": location_id, "password": password}

    @app.get(
        API + "/playback/discovery",
        dependencies=[Depends(authenticated)],
        tags=["Playback"],
    )
    async def playback_discovery():
        return await discover_upnp()

    @app.get(
        API + "/playback/cloud/apps",
        dependencies=[Depends(writable)],
        tags=["Playback"],
    )
    def playback_oauth_apps():
        return store.playback_oauth_apps()

    @app.put(
        API + "/playback/cloud/apps/{provider}",
        dependencies=[Depends(writable)],
        tags=["Playback"],
    )
    def save_playback_oauth_app(provider: str, payload: dict):
        from urllib.parse import urlsplit

        if provider not in cloud_auth.supported:
            raise HTTPException(422, "Unsupported cloud provider.")
        client_id = str(payload.get("client_id") or "").strip()
        redirect = str(payload.get("redirect_url") or "").strip()
        secret = str(payload.get("client_secret") or "")
        parsed = urlsplit(redirect)
        if (
            not client_id
            or len(client_id) > 1024
            or len(secret) > 4096
            or (provider != "onedrive" or redirect)
            and (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.path != "/api/v1/playback/cloud/callback"
                or parsed.fragment
                or parsed.query
            )
        ):
            raise HTTPException(
                422,
                "A client ID is required; HTTPS callback is optional only for OneDrive device login.",
            )
        encrypted = cloud_auth.encrypt_password(secret) if secret else None
        store.set_playback_oauth_app(provider, client_id, redirect, encrypted)
        return {
            "provider": provider,
            "configured": cloud_auth.configured(provider),
            "device_configured": provider == "onedrive"
            and cloud_auth.device_configuration_error(provider) is None,
        }

    @app.get(
        API + "/playback/cloud/providers",
        dependencies=[Depends(authenticated)],
        tags=["Playback"],
    )
    def playback_cloud_providers():
        # A public (non-secret) readiness view for the web and native clients.
        # Configured providers can exist even before a playback location does.
        return [
            {
                "provider": provider,
                "configured": cloud_auth.configured(provider),
                "reason": cloud_auth.configuration_error(provider),
                "device_supported": provider == "onedrive",
                "device_configured": provider == "onedrive"
                and cloud_auth.device_configuration_error(provider) is None,
                "device_reason": cloud_auth.device_configuration_error(provider)
                if provider == "onedrive"
                else None,
                "device_client_id": cloud_auth.device_client_id(provider)
                if provider == "onedrive"
                else None,
            }
            for provider in sorted(cloud_auth.supported)
        ]

    @app.get(
        API + "/playback/cloud/status",
        dependencies=[Depends(authenticated)],
        tags=["Playback"],
    )
    async def playback_cloud_status():
        locations = [
            {
                "id": loc["id"],
                "provider": loc["kind"],
                "configured": cloud_auth.configured(loc["kind"]),
                "connected": store.playback_cloud_status(loc["id"]),
            }
            for loc in store.playback_locations()
            if loc["kind"] in cloud_auth.supported or loc["kind"] == "icloud"
        ]

        # Only add non-secret display metadata. Refresh independent accounts
        # concurrently; absence of a provider email never hides a location.
        async def with_account_email(item: dict) -> dict:
            if item["connected"] and item["provider"] in cloud_auth.supported:
                item["account_email"] = await cloud_auth.account_email(item["id"], item["provider"])
            return item

        return await asyncio.gather(*(with_account_email(item) for item in locations))

    @app.get(
        API + "/playback/cloud/{location_id}/device-credentials",
        dependencies=[Depends(writable)],
        tags=["Playback"],
    )
    def playback_cloud_device_credentials(location_id: str, response: Response):
        """Share only public-client OneDrive grants usable without a client secret."""
        location = store.playback_location(location_id)
        if not location or location["kind"] != "onedrive":
            raise HTTPException(404, "Unknown OneDrive location.")
        saved = store.playback_cloud_credentials(location_id)
        if not saved or saved["provider"] != "onedrive":
            raise HTTPException(409, "OneDrive account not connected.")
        try:
            token = json.loads(cloud_auth.decrypt_secret(saved["token_cipher"]))
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise HTTPException(409, "OneDrive token is unavailable.") from None
        client_id = token.get("_hkl_client_id")
        if not client_id or not token.get("refresh_token") or not token.get("access_token"):
            raise HTTPException(
                409, "This OneDrive account uses confidential web authorization. "
                "Connect the account on the iPhone or use public device authorization."
            )
        response.headers["Cache-Control"] = "no-store"
        return {
            "id": location_id,
            "client_id": client_id,
            "access_token": token["access_token"],
            "refresh_token": token["refresh_token"],
            "expires_at": float(token.get("expires_at") or 0),
        }

    @app.post(
        API + "/playback/cloud/{location_id}/device/start",
        dependencies=[Depends(writable)],
        tags=["Playback"],
    )
    async def start_playback_cloud_device(location_id: str):
        location = await asyncio.to_thread(store.playback_location, location_id)
        if not location:
            raise HTTPException(404, "Unknown playback location.")
        try:
            return await cloud_auth.start_device(location_id, location["kind"])
        except CloudOAuthError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post(
        API + "/playback/cloud/{location_id}/device/poll",
        dependencies=[Depends(writable)],
        tags=["Playback"],
    )
    async def poll_playback_cloud_device(location_id: str, payload: dict):
        import re

        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{24,128}", request_id
        ):
            raise HTTPException(422, "Invalid device authorization request.")
        # Never return the Microsoft device_code, bearer or refresh token.
        return await cloud_auth.poll_device(location_id, request_id)

    @app.post(
        API + "/playback/cloud/{location_id}/authorize",
        dependencies=[Depends(writable)],
        tags=["Playback"],
    )
    def authorize_playback_cloud(location_id: str, return_to: str = "web"):
        location = store.playback_location(location_id)
        if not location:
            raise HTTPException(404, "Unknown playback location.")
        try:
            return {
                "authorization_url": cloud_auth.start(
                    location_id, location["kind"], return_to=return_to
                )
            }
        except CloudOAuthError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get(API + "/playback/cloud/callback", include_in_schema=False)
    async def playback_cloud_callback(
        state: str = "", code: str = "", error: str = "", hostname: str = ""
    ):
        from fastapi.responses import HTMLResponse, RedirectResponse

        # The iOS flow is still a server-owned OAuth transaction: the third
        # party only redirects to our HTTPS callback, never to the iPhone.
        # The final app callback carries no authorization code or access token.
        return_to = cloud_auth.pending.get(state, {}).get("return_to", "web")
        try:
            result = await cloud_auth.complete(
                state=state, code=code, error=error, hostname=hostname
            )
            if result == "ios":
                return RedirectResponse(
                    "homekodilibrary://oauth/server?result=connected",
                    status_code=302,
                    headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
                )
            return HTMLResponse(
                '<!doctype html><html lang="fr"><meta charset="utf-8">'
                "<title>Compte connecté</title><body><h2>Compte connecté.</h2>"
                "<p>Cette fenêtre peut être fermée.</p>"
                "<script>if(window.opener){window.opener.postMessage("
                '{type:"homekodi-cloud-oauth",success:true},"*");'
                "window.close();}</script></body></html>",
                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
            )
        except CloudOAuthError as exc:
            import html
            import json

            # Keep the error visible: the browser must never silently close a
            # failed login. The parent receives the same safe error message.
            explanation = str(exc) or "Connexion refusée ou expirée."
            if return_to == "ios":
                from urllib.parse import urlencode

                return RedirectResponse(
                    "homekodilibrary://oauth/server?"
                    + urlencode({"result": "error", "message": explanation}),
                    status_code=302,
                    headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
                )
            return HTMLResponse(
                '<!doctype html><html lang="fr"><meta charset="utf-8">'
                "<title>Connexion refusée</title><body>"
                "<h2>Connexion cloud non effectuée</h2>"
                "<p>" + html.escape(explanation) + "</p>"
                "<p>Vous pouvez fermer cette fenêtre et corriger le problème.</p>"
                "<script>if(window.opener){window.opener.postMessage("
                + json.dumps(
                    {"type": "homekodi-cloud-oauth", "success": False, "error": explanation}
                )
                + ',"*");}</script></body></html>',
                status_code=400,
                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
            )

    @app.delete(
        API + "/playback/cloud/{location_id}",
        status_code=204,
        dependencies=[Depends(writable)],
        tags=["Playback"],
    )
    def disconnect_playback_cloud(location_id: str):
        if not store.playback_location(location_id):
            raise HTTPException(404, "Unknown playback location.")
        store.delete_playback_cloud_token(location_id)

    @app.get(
        API + "/lists",
        response_model=list[ListView],
        tags=["Lists"],
        dependencies=[Depends(authenticated)],
    )
    def lists():
        return store.lists()

    @app.post(
        API + "/lists",
        response_model=ListView,
        status_code=201,
        tags=["Lists"],
        dependencies=[Depends(writable)],
    )
    def create_list(data: ListInput, background_tasks: BackgroundTasks):
        result = store.save_list(data.model_dump())
        background_tasks.add_task(thumbnails.warm, result["id"])
        return result

    @app.put(
        API + "/lists/{list_id}",
        response_model=ListView,
        tags=["Lists"],
        dependencies=[Depends(writable)],
    )
    def update_list(list_id: str, data: ListInput, background_tasks: BackgroundTasks):
        result = list_error(lambda: store.save_list(data.model_dump(), list_id))
        background_tasks.add_task(thumbnails.warm, list_id)
        return result

    @app.delete(
        API + "/lists/{list_id}", status_code=204, tags=["Lists"], dependencies=[Depends(writable)]
    )
    def delete_list(list_id: str, background_tasks: BackgroundTasks):
        list_error(lambda: store.delete_list(list_id))
        background_tasks.add_task(thumbnails.delete, list_id)

    @app.get(
        API + "/lists/{list_id}/items",
        response_model=ListPage,
        tags=["Lists"],
        dependencies=[Depends(authenticated)],
    )
    def list_items(
        list_id: str,
        sort: Literal[
            "position", "title", "year", "rating", "user_rating", "date_added"
        ] = "position",
        order: Literal["asc", "desc"] = "asc",
        limit: int = Query(60, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        return list_error(
            lambda: store.list_page(list_id, sort=sort, order=order, limit=limit, offset=offset)
        )

    @app.post(
        API + "/lists/{list_id}/items",
        status_code=204,
        tags=["Lists"],
        dependencies=[Depends(writable)],
    )
    def add_list_item(list_id: str, data: ListItemInput, background_tasks: BackgroundTasks):
        list_error(lambda: store.change_list_item(list_id, data.media_id))
        background_tasks.add_task(thumbnails.warm, list_id)

    @app.post(
        API + "/lists/{list_id}/items/bulk",
        response_model=ListBulkAddResult,
        tags=["Lists"],
        dependencies=[Depends(writable)],
    )
    def add_list_items_bulk(
        list_id: str, data: ListBulkAddInput, background_tasks: BackgroundTasks
    ):
        result = list_error(lambda: store.add_list_items(list_id, data.media_ids))
        if result["added"]:
            background_tasks.add_task(thumbnails.warm, list_id)
        return result

    @app.delete(
        API + "/lists/{list_id}/items/{media_id}",
        status_code=204,
        tags=["Lists"],
        dependencies=[Depends(writable)],
    )
    def remove_list_item(list_id: str, media_id: str, background_tasks: BackgroundTasks):
        list_error(lambda: store.change_list_item(list_id, media_id, remove=True))
        background_tasks.add_task(thumbnails.warm, list_id)

    @app.put(
        API + "/lists/{list_id}/order",
        status_code=204,
        tags=["Lists"],
        dependencies=[Depends(writable)],
    )
    def order_list(list_id: str, data: ListOrderInput, background_tasks: BackgroundTasks):
        list_error(lambda: store.reorder_list(list_id, data.media_ids, data.revision))
        background_tasks.add_task(thumbnails.warm, list_id)

    @app.get(
        API + "/lists/{list_id}/thumbnails/{revision}.jpg",
        tags=["Lists"],
        dependencies=[Depends(authenticated)],
        responses={200: {"content": {"image/jpeg": {}}}},
    )
    async def list_thumbnail(list_id: str, revision: int):
        # Revisioned immutable URL prevents obsolete covers in browsers and
        # iOS NSCache. Old URLs are not recomputed after a list changes.
        source = await asyncio.to_thread(store.list_thumbnail_input, list_id)
        if source is None or source["revision"] != revision:
            raise HTTPException(404, "Unknown list thumbnail revision.")
        data = await thumbnails.ensure(list_id, revision)
        if data is None:
            raise HTTPException(404, "List thumbnail changed while loading.")
        return Response(data, media_type="image/jpeg")

    @app.get(
        API + "/media/{media_id}/artwork/{aspect}",
        tags=["Catalogue"],
        dependencies=[Depends(authenticated)],
        responses={200: {"content": {"image/jpeg": {}, "image/png": {}, "image/webp": {}}}},
    )
    async def media_artwork(media_id: str, aspect: Literal["poster", "fanart"]):
        candidates = await asyncio.to_thread(store.artwork, media_id, aspect)
        try:
            data, content_type = await app.state.artwork.get(candidates)
            return Response(data, media_type=content_type)
        except ArtworkError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post(
        API + "/connections/mariadb/databases",
        response_model=MariaDBDatabasesResult,
        tags=["Connections"],
        dependencies=[Depends(writable)],
    )
    async def discover_mariadb_databases(data: MariaDBConnectionInput):
        """List only visible Kodi video schemas; never persist entered credentials."""
        config = unsecret(data)["config"]
        if data.source_id:
            existing = await asyncio.to_thread(store.source, data.source_id, private=True)
            if not existing or existing["type"] != "kodi":
                raise HTTPException(404, "Unknown Kodi MariaDB source.")
            if config.get("password") is None:
                config["password"] = existing["config"].get("password", "")
        config["password"] = config.get("password") or ""
        try:
            return await asyncio.to_thread(KodiConnector(config).discover_databases)
        except ConnectorError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.post(
        API + "/connections/mariadb/test",
        response_model=ConnectionResult,
        tags=["Connections"],
        dependencies=[Depends(writable)],
    )
    async def test_mariadb(data: MariaDBConnectionInput):
        config = unsecret(data)["config"]
        if data.source_id:
            existing = await asyncio.to_thread(store.source, data.source_id, private=True)
            if not existing or existing["type"] != "kodi":
                raise HTTPException(404, "Unknown Kodi MariaDB source.")
            if config.get("password") is None:
                config["password"] = existing["config"].get("password", "")
        config["password"] = config.get("password") or ""
        try:
            result = await asyncio.to_thread(KodiConnector(config).probe)
        except ConnectorError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {
            "kind": "mariadb",
            "message": f"Connexion MariaDB réussie : {result['database']}, accès en lecture vérifié.",
            **result,
        }

    @app.post(
        API + "/connections/kodi/test",
        response_model=ConnectionResult,
        tags=["Connections"],
        dependencies=[Depends(writable)],
    )
    async def test_kodi(data: KodiConnectionInput):
        config = unsecret(data)
        if data.player_id:
            existing = await asyncio.to_thread(store.player, data.player_id)
            if not existing:
                raise HTTPException(404, "Unknown Kodi player.")
            if config.get("password") is None:
                config["password"] = existing["config"].get("password", "")
        config["password"] = config.get("password") or ""
        try:
            result = await probe_player(config)
        except ConnectorError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {
            "kind": "kodi",
            "message": "Connexion Kodi réussie : API HTTP et contrôle de lecture vérifiés.",
            **result,
        }

    @app.get(
        API + "/sources",
        response_model=list[SourceView],
        tags=["Sources"],
        dependencies=[Depends(authenticated)],
    )
    def sources():
        return store.sources()

    @app.post(
        API + "/connections/tmdb/test",
        response_model=ConnectionResult,
        tags=["Connections"],
        dependencies=[Depends(writable)],
    )
    async def test_tmdb():
        if not settings.tmdb_api_token.strip():
            raise HTTPException(
                422, "Renseignez TMDB_API_TOKEN dans .env puis recréez le conteneur."
            )
        try:
            await probe_tmdb(settings.tmdb_api_token)
        except ConnectorError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {
            "kind": "tmdb",
            "message": "Connexion TMDB réussie : accès à l'API vérifié. Ouvrez une fiche pour récupérer le casting et les bandes-annonces disponibles.",
        }

    @app.post(
        API + "/sources",
        response_model=SourceView,
        status_code=201,
        tags=["Sources"],
        dependencies=[Depends(writable)],
    )
    def create_source(data: SourceInput):
        # The primary source can be Kodi JSON-RPC only. MariaDB is optional.
        if data.type == "kodi" and bool(data.config.host) != bool(data.config.username):
            raise HTTPException(422, "MariaDB fallback requires both host and username.")
        if (
            data.type == "kodi"
            and data.config.mariadb_write_enabled
            and (not data.config.host or not data.config.username)
        ):
            raise HTTPException(
                422, "MariaDB write fallback requires a host and username."
            )
        # An empty database intentionally selects the newest visible
        # MyVideos schema in both read and write mode. The SQL writer
        # verifies the record identity before any UPDATE.
        return store.save_source(unsecret(data))

    @app.put(
        API + "/sources/{source_id}",
        response_model=SourceView,
        tags=["Sources"],
        dependencies=[Depends(writable)],
    )
    def update_source(source_id: str, data: SourceInput, background_tasks: BackgroundTasks):
        if source_id in sync.active:
            raise HTTPException(409, "Wait for this source's current job before editing it.")
        previous = store.source(source_id, private=True)
        if not previous:
            raise HTTPException(404, "Unknown source.")
        if data.type == "kodi" and bool(data.config.host) != bool(data.config.username):
            raise HTTPException(422, "MariaDB fallback requires both host and username.")
        if (
            data.type == "kodi"
            and data.config.mariadb_write_enabled
            and (not data.config.host or not data.config.username)
        ):
            raise HTTPException(
                422, "MariaDB write fallback requires a host and username."
            )
        # An empty database intentionally selects the newest visible
        # MyVideos schema in both read and write mode. The SQL writer
        # verifies the record identity before any UPDATE.
        try:
            saved = store.save_source(unsecret(data), source_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        # A pending edit produced before SQL fallback was enabled cannot be
        # sent again via an idempotent PATCH. Re-evaluate it against the newly
        # configured *server-side* source credentials without any iOS secrets.
        if data.type == "kodi" and data.config.mariadb_write_enabled:
            old_config = previous.get("config") or {}
            new_config = store.source(source_id, private=True)["config"]
            fields = ("mariadb_write_enabled", "host", "port", "username",
                      "password", "database", "tls", "ca_file")
            if any(old_config.get(key) != new_config.get(key) for key in fields):
                background_tasks.add_task(retry_pending_source_writebacks, source_id)
        return saved

    @app.delete(
        API + "/sources/{source_id}",
        status_code=204,
        tags=["Sources"],
        dependencies=[Depends(writable)],
    )
    def delete_source(source_id: str):
        if source_id in sync.active:
            raise HTTPException(409, "Wait for this source's current job before removing it.")
        if not store.source(source_id):
            raise HTTPException(404, "Unknown source.")
        store.delete_source(source_id)
        # Remove deleted catalogue works from the semantic index asynchronously.
        start_ai_index()

    @app.post(
        API + "/sources/sync-stale",
        response_model=list[JobView],
        status_code=202,
        tags=["Sources"],
        dependencies=[Depends(authenticated)],
    )
    async def synchronize_stale_on_open():
        """Refresh MariaDB snapshots on client open if older than 24 hours.

        This endpoint never invokes Kodi's video scan; the server alone
        decides whether any enabled SQL source is stale.
        """
        return await sync.sync_stale_on_open()

    @app.post(
        API + "/sources/{source_id}/sync",
        response_model=JobView,
        status_code=202,
        tags=["Sources"],
        dependencies=[Depends(writable)],
    )
    async def synchronize(source_id: str, mariadb_only: bool = False):
        """Refresh metadata; optionally use MariaDB without touching Kodi."""
        if not await asyncio.to_thread(store.source, source_id):
            raise HTTPException(404, "Unknown source.")
        try:
            return await sync.submit(source_id, mariadb_only=mariadb_only)
        except BusyError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post(
        API + "/sources/scan-all",
        response_model=list[JobView],
        status_code=202,
        tags=["Sources"],
        dependencies=[Depends(writable)],
    )
    async def scan_all_kodi_players():
        """Scan every configured remote Kodi instance, once per SQL source."""
        players = await asyncio.to_thread(store.players)
        sources = await asyncio.to_thread(store.sources)
        available = {s["id"] for s in sources if s["type"] == "kodi" and s["enabled"]}
        grouped: dict[str, list[str]] = {}
        for player in players:
            if player["source_id"] in available:
                grouped.setdefault(player["source_id"], []).append(player["id"])
        if not grouped:
            raise HTTPException(422, "Configure at least one Kodi instance on an enabled source.")
        if any(sid in sync.active for sid in grouped):
            raise HTTPException(409, "A Kodi source is already synchronizing. Retry later.")
        jobs = []
        for source_id, ids in grouped.items():
            private_players = [
                await asyncio.to_thread(store.player, player_id) for player_id in ids
            ]
            try:
                jobs.append(await sync.submit_scans(source_id, private_players))
            except BusyError as exc:
                raise HTTPException(409, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        return jobs

    @app.post(
        API + "/sources/{source_id}/scan",
        response_model=JobView,
        status_code=202,
        tags=["Sources"],
        dependencies=[Depends(writable)],
    )
    async def scan_kodi_source(source_id: str, player_id: str | None = None):
        source = await asyncio.to_thread(store.source, source_id)
        if not source:
            raise HTTPException(404, "Unknown source.")
        if source["type"] != "kodi":
            raise HTTPException(422, "Only Kodi sources can be scanned.")
        if player_id:
            player = await asyncio.to_thread(store.player, player_id)
            if not player or player["source_id"] != source_id:
                raise HTTPException(422, "Kodi player is not associated with the selected source.")
        else:
            all_players = await asyncio.to_thread(store.players)
            chosen = next((p for p in all_players if p["source_id"] == source_id), None)
            if not chosen:
                raise HTTPException(422, "Configure a Kodi JSON-RPC player for this source.")
            player = await asyncio.to_thread(store.player, chosen["id"])
        try:
            return await sync.submit_scan(source_id, player)
        except BusyError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post(
        API + "/sources/{source_id}/clean",
        response_model=JobView,
        status_code=202,
        tags=["Sources"],
        dependencies=[Depends(writable)],
    )
    async def clean_kodi_source(source_id: str, player_id: str | None = None):
        source = await asyncio.to_thread(store.source, source_id)
        if not source:
            raise HTTPException(404, "Unknown source.")
        if source["type"] != "kodi":
            raise HTTPException(422, "Only Kodi sources can be cleaned.")
        if player_id:
            player = await asyncio.to_thread(store.player, player_id)
            if not player or player["source_id"] != source_id:
                raise HTTPException(422, "Kodi player is not associated with the selected source.")
        else:
            candidates = await asyncio.to_thread(store.players)
            chosen = next((p for p in candidates if p["source_id"] == source_id), None)
            if chosen is None:
                raise HTTPException(422, "Configure a Kodi JSON-RPC player for this source.")
            player = await asyncio.to_thread(store.player, chosen["id"])
        try:
            return await sync.submit_clean(source_id, player)
        except BusyError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get(
        API + "/sources/{source_id}/writeback",
        tags=["Sources"],
        dependencies=[Depends(authenticated)],
    )
    def source_writeback_history(source_id: str, limit: int = Query(default=50, ge=1, le=200)):
        if not store.source(source_id):
            raise HTTPException(404, "Unknown source.")
        return store.writeback_history(source_id=source_id, limit=limit)

    @app.post(
        API + "/imports/clz",
        response_model=JobView,
        status_code=202,
        tags=["Imports"],
        dependencies=[Depends(writable)],
    )
    @app.post(
        API + "/imports/mymovies",
        response_model=JobView,
        status_code=202,
        tags=["Imports"],
        dependencies=[Depends(writable)],
    )
    @app.post(
        API + "/imports/filmotech",
        response_model=JobView,
        status_code=202,
        tags=["Imports"],
        dependencies=[Depends(writable)],
    )
    async def import_collection(
        request: Request,
        file: UploadFile = File(...),
        source_id: str = Form(...),
    ):
        collection_type = request.url.path.rsplit("/", 1)[-1]
        source = await asyncio.to_thread(store.source, source_id)
        if not source:
            raise HTTPException(404, "Unknown source.")
        if source["type"] != collection_type:
            raise HTTPException(422, "The export type must match the selected collection source.")
        chunks, length = [], 0
        while chunk := await file.read(65536):
            length += len(chunk)
            if length > settings.max_import_bytes:
                await file.close()
                raise HTTPException(413, "The XML export exceeds the 20 MiB import limit.")
            chunks.append(chunk)
        await file.close()
        if not length:
            raise HTTPException(422, "Upload a non-empty XML file.")
        try:
            return await sync.submit(source_id, b"".join(chunks))
        except BusyError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get(
        API + "/jobs",
        response_model=list[JobView],
        tags=["Imports"],
        dependencies=[Depends(authenticated)],
    )
    def recent_jobs():
        """Expose background and manual jobs to native/web progress indicators."""
        return store.jobs()

    @app.get(
        API + "/jobs/{job_id}",
        response_model=JobView,
        tags=["Imports"],
        dependencies=[Depends(authenticated)],
    )
    def job(job_id: str):
        if value := store.job(job_id):
            return value
        raise HTTPException(404, "Unknown import job.")

    @app.get(
        API + "/players",
        response_model=list[PlayerView],
        tags=["Players"],
        dependencies=[Depends(authenticated)],
    )
    def players():
        return store.players()

    def validate_player(data: PlayerInput):
        source = store.source(data.source_id)
        if not source or source["type"] != "kodi":
            raise HTTPException(
                422, "Associate this Kodi instance with an existing Kodi library source."
            )
        if any(c in data.host for c in ("/", "@", "?", "#")):
            raise HTTPException(422, "Enter a Kodi hostname, not a URL.")

    @app.get(
        API + "/players/{player_id}/device-credentials",
        tags=["Players"],
        dependencies=[Depends(authenticated)],
        include_in_schema=False,
    )
    def kodi_device_credentials(
        player_id: str, response: Response, principal: Principal = Depends(authenticated)
    ):
        """Explicit admin-only credential sync for paired native clients.

        The ordinary GET /players API deliberately excludes passwords. This
        separate endpoint must never be cached, included in backups, logged,
        or exposed to read-only devices. Use HTTPS for remote/LAN transport.
        """
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
        if principal.role != "admin":
            raise HTTPException(403, "Kodi credentials require administrator access.")
        player = store.player(player_id)
        if not player:
            raise HTTPException(404, "Unknown Kodi player.")
        configuration = player["config"]
        return {
            "player_id": player_id,
            "username": str(configuration.get("username") or ""),
            "password": str(configuration.get("password") or ""),
        }

    @app.post(
        API + "/players",
        response_model=PlayerView,
        status_code=201,
        tags=["Players"],
        dependencies=[Depends(writable)],
    )
    def create_player(data: PlayerInput):
        validate_player(data)
        return store.save_player(unsecret(data))

    @app.put(
        API + "/players/{player_id}",
        response_model=PlayerView,
        tags=["Players"],
        dependencies=[Depends(writable)],
    )
    def update_player(player_id: str, data: PlayerInput):
        if not store.player(player_id):
            raise HTTPException(404, "Unknown player.")
        validate_player(data)
        return store.save_player(unsecret(data), player_id)

    @app.delete(
        API + "/players/{player_id}",
        status_code=204,
        tags=["Players"],
        dependencies=[Depends(writable)],
    )
    def delete_player(player_id: str):
        if not store.player(player_id):
            raise HTTPException(404, "Unknown player.")
        store.delete_player(player_id)

    @app.post(
        API + "/players/{player_id}/play",
        response_model=PlayResult,
        tags=["Players"],
        dependencies=[Depends(writable)],
    )
    async def play_media(player_id: str, data: PlayInput):
        player = await asyncio.to_thread(store.player, player_id)
        detail = await asyncio.to_thread(store.detail, data.media_id)
        if not player or not detail:
            raise HTTPException(404, "Unknown player or media.")
        try:
            return await play(player, detail, data.edition_id, data.resume)
        except ConnectorError as exc:
            raise HTTPException(502, str(exc)) from exc

    # Music library: artists, albums (physical and digital editions), tracks.
    from .music_api import router as music_router

    app.include_router(music_router)

    # Remote-control API and authenticated Kodi notification relay.
    from .remote import router as remote_router

    app.include_router(remote_router)

    # Kodi credentials remain on this server; native players receive only
    # an expiring HTTP byte-range streaming ticket.
    from .local_stream import router as local_stream_router

    app.include_router(local_stream_router)

    return app
