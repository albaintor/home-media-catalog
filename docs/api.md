# REST API v1

Prefix: `/api/v1`. UTF-8 JSON, multipart CLZ imports, and binary images. Catalog and playback operations remain available independently of the web frontend and Home Assistant.

## Authentication

A native client uses `Authorization: Bearer <API_TOKEN>`. `API_READ_TOKEN` allows read-only access to the catalog, images, redacted sources, playback targets, and jobs, without modification, import, or playback initiation. Tokens are configured at startup and must be random and distinct (at least 24 characters). Multiple users and OAuth are not yet supported.

The browser exchanges the token via `POST /auth/session` for an opaque HttpOnly, SameSite=Lax cookie valid for 12 hours. `GET /auth/session` returns the role and CSRF token; cookie-authenticated writes require `X-CSRF-Token`. `DELETE /auth/session` ends the session. Bearer tokens require neither cookies nor CSRF. Sessions do not survive a restart; Bearer tokens remain valid.

Ingress forwards a separate secret in `X-Kodi-Library-Ingress-Token`. `X-Ingress-Path` alone does not grant access. CORS is disabled by default and is unnecessary for `URLSession`; `CORS_ORIGINS` accepts a JSON list of explicitly allowed web origins.

## Mobile client compatibility

`GET /api/v1/discovery` is public and returns the API version, server
version, data-model revision, and `mobile_compatibility` contract range.
Mobile contract version **1** is currently supported by this server. It is
independent of the application build and the server's model revision.

As of **data model 6**, discovery also returns additive `capabilities`:
`["ai_search", "bulk_list_add"]`. A client must check `bulk_list_add`
before calling `POST /lists/{list_id}/items/bulk`, because older NAS
installations with the same base data contract may not expose that route.
Missing `capabilities` means optional features have not been advertised,
not that the entire catalogue is incompatible. New discovery keys are
optional for older clients. For breaking DTO changes, increment the mobile
contract and keep the discovery handshake backwards-readable.

## Routes

| Method | Route after `/api/v1` | Result |
|---|---|---|
| GET | `/status` | Counts, versions, source status |
| GET | `/media` | Catalog page and total |
| GET | `/media/{id}` | Media details, identifiers, editions, per-source metadata |
| GET | `/media/{id}/artwork/{poster\|fanart}` | Protected and cached image |
| GET | `/facets` | Genres, formats, collections, years, sources |
| POST | `/connections/mariadb/test` | Read-only MariaDB configuration test without saving |
| POST | `/connections/kodi/test` | HTTP/JSON-RPC test, Kodi version, and playback permission without saving |
| POST | `/connections/tmdb/test` | Test the server-configured TMDB key without a request body or client-side secret |
| GET/POST | `/sources` | List/create a source |
| PUT/DELETE | `/sources/{id}` | Update/remove a source and its editions |
| POST | `/sources/{id}/sync` | Kodi synchronization; returns a job with HTTP 202 |
| POST | `/imports/clz` | Multipart XML, `source_id` and `file` fields, HTTP 202 |
| GET | `/jobs/{id}` | `pending`, `running`, `completed`, or `failed` |
| GET | `/jobs` | Last 30 jobs, including automatically started ones; web/iOS activity tracking |
| GET/POST | `/players` | List/create a Kodi playback target |
| PUT/DELETE | `/players/{id}` | Update/remove a target |
| POST | `/players/{id}/play` | Play media, with optional edition and resume |
| POST | `/players/{id}/local-play` | Short-lived ticket to stream a Kodi file (byte ranges via Kodi VFS) |
| GET | `/players/{id}/local-media/{ticket}/probe` | Web player: duration, video details, audio/subtitle tracks and chapters (FFprobe) |
| GET | `/players/{id}/local-media/{ticket}/subtitles/{stream}.vtt` | Text subtitle stream as WebVTT (extracted once, cached) |
| GET | `/players/{id}/local-media/{ticket}/stream.mp4` | Fragmented MP4 from `start` seconds with `audio` stream, optional burned-in bitmap `subtitle`, `copy=h264,hevc` codecs the client can decode and `height` cap |
| GET | `/playback/media/{location_id}/probe`, `/subtitles/{stream}.vtt`, `/stream.mp4` | Same three operations for a file in an SMB/NFS/FTP/SFTP playback location (`path` query parameter) |
| GET | `/music/status`, `/music/facets`, `/music/search` | Music library counts/revision, filter values and combined search |
| GET | `/music/artists`, `/music/albums`, `/music/tracks` (+ `/{id}`) | Paged music browsing; album detail lists physical/digital editions and tracks |
| GET | `/music/export/{artists\|albums\|tracks}` | Keyset pages for native offline mirrors (`after`, `limit` ≤ 5000) |
| GET | `/music/{albums\|artists}/{id}/artwork/{thumb\|fanart}` | Cached cover art |
| POST | `/sources/{id}/music/sync` | Kodi music library refresh (AudioLibrary, MyMusic fallback), HTTP 202 |
| POST | `/imports/music/{discogs\|clz_music\|music_csv}` | Multipart physical music collection, `source_id` and `file`, HTTP 202 |
| POST | `/players/{id}/music-stream` | Short-lived byte-range URL for one Kodi song |
| POST | `/players/{id}/music/play` | Play a song, album or artist on Kodi |
| GET | `/openapi.json` | Typed OpenAPI contract; authentication required |

