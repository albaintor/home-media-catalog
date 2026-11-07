# Features and usage

[← Overview](../README.md) · [Install the server](installation.md)

## Current development features (November 2026)

The native client and server/web application are developed separately. Availability
depends on the checked-out branch, server compatibility, network source and device.

- **AI:** choose OpenAI, Anthropic Claude or Google Gemini as the single language
  model provider on a compatible server. OpenAI keeps its existing hybrid
  embedding/FTS search; switching to Claude or Gemini uses local full-text
  and metadata candidate retrieval with language-model relevance validation.
  Switching providers does not delete the stored OpenAI vector index.
  [Provider setup and data disclosure](ai-search.md).
- **Native remote explorer:** SMB/NFS/FTP/SFTP and OneDrive where configured;
  source-dependent streaming, recursive file search and context actions such
  as rename, copy, move and delete. File-modification permissions depend on
  standalone/server mode and the source. OneDrive remains read-oriented for
  these operations. The browser does not inherit the iOS app's local credentials
  or administrative capabilities automatically.
- **Disc images and folders (native development branch):** Blu-ray ISO title
  selection, DVD `VIDEO_TS.IFO` / `VTS_XX_0.IFO` exploration, and BDMV
  `index.bdmv` or `BDMV` directory playlist inspection. Playlists can be
  sorted by calculated duration. Simple, single-clip BDMV playlists may stream
  directly; authored clip IN/OUT points, multi-clip playlists, seamless branching,
  menus and angles need more complete `libbluray` navigation. The new paths
  have **not** passed a device-level validation gate.
- **Native MEMC (experimental):** 48/60 fps frame interpolation with
  VideoToolbox and Metal/Vision alternatives; coarse-to-fine block motion,
  occlusion rejection and conservative local panorama correction are under
  active development. The prior crash-prone global panorama dispatch remains
  disabled. This is not SVPflow or RIFE; successful performance and artefact
  parity are **not** asserted.
- **Licence transparency:** see [third-party software, LGPL obligations and
  release checks](../THIRD_PARTY_NOTICES.md). An experimental dynamic-FFmpeg
  dependency does not make every transitive library App Store compliant.

## Built-in catalogue, barcode identification and collection grouping

The **internal catalogue** is available alongside Kodi, CLZ Movies, My Movies and Filmotech. A movie, TV series or physical DVD/Blu-ray edition can be added without a playable file, an XML export or a Kodi instance. It is usable through the web server and in the native iPhone/iPad app, including iOS standalone mode.

1. **Identify by title:** search TMDB progressively and choose a result with its title, year, media type and poster. The selected poster stays visible while editing; the user can switch to another result.
2. **Identify by barcode:** enter an EAN/UPC/GTIN on the web, or enter/scan it with the iPhone/iPad camera. The catalog is checked first when available, followed by external product indexes. The first available TMDB search result can be **preselected** following a successful barcode lookup, but must be reviewed before adding: the product barcode identifies an edition, **not** a TMDB film ID. Barcode database coverage and availability are not guaranteed.
3. **Match across sources:** add the internal physical edition to the existing title when a strong TMDB/IMDb identifier matches or the title/year match is unambiguous and compatible. Different editions can therefore be displayed within **one media detail** even when they come from CLZ, Kodi or another source.
4. **Remove individually:** use the catalogue item context menu or the button at the bottom of its media details to remove an internal edition. This action must not delete any edition belonging to CLZ, Kodi, My Movies or Filmotech.
5. **Browse by collection:** group catalog items by collection instead of displaying only individual titles. The web and iOS navigation show collection artwork and allow opening a group to browse its members. Imported collections are separate from user-maintained custom lists.

TMDB identification requires access to TMDB: the server's configured API token for web/paired operation, or a TMDB token configured on-device for standalone mode. The catalogue still supports manual title entry without that service. Local and server-synchronized behavior depends on compatible client/server versions.