See [the music library guide](music.md) for sources, matching and synchronisation.

`/healthz` exposes only process health without authentication. `/docs` is protected and exposes Swagger UI; its Swagger assets are served from FastAPI's default CDN.

## Search, Filters, and Pagination

`GET /media` accepts: `q`, `kind` (`movie`, `tvshow`, `episode`), `watch_status` (`watched`, `unwatched`, `in_progress`, `unknown`), `genre`, `format`, `collection`, `source_type` (`kodi`, `clz`), `source_id`, `parent_id`, `year`, and `recent_days` (1–3650).

Filters are combined with **AND**. `q` searches all entered words by prefix, ignoring case and accents; it is neither an arbitrary substring search nor spelling correction. Facets apply to the entire catalog.

Kodi cast names and roles are searchable. The `/media/{id}` detail exposes `cast: [{name, role, order}]`. `formats` includes physical formats and known technical labels (for example `4K`, `1080p`, `3D`, `Dolby Vision`, `HDR10+`, `Dolby Atmos`). Each edition's metadata includes `cast`, `video_streams`, `audio_streams`, and subtitles when Kodi provides them. These streams belong to each individual file, including alternative versions; a work's formats aggregate those of its editions.

The interface's infinite scrolling uses the same paginated routes: the first request uses `offset=0&limit=60`, followed by offsets 60, 120, etc., retaining filters and sorting. A mobile client can use the same pagination and `has_more`.

Sorting: `sort=title|date_added|year|rating|last_played`, `order=asc|desc`. Default: `date_added`, `desc`. Unknown values always sort last; IDs break ties. `limit` defaults to 60, with a maximum of 200; `offset` defaults to 0. Response: `{items, total, limit, offset, has_more}`. A new import between requests may shift paginated results.

Returned artwork links begin with `/api/v1/…` and are relative to the service root. Clients using a proxy prefix must resolve them **under their service base URL**, as the Swift client does, without stripping the Ingress prefix.

## Examples Without the Web Frontend

```bash
export KODI_LIBRARY_URL='https://catalogue.example.net'
# Set KODI_LIBRARY_TOKEN in your environment without exposing it.
curl -H "Authorization: Bearer $KODI_LIBRARY_TOKEN" \
  --get "$KODI_LIBRARY_URL/api/v1/media" \
  --data-urlencode 'q=blade runner' \
  --data-urlencode 'watch_status=unwatched' \
  --data-urlencode 'genre=Science Fiction' \
  --data-urlencode 'sort=date_added' --data-urlencode 'order=desc'
```

Create a CLZ collection, then use its `id` for the import:

```bash
curl -H "Authorization: Bearer $KODI_LIBRARY_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"My Blu-rays","type":"clz"}' \
  "$KODI_LIBRARY_URL/api/v1/sources"
curl -H "Authorization: Bearer $KODI_LIBRARY_TOKEN" \
  -F 'source_id=RETURNED_ID' -F 'file=@export-clz.xml' \
  "$KODI_LIBRARY_URL/api/v1/imports/clz"
```

Poll `/jobs/JOB_ID` before refreshing the catalog. Each import applies an atomic snapshot: editions missing from the export are removed from that source only. On failure, the previous catalog is preserved. Only one active job per source; maximum import size is 20 MiB.

Create a Kodi source with `{"name":"NAS","type":"kodi","config":{"host":"192.168.1.10","port":3306,"username":"home_media_catalog","password":"…","database":"MyVideos131","tls":false}}`. `database` may be empty for automatic discovery. During PUT, a missing or `null` password preserves the previous value; an empty string clears it. Responses replace the password with `has_password`.

Before creating a source, `POST /connections/mariadb/test` accepts `{"config":{"host":"192.168.1.10","port":3306,"username":"home_media_catalog","password":"…","database":"","tls":false}}`. The test checks access to the views and tables used without importing the catalog. The response includes `success`, `kind`, `message`, `database`, and `available_media`.

For a playback target, `POST /connections/kodi/test` accepts `{"host":"192.168.1.20","port":8080,"username":"kodi","password":"…","tls":false}`. It calls `JSONRPC.Ping`, `JSONRPC.Permission`, and `Application.GetProperties` without playing media. The response includes `version` and `playback_allowed`, among other fields. Kodi must be running with HTTP remote control enabled. This test does not guarantee that the instance uses the same MyVideos database; that association still needs to be configured.

These routes require the administrator token and create no source, target, or job. Connection failures return HTTP 502 with a network, authentication, or permission cause, without disclosing passwords. To test an update, add `source_id` or `player_id`: a missing or `null` password reuses the saved one; an empty string tests without a password. The interface automatically runs tests before adding new entries; creation routes remain directly usable to prepare an offline configuration.

Create a Kodi target with `{"name":"Living Room","source_id":"KODI_SOURCE_ID","host":"192.168.1.20","port":8080,"username":"kodi","password":"…","tls":false}`, then:

```bash
curl -H "Authorization: Bearer $KODI_LIBRARY_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"media_id":"MEDIA_ID","resume":true}' \
  "$KODI_LIBRARY_URL/api/v1/players/TARGET_ID/play"
```

`edition_id` selects a specific Kodi file/version. Before `Player.Open`, the server verifies that the target is linked to the correct source and rejects conflicting remote identifiers. Playing a TV show requires selecting an episode via `GET /media?kind=episode&parent_id=SHOW_ID`. The server does not wake Kodi.

## Writing Ratings and Watch Status

`PATCH /media/{id}/personal` accepts partial updates such as `{"user_rating":7.5}`, `{"watched":true}`, or `{"watched":false}`. A `null` value clears only the local preference without modifying source data. Changes are stored in SQLite and propagated to connectors with write capabilities.

For Kodi, the application uses the HTTP/JSON-RPC instances already associated with each MariaDB source. It checks the `UpdateData` permission, media identity, and Kodi identifier before calling `VideoLibrary.SetMovieDetails`, `SetEpisodeDetails`, or `SetTVShowDetails`. Marking a TV show watched/unwatched is propagated to its episodes by Kodi. Different versions of a movie share one `movieid` and therefore result in a single update. Kodi ratings are integers: 7.5 is sent as 8, while the local rating remains 7.5.