## Full feature list

- Title and original-title search remains available. **The current iOS development client enables advanced search by default** (with a one-time migration of older saved defaults), while users can still disable it in Filters and sorting. Advanced search covers synopses, genres, collections, cast and credits. Multiword person names such as **John Williams** are matched against the exact normalized credit-name index where available, avoiding false positives from independent tokens in unrelated metadata. If no credit is indexed, capitalized names fall back to adjacent FTS phrase matching; ordinary keyword searches still support flexible AND matching and prefix typing.
- Combinable filters: movies/TV shows/episodes, Kodi/CLZ, genre, video/audio/disc formats (multiple selections combined with AND), collection, watched/unwatched/in progress/unknown, and additions from the past 30 days. The API also supports release year, a specific source, and parent TV series. Video resolution **4K** is distinct from physical **4K UHD / Blu-ray 4K** discs. Filter and sort preferences are saved in the browser.
- Sort by title, date added, year, rating, or last watched, in ascending or descending order. Infinite scrolling loads batches of 60 items, with a fallback button and recovery after errors.
- Poster grid and list views; detailed media pages with ratings, synopses, physical editions, Kodi files, resume positions, and playback history.
- Responsive details with Kodi cast members and roles, resolution and 3D badges, Dolby Vision/HDR10/HDR10+, Dolby/DTS audio, and physical media formats. Per-edition technical details remain available through the API.
- **Read-only** synchronization from Kodi MariaDB, covering movies, TV shows, episodes, IMDb/TMDB/TVDB identifiers, and alternative versions when exposed by `movie_view`.
- CLZ Movies XML import: editions, Blu-ray/4K/3D/DVD formats, barcodes, box sets, languages, subtitles, and `seenhistory`.
- Kodi/CLZ/My Movies/Filmotech/internal matching using reliable external identifiers and media type, with a conservative fallback to unambiguous title **and** year matches. Editions remain attached to their respective sources.
- Multiple Kodi HTTP/JSON-RPC players associated with a MariaDB source. Play or resume on an **already running** Kodi instance without relying on Home Assistant entities.
- MariaDB and Kodi connection tests before adding a source: networking, authentication, MyVideos database, read permissions, and Kodi HTTP control. These tests are also available through the API.
- Persistent local SQLite database and artwork cache: **no second MariaDB server**.
- Administrator token, optional read-only token, browser sessions with CSRF protection, and a separate Ingress secret.

The web interface uses exactly the same API as other clients. No essential server-side business logic lives exclusively in the frontend.

## Native iOS/iPadOS application

The native SwiftUI client uses the same versioned API and catalog as the web interface. After the server is installed, use local Bonjour discovery where available, or enter the server URL. Generate a short-lived code in **Settings → iOS Pairing** in the web interface. A native client uses its own token for remote access, not the Home Assistant Ingress secret.

## Cast, portraits, and trailers

Media details distinguish cast, directing, writing, production, composing, cinematography, and other credits. Kodi provides roles, directors/writers, and portraits already available in its database. CLZ-exported credits/portraits and YouTube links are imported when present. The original sample export has empty cast sections: images visible in CLZ Cloud are not included in that XML.