The `MediaDetail` response includes `writeback: [{source_id,source_name,source_type,status,message}]`. `status` is `updated`, `unavailable` (no instance or a disabled source), or `failed` (connection, permissions, or mismatched identity). This feedback applies only to writable sources; imported CLZ/MyMovies/Filmotech XML files are never modified. My Movies sources with a licensed Remote Data API server can receive watched/unwatched changes only (`ElementWatched` and `ClearElementWatched`), never ratings; CLZ and Filmotech sources remain strictly read-only. To enable it, update the My Movies source configuration: `config.remote_enabled=true`, `remote_host`, `remote_port` (default 51414), and `remote_api_key` (redacted in responses and preserved when omitted in PUT). The Remote Data API service requires an authorized professional/license, and its documentation is restricted to integrations supported by the publisher; no direct database access, reverse engineering, or modification of exports is performed. `GetTitle` verifies the identifier and title before writing. Only exports containing a numeric ID can be used for matching. This is a legacy HTTP API: restrict it to a trusted local network. The local change is retained when Kodi is offline, but the failure is reported to the client. Failed attempts are stored in a durable SQLite queue. Retries are strictly manual, through the web settings or API. MariaDB import permissions remain limited to SELECT unless SQL write fallback is enabled (requiring UPDATE for the same account).

### Kodi Write Fallback (MariaDB)

Kodi JSON-RPC remains the preferred method for rating and watched/unwatched changes. If **all** JSON-RPC instances associated with a source are unreachable (network/timeout), an **optional SQL fallback**, disabled by default, can update `movie.userrating`, `tvshow.userrating`, `episode.userrating`, and `files.playCount` / `files.lastPlayed` (for a TV show, its episode files). This fallback is **not** triggered if Kodi responds but rejects the command or the `UpdateData` permission.

Explicitly configure `config.database="MyVideos…"`, `config.mariadb_write_enabled=true`, and the source credentials `config.username` and `config.password` on the Kodi source. Use the MariaDB account already configured for the source, granting `SELECT` on identification tables and `UPDATE` limited to `movie.userrating`, `tvshow.userrating`, `episode.userrating`, and `files.playCount`, `files.lastPlayed`. The fallback requires matching external identifiers and title; it runs in an SQL transaction and rejects inconsistent data. SQL updates do not generate Kodi events: the Kodi interface may require a manual refresh.

Each PATCH exposes `method` and `attempts` in `writeback[]`, for example `["jsonrpc:unavailable","mariadb:updated"]`. A persistent log without secrets is available through `GET /media/{id}/writeback` and `GET /sources/{id}/writeback` (maximum 200 entries).

### Synchronization Status and Manual Retries (Web and iOS)

An **explicit PATCH** to `/media/{id}/personal` is the only event that creates a writeback request to Kodi or My Movies (when the My Movies API is enabled). MariaDB-to-local-catalog synchronization, Kodi scans, and XML imports **never trigger propagation**. `null` updates clear only a local preference and invalidate the pending writeback for that field.

Successful requests display a source-icon indicator alongside the movie detail. Both `GET /media/{id}` and PATCH return `writeback_state[]`: `media_id`, `source_id`, `source_name`, `source_type`, `icon` (an SF Symbol name usable on iOS, for example `play.rectangle.on.rectangle` for Kodi and `film.stack` for My Movies), `status` (`updated`, `pending`, `failed`, `unavailable`), `method`, `updated_at`, `pending_fields`, and `message`.

An unconfirmed write remains in the SQLite `writeback_pending` table (no password or external token is stored). The queue retains the latest value **per media/source/field**; a new update to one field replaces the previous value without discarding other pending fields. Every attempt is logged without secrets in `writeback_audit`. No automatic retry takes place, to avoid overwriting newer external changes. On retry, the server verifies that the source still matches the media and that the local value has not changed; stale retries are blocked (HTTP 409).

Shared API for the web interface and iOS clients:

| Method | Route | Role | Purpose |
|---|---|---|---|
| GET | `/writebacks/status` | read | `pending_count`, `pending[]`, and `recent[]` (statuses with icons) |
| GET | `/writebacks/pending` | read | All pending writebacks awaiting retry (up to 200 returned) |
| POST | `/writebacks/pending/{pending_id}/retry` | admin | Retry a specific writeback |
| POST | `/writebacks/retry` | admin | Manually retry all pending writebacks and return the results |
| GET | `/media/{id}/writeback` | read | History of writeback attempts for a movie |
| GET | `/sources/{id}/writeback` | read | History of writeback attempts for a source |

Retry responses include `results[]` (with `pending_id`, `media_id`, and `result` of type `WritebackResult`) and `pending_count`. Failed writes remain in the queue, including when the source is unavailable. The queue persists across restarts unless the local SQLite database is deleted. Deleting media or its source removes the associated orphaned writes through referential integrity.

### Refresh Kodi and Then Synchronize the Local Catalog

`POST /sources/{id}/scan` (optional `player_id`) starts a `JobView` job through a Kodi JSON-RPC player. The service first connects to Kodi WebSocket at `ws://HOST:9090/jsonrpc` (port configurable on the player through `websocket_port`), checks `UpdateData`, then sends `VideoLibrary.Scan`. Once `VideoLibrary.OnScanStarted` and `VideoLibrary.OnScanFinished` have been observed on the same connection, the job automatically proceeds to reading MariaDB and atomically publishing the SQLite catalog. Poll `GET /jobs/{id}`; the final result contains `kodi_scan="finished"`.

If the WebSocket connection is lost or completion cannot be confirmed, the job fails **without** marking the scan as completed or starting the local import. Kodi's WebSocket/TCP API must be enabled; the HTTP JSON-RPC service alone does not provide scan-completion notifications. Only one synchronization or scan job per source is accepted at a time.


## Diagnosing Kodi Deletions

Synchronization queries both **movie** and **movie_view**: the `movie` table is the authoritative source of movie IDs. Even if a stale row remains in `movie_view`, it is not imported when its `idMovie` is absent from `movie`. The job report includes `movie_table_count`, `orphaned_movie_view_rows`, and an `orphaned_movie_view_examples` sample (limited to 10 rows).

In administrator mode, the movie detail displays a **Check presence in video library** button: `GET /api/v1/media/{id}/diagnostics` queries only the MariaDB MyVideos database and checks whether the `movie_view` row has a default version (`isDefaultVersion = 1`). It distinguishes a movie still present in `movie` from one absent from Kodi's library listing. The `movie`/`files`/`path`/`videoversion` relationships remain available as secondary diagnostic information, without checking storage or making HTTP requests to Kodi.

This criterion reproduces `CVideoDatabase::GetFilter` followed by `GetMoviesByWhere` for `videodb://movies/titles/` in standard display mode (versions not flattened). Kodi's *flattened versions* mode uses another predicate (`videoVersionTypeItemType`) dependent on a local setting that is not stored in MariaDB. Non-default editions of a listed movie are retained, but movies without a valid default version are excluded, even if a row still exists in `movie`. Extras are not imported. For older schemas lacking `isDefaultVersion`, the connector falls back to legacy behavior and the report indicates `library_verified=false`; no deletion is inferred solely from a missing file.

**No Kodi instance is required**, even when a playback target has been configured. Synchronization results expose `library_verified`, `library_rule`, `library_movie_count`, `library_excluded_editions`, and `library_excluded_examples`.

Synchronization retrieves a **complete** snapshot from the `MyVideos` database actually selected. Missing editions are deleted from SQLite in the same transaction, then media entries with no remaining editions are removed (including from lists and ratings). A work that still has a CLZ edition **remains** in the catalog but loses its Kodi editions and metadata.

The `GET /jobs/{id}` result contains `database` (the MyVideos database queried), `source_records_before`, `source_records_after`, `removed_editions`, `removed_works`, `kept_clz_works`, `removed_titles`, and `removed_titles_total`. The server checks that persisted Kodi keys match the received keys exactly; an inconsistency rolls back the transaction.

If `removed_editions=0` despite a deletion in Kodi, first check the `database` field in the job result: multiple `MyVideos…` databases can coexist on MariaDB; a source configured without an explicit database name uses the highest-numbered **accessible** version (not necessarily the one used by the queried Kodi instance). Under **Settings → Kodi source → Edit**, explicitly select the correct database and synchronize again. Also check other Kodi sources and CLZ editions associated with that media. Files merely deleted from the NAS without cleaning up Kodi's video library remain referenced in the database and cannot be distinguished by this read-only connector.