To enrich movies and TV shows automatically with TMDB, place the **API Read Access Token** from [your TMDB account](https://www.themoviedb.org/settings/api) in `.env`, then rebuild or restart the server as described in the [installation guide](installation.md#updating-an-existing-installation). An **API v3 key** is also accepted in the same setting. Your TMDB account password is not valid here.

```dotenv
TMDB_API_TOKEN=your_TMDB_read_access_token
```

Under **Settings → Metadata and portraits · TMDB**, the UI reports whether TMDB is configured and offers **Test TMDB connection**. Invalid credentials, network problems, and rate limits produce explicit errors. Each media page also indicates when TMDB is unavailable. Enabling TMDB does not require reimporting the catalog: reopening a media page is sufficient when it already has an IMDb/TMDB identifier.

An administrator opening a media page triggers enrichment using the title's IMDb/TMDB identifier. The process fetches cast, crew, portraits, and YouTube trailers without fuzzy title matching. **Names and portraits** link to TMDB/IMDb profiles; otherwise an explicitly labeled **IMDb search** link is provided. The YouTube button appears at the top of the media page: it opens the only trailer directly, lets you choose when several are available, or searches YouTube if no trailer was supplied.

Metadata persists in SQLite across restarts and synchronization. It is reused for 30 days and refreshed at the next opening afterward. **Refresh TMDB metadata** forces an update and invalidates affected cached images; if a request fails, previously saved information is retained. Portraits use the same authenticated image proxy, 30-day/512 MiB cache, and security checks as posters. API clients can request enrichment independently of the web frontend. TMDB is optional: imported or cached content remains accessible without network access or a TMDB token.

The browser remembers search terms, filters, sort direction, view mode, and other preferences. Facets that no longer exist in the catalog are ignored on reload. Background catalog scrolling is locked while media details or a list are open and its position is restored on close; nested dialogs have independent scrolling.

## Ratings, watch status, and lists

At the top of each media page, the star control lets you set **your personal rating out of 10** using hover, click, or keyboard input; a star or half-point slider saves immediately. The **Watched / Unwatched** badge toggles and saves the status on click without a separate Save button. These personal values are shared by users of the installation, persist in SQLite, and take precedence over imports. Clearing a rating or choosing **From sources** restores the imported value. Public IMDb ratings and imported metadata remain separate. When a Kodi JSON-RPC player is configured for the matching MariaDB source, personal ratings and watched/unwatched changes are also written back using Kodi's `VideoLibrary.Set*Details` methods (including the episodes of a series when its watch status changes). No write access to MariaDB is required. CLZ and Filmotech remain strictly read-only. My Movies XML imports also remain read-only, but an explicitly enabled, licensed/professional My Movies Remote Data API can receive **watched/unwatched** changes via `ElementWatched` and `ClearElementWatched`. My Movies exposes no documented personal rating setter, so ratings remain local. Its HTTP API credentials must only be used on a trusted network. The app reports each writable source's result, and keeps the local override with an explicit warning if Kodi is unavailable or rejects the update. Half-point ratings are kept exactly in SQLite but rounded to the nearest integer in Kodi, which supports integer user ratings. Choosing **From sources** only clears the local rating override and never erases the rating in Kodi. A running Kodi instance with `UpdateData` permission is necessary for immediate write-back; failed writes are not queued for retry.

In the catalog, the **Custom lists** section offers **＋ Create a list** and **Open my lists**. **My lists** also remains accessible in the header. Creating a list opens the name form directly; the same form appears on first opening when no list exists. Administrator access is required to create or edit lists; read-only access displays an explanation in the dialog.

**My lists** supports creating, renaming, and deleting lists; searching for **movies or TV shows** to add via **＋ Add media**; removing items; and moving them with ↑/↓ in **Manual order, ascending** mode. A media page also has a **＋ Add to list** button, including the option to create a list from that dialog; existing associated list names are clickable. The last opened list and its sorting preference are saved in the browser. Sorting by title, year, date added, public rating, or personal rating changes the view only and does not modify the saved manual order. Lists use pagination and revision numbers to prevent concurrent changes from silently overwriting a reordering operation. They are separate from imported CLZ collections and box sets.

Work UUIDs preserve list associations during normal synchronization. If a media item disappears from every source, it is removed from its lists; deleting a list does not delete any media. Upgrading automatically migrates SQLite from schema version 1 to version 2. Back up the `data` volume before downgrading: older releases cannot read schema version 2.


## Kodi write-back fallback and scan-triggered refresh

The source settings allow an explicitly enabled MariaDB write fallback with a dedicated write account. HTTP JSON-RPC remains the preferred update path; fallback is attempted only when all Kodi endpoints are unreachable, never when a write is rejected. A durable audit records the method, attempts and outcome, accessible from the source's History control and API. Direct SQL updates do not notify Kodi, so Kodi's UI can require a refresh. Kodi versions and database schemas differ; use limited table/column grants and an explicit `MyVideos` database.

Use **Refresh Kodi** on a source with an associated Kodi player to request `VideoLibrary.Scan` over the Kodi WebSocket interface (normally TCP 9090, configurable independently of the HTTP JSON-RPC port). The background job waits for `VideoLibrary.OnScanFinished`, then imports Kodi MariaDB again into the local catalogue. If notification monitoring fails or times out, the job fails instead of falsely reporting successful publication. Kodi's remote JSON-RPC TCP/WebSocket interface must be enabled for this feature.

## Kodi file paths and playback

Full file paths are visible on media pages, with a **Copy path** action (on HTTP, a copy dialog is shown instead of clipboard access). **Play on Kodi** contacts the selected player from the backend and lets you select the file/version. Explicit selection opens that exact path, even for a default version. Kodi uses its own access permissions to the media files.

If no player is associated with the source, the media page offers **Configure a Kodi player**. The Kodi-logo button opens playback options: player, file/version, and resume position. HTTP API and file-read permission tests are available for saved players under **Settings**. Playback launch errors remain visible in the playback dialog. A successful command means Kodi accepted it; access to the file still depends on Kodi's media storage permissions.

Web browsers do not provide native SMB/NFS playback. Opening these protocol links depends on a local protocol handler and its share permissions. `file://` links are also restricted from web pages. There is no universal local-playback link; users can open the path in a compatible player with access to media files. To watch in the browser, use the web player described below. [Browser protocol handlers](https://developer.mozilla.org/en-US/docs/Web/API/Navigator/registerProtocolHandler) · [Kodi Player.Open](https://kodi.wiki/view/JSON-RPC_API/v13.11).

## Web video player

**Play in the browser** (Kodi playback dialog) and files opened from the network explorer use a full-window player modelled on the iOS app's controls:

- **Audio tracks**, **subtitles** (size S–XL and ±timing offset) and **chapters** (list, markers on the timeline, previous/next), detected server-side with FFprobe.
- Aspect ratio (fit, fill, stretch, zoom), playback speed, conversion quality, technical details, Picture in Picture, full screen and Media Session (keyboard media keys / lock screen).
- Keyboard: Space/K play-pause, ←/→ or J/L ±10 s, ↑/↓ volume, M mute, F full screen, C subtitles, A audio, N/P or Page Down/Up chapters, I details.
- The volume, preferred audio/subtitle languages, subtitle size and aspect mode are remembered per browser. Forced subtitles in the spoken language are enabled automatically.

Three playback modes, shown in the top-right chip:

| Mode | When | Server cost |
| --- | --- | --- |
| **Direct** | MP4/MOV/WebM whose codecs the browser can decode, with the default audio track (or any track in Safari). | None (HTTP ranges). |
| **Remux** | MKV/AVI/TS, or another audio track: video copied (H.264, or HEVC when the browser supports it), audio converted to stereo AAC. | Low. |
| **Conversion** | Video the browser cannot decode, bitmap subtitles (PGS/VobSub/DVB, burned in), or *Always convert video*. HDR is tone-mapped when FFmpeg has `zscale`. | High: at most three concurrent streams. |

Seeking in Remux/Conversion restarts the server stream at the requested time (positions already buffered stay instant). Text subtitles (SRT, ASS, mov_text, WebVTT) are extracted to WebVTT in a single sequential read of the file and cached under `DATA_DIR/subtitles`; on a large remux over a slow share the first extraction can take a while. Picture in Picture shows the video without the player's subtitle overlay.

For Docker deployment, permissions, updates and troubleshooting, see [server installation](installation.md).