`GET /jobs` and `GET /jobs/{id}` responses expose `progress_phase`, `progress_current`, and `progress_total` (rows in the current stage), along with `overall_percent` (a monotonic estimate from 0 to 100% for the whole job, without resetting between movies, TV shows, and episodes). Because MariaDB reads and SQLite publication take variable amounts of time, this **overall percentage is an estimate**, not a measurement of remaining time. Stages without a known total display an indeterminate animation while retaining a consistent estimated overall progression.

A new synchronization now skips editions whose payload is identical to the previous one. Reports return `unchanged_editions`, `rebuilt_works`, and, for diagnosing slowdowns, `mariadb_read_ms`, `sql_view_fetch_ms`, and `sqlite_publish_ms`. The server limits SQLite progress-counter updates to twice per second (plus stage changes). Cast extraction avoids expensive global sorting in MariaDB and preserves cast order per work in the application.

`GET /jobs` powers the activity indicator: scheduled and manual jobs are visible after reload, and their status remains available even if the browser or iOS app was closed during processing.

## Errors and API Evolution

Application errors have the form `{"error":{"code":"invalid_request","message":"…","details":[…]}}`; `details` is optional and does not include submitted data. Main HTTP statuses: 401, 403, 404, 409, 413, 422, 429, 502. Unexpected errors are logged on the server; secrets must not appear in responses.

The API exposes stable catalog UUIDs across reimports as long as source keys are preserved. Deleting and recreating a source may change these UUIDs. Backward-compatible changes add fields/routes to v1; breaking changes must use v2. Refer to the OpenAPI schema for exact fields, types, and constraints.

## Personal Data and Lists

All writes require administrator permissions: external Bearer, CSRF-protected session, CSRF-protected local access, or Ingress secret. Reads are also available with the read-only token.

| Method | Route under `/api/v1` | Body / result |
|---|---|---|
| PATCH | `/media/{id}/personal` | `{ "user_rating": 8.5, "watched": true }` → updated details |
| GET | `/lists` | Lists, descriptions, counts, revisions |
| POST | `/lists` | `{ "name": "Movies to Watch", "description": "Priorities" }` → created list (201) |
| PUT | `/lists/{id}` | Name and description |
| DELETE | `/lists/{id}` | Deletes only the list (204) |
| GET | `/lists/{id}/items` | Page with `items,total,limit,offset,has_more,revision` |
| POST | `/lists/{id}/items` | `{ "media_id": "UUID" }`; idempotent append to the end of the list (204) |
| POST | `/lists/{id}/items/bulk` | `{ "media_ids": ["UUID1","UUID2"] }` → `{ "added": 2, "already_present": 0, "total_requested": 2 }`; add 1–200 media IDs atomically, ignoring duplicates (200) |
| DELETE | `/lists/{id}/items/{media_id}` | Removes media from the list (204) |
| PUT | `/lists/{id}/order` | `{ "media_ids": ["UUID2","UUID1"], "revision": 3 }`; full reorder (204) |

`PATCH personal` leaves omitted fields unchanged; `null` removes the personal override for that field and restores its imported value. `user_rating` accepts values from 0 to 10. `watched` accepts true/false/null. Effective values appear in pages, details, and filters; `user_rating` sorting is available in the catalog. Editions retain their original metadata. These values are shared across the installation, without per-user profiles.

The bulk-list endpoint requires write privileges and accepts at most 200
IDs per request. It preserves the supplied order for new additions, skips
IDs already in the list, increments the list revision once when needed and
triggers one thumbnail refresh. Unknown media IDs reject the entire
transaction (404) without partial insertion. The iOS AI search flow uses
this endpoint for displayed results, without a new OpenAI request.

List items accept `sort=position|title|year|date_added|rating|user_rating`, `order=asc|desc`, `limit=1..200`, and `offset>=0`. Automatic sorting does not rewrite `position`. To reorder a paginated list, read all UUIDs at the same revision, then send each exactly once (maximum 10,000). A concurrent change returns 409; an incomplete or duplicate reorder returns 422. Mutations, including media disappearing from all sources, change the revision. The detail view includes `personal` and `lists`.

## People and TMDB Cache

`MediaDetail.people` groups `name,role,order,category,imdb_id,tmdb_id,links,thumbnail`. `category` is `cast`, `director`, `writer`, `producer`, `composer`, `photography`, or `crew`. `cast` remains available to older clients. `links` contains direct URLs with identifiers or a clearly distinguished `imdb_search`. Portraits are exposed by an authenticated relative route, `GET /media/{id}/people/{index}/artwork`; retain any Ingress prefix.

`trailers` contains validated YouTube HTTPS links (Kodi plugin links are converted). `tmdb_available` indicates whether the server has a TMDB token. `enriched_at` provides the persistent cache timestamp. `POST /media/{id}/enrich` retrieves people/portraits/trailers from TMDB for a movie or TV show identified by IMDb/TMDB, reusing the cache for 30 days. `POST /media/{id}/enrich?refresh=true` forces a refresh and invalidates the relevant portraits. Errors preserve the previous cache. No TMDB key is provided by clients or exposed in responses. Episodes are not yet enriched individually.

`GET /status` also exposes `tmdb_available`. `POST /connections/tmdb/test` requires write permissions; `422` indicates a missing key, `502` a TMDB/network error, and `200` verified access. `TMDB_API_TOKEN` accepts either the Bearer read token or a v3 key. With an explicit `edition_id`, `/players/{id}/play` sends Kodi that file's path when available; library-identifier validation still takes place first.

GET requests do not trigger TMDB requests. The administrator frontend automatically calls `POST enrich` on opening media details when necessary; an iOS app can use the same routes. Read-only clients see imported or previously enriched information. `editions[].metadata.file` exposes the Kodi path; playback of a specific version uses `edition_id` in `/players/{id}/play`.

## Refresh Remote Kodi Libraries

`POST /api/v1/sources/{source_id}/scan?player_id=...` triggers a scan on the specified instance, then refreshes the SQLite database from MariaDB **only after Kodi's scan completion has been confirmed**.

`POST /api/v1/sources/scan-all` (administrator, HTTP 202) triggers scanning of **all configured Kodi instances**. Instances sharing the same MariaDB source are scanned sequentially within a single job; SQL import runs once per source. The response is a list of `JobView` entries, tracked via `GET /api/v1/jobs` or `GET /api/v1/jobs/{id}`.

The server first attempts Kodi WebSocket notifications (`ws://host:9090/jsonrpc`, with a connection timeout of approximately 4 seconds). If this service is unavailable, it uses the **HTTP JSON-RPC endpoint already tested in Kodi settings** (`http://host:8080/jsonrpc`, configurable and using the instance's authentication) to execute `VideoLibrary.Scan`. Completion is monitored through `XBMC.GetInfoBooleans(Library.IsScanningVideo)`. A scan that was started but whose completion cannot be confirmed **does not** trigger an automatic SQL update.

If no connection can **even start** a scan, synchronization of the existing MariaDB database is still possible, but is explicitly reported as `result.kodi_scan="sql_only"`, with `kodi_scan_warnings`. This does not replace scanning the remote library. The web interface hides the estimated percentage during remote scanning (there is no reliable progress measurement), then displays MariaDB/SQLite import progress.

## Persistent List Thumbnails

Each list exposes an authenticated `thumbnail` URL:
`/api/v1/lists/{id}/thumbnails/{revision}.jpg`. The server stores
the 360 × 216 px JPEG in `DATA_DIR/list-thumbnails/`. Subsequent reads
do not regenerate the image, and the client can cache the versioned URL.
The mosaic shows the **first four items in manual order**: a 2×2 layout (4),
one large tile plus two smaller ones (3), two columns (2), full frame (1),
or a neutral placeholder (0).

Creating, editing, deleting, inserting into, removing from, or reordering
a list triggers generation of its new thumbnail in the background after
the API response. The old revision becomes inaccessible and its files
are deleted. Existing lists are initialized when first displayed.
Missing artwork is replaced with neutral placeholders. Posters are
downloaded through the same secure image-fetching layer used by the catalog,
never via an internal HTTP call to the API.

## Partially Watched TV Show Indicators

TV show details and summaries (catalog, search, lists, and detail views)
expose two additional optional fields:
`episode_count` and `watched_episode_count`. They are calculated from
episodes actually imported, including personal watched/unwatched status
overrides. The total includes specials (season 0) and does not claim to
represent episodes that were not imported.

**Partially watched** means `0 < watched_episode_count < episode_count`.
A show with all its imported episodes watched is marked as **watched**.
The existing `watch_status` is preserved for compatibility:
episode progress remains separate from the playback resume status of a video.
Seasons are already described by `watched_count` and `episode_count` in
`GET /api/v1/media/{show_id}/seasons`.

## TV Show, Season, and Episode Hierarchy

The main catalog (`GET /api/v1/media` without `kind` or `parent_id`)
and its text search list **only movies and TV shows**;
episodes are no longer displayed at the same level. Seasons are
not standalone media entries. For compatibility with older clients,
`GET /api/v1/media?kind=episode&parent_id=<show_id>` remains available
to explicitly retrieve episodes.

A TV show detail has two additional authenticated endpoints:

- `GET /api/v1/media/{show_id}/seasons`: ordered seasons, including
  season 0 (specials), with `season`, `episode_count`,
  and `watched_count`. Returns `[]` if no episodes were imported.
- `GET /api/v1/media/{show_id}/seasons/{number}/episodes?limit=100&offset=0`:
  standard `MediaPage` sorted by episode number, with pagination up to
  200 entries. Each item contains `id`, `episode`, `season`,
  `plot`, `runtime_minutes`, `watch_status`, and available artwork.
  `GET /api/v1/media/{episode_id}` returns the full details.

An unknown ID or one that does not refer to a TV show returns 404.
A nonexistent season of a valid TV show returns an empty page. These
GET requests do not reimport data or trigger a remote update.

## Mobile Application Compatibility Contract

The server version number (`version`), REST API version (`api_version: v1`),
and mobile data model compatibility are **three distinct concepts**.
The server advertises a range of supported contracts without requiring an
iOS update for every technical release:

```json
{
  "api_version": "v1",
  "version": "0.1.0",
  "mobile_compatibility": {"min_contract": 1, "max_contract": 1}
}
```

These additional fields are provided by `GET /api/v1/status` (authenticated)
and `GET /api/v1/discovery` (public, with no private data). Older
app versions ignore the new fields and continue to work as long as
the model remains compatible; **it is impossible to force a message
to appear in an already released iOS version that lacks this mechanism**.

The iOS client knows its own contract version and must check
`min_contract <= client_version <= max_contract` before reading the catalog.
If its contract is too old, it displays a screen prompting an app update;
if it is too new, it prompts an update of the server.
If an older server does not return `mobile_compatibility`, the app
assumes contract **1** is supported for backward compatibility.

Maintenance rules:
- Adding an optional field or endpoint **does not** require a new contract:
  the value stays at 1.
- When a model becomes incompatible, implement contract N+1 in both
  server and app, then increase `max_contract`. Keep `min_contract` at N
  during a transition period **only if** the server still actually supports
  the older model.
- When the old contract is no longer served, increase `min_contract`;
  keep **`/api/v1/status` and `/api/v1/discovery` available**, even if
  the rest of the API has moved to a newer version.
- Never infer compatibility from server or iOS version numbers:
  they evolve independently. Older published clients must be
  accounted for during deployment.

## Local Networks Without Access Tokens

Configure `LOCAL_AUTH_NETWORKS` (JSON array of CIDRs) and, if necessary,
`TRUSTED_PROXY_NETWORKS` in the environment. No network is allowed
by default. `GET /auth/session` returns `access_method=local` and a CSRF
token for clients permitted without Bearer; send that token in
`X-CSRF-Token` for writes. The frontend handles this automatically.
Bearer authentication remains available independently of the interface
and takes precedence over local access. Consult the README for proxy
configuration; do not enable automatic proxy-header handling in Uvicorn.

Season posters provided by TMDB are available in the
`GET /api/v1/media/{show_id}/seasons` response through the optional
`poster` field (`/api/v1/media/{show_id}/seasons/{season}/poster`,
authenticated, image served from the server cache). If no poster is
available, clients may reuse the TV show's main poster.
