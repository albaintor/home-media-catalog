# <img src="home_media_catalog/static/cinema.svg" width="36" alt=""> Home Media Catalog

**Your movie library, network videos and advanced playback — all in one place.**

Home Media Catalog combines a responsive, self-hosted **web application** with a native **iOS/iPadOS app**. The iOS app can manage a catalogue **standalone, without installing a server**. A Docker server is optional for a shared catalogue, web access and AI features; it runs on Linux, macOS/Windows with Docker Desktop, a home server or a NAS.


## Highlights

- **Combine** Kodi and CLZ Movies, MyMovies, or Filmotech (XML imports), with editions grouped by title.
- **Explore** movies, TV shows and episodes with posters, cast, formats, advanced filters and optional TMDB metadata.
- **Organize** your collection using custom lists, personal ratings and watched status.
- **Play** selected titles on an existing Kodi player, with advanced playback controls and media information.
- **Stream across your network:** browse SMB, NFS, FTP and SFTP sources on iPhone/iPad, directly in standalone mode or with your own server; also access local files and OneDrive.
- **Enjoy advanced video playback:** AVC/H.264, HEVC/H.265 and VC-1 where supported, selectable audio/subtitle tracks, compatible multichannel Dolby Atmos audio, and smooth 48/60 fps MEMC motion interpolation.
- **Play Blu-ray content:** access compatible ISO images and BDMV folder structures with disc-aware playback and track selection.
- **Access** your collection on iOS/iPadOS alone, or via a shared server and web interface.
- **Search with AI (optional, server required)** using OpenAI, Anthropic Claude or Google Gemini. OpenAI retains its optional semantic/vector search; Claude and Gemini use the selected language model with local full-text/metadata retrieval and relevance checking. No parallel multi-model inference is required.
- **Choose standalone or server mode:** in standalone mode the iOS app imports and synchronizes Kodi directly, without Docker. In server mode it synchronizes catalogue metadata with the server while Kodi remote commands are sent directly from the iPhone.

Collection-only catalogs do not require Kodi.

### Web interface

<img width="500" alt="image" src="https://github.com/user-attachments/assets/5194ecd5-8360-4c87-bc23-7610ca5eb354" />

<img width="400" alt="image" src="https://github.com/user-attachments/assets/29fcdd5e-4424-4a29-bdbc-96fa26ee25f3" />




### iOS interface

<img width="300" alt="image" src="https://github.com/user-attachments/assets/d104a967-75d8-412f-9a92-2b184563fdde" />
<img width="300" alt="image" src="https://github.com/user-attachments/assets/01d21701-3361-4055-8f92-3906b409841b" />
<img width="300" alt="image" src="https://github.com/user-attachments/assets/16a8a792-6931-42de-934c-65f4947ef564" />
<img width="250" alt="image" src="https://github.com/user-attachments/assets/c74639b9-a45e-4f1c-87f0-9998220aecbc" />



### Automatic HTTPS pairing (no certificate management)

The server automatically creates a persistent self-signed TLS identity. The existing web UI
continues to run over HTTP on `PORT=8099`, and the native API adds
`https://NAS:8443/` (configurable using `TLS_PORT`).

From **Settings → iOS Pairing**, generate a short-lived code and scan its QR
with **Scan secure HTTPS pairing QR** on the iPhone. The QR transfers the exact
server certificate SHA-256 fingerprint through the camera; iOS validates the
encrypted pairing exchange and retains the fingerprint in its device-only
Keychain. No publicly trusted certificate, DNS name or CA installation is
required.

Deletion or regeneration changes the pinned identity and requires re-pairing. Never expose
the legacy HTTP port on the public Internet. Private NAS addresses still need
VPN/routing for off-LAN access.

## Native iOS/iPadOS experience and AI search

**Home Media Catalog for iOS/iPadOS** provides ordinary catalogue search **inside the main poster/list view** on iPhone and iPad. An optional sparkles action in the top toolbar opens a separate conversational AI search page when a configured server supports it. Search results and follow-up context remain available when returning from media details.

Standalone catalogue import, browsing and direct Kodi remote control do not require the server. AI search and shared web/iOS catalogue synchronization do require the compatible server.

The web and native iOS settings can select **one language-model provider at a time**:
**OpenAI, Anthropic (Claude), or Google (Gemini)**. Credentials are configured
on the self-hosted server. This does **not** require deploying multiple language
models concurrently.

- **OpenAI:** optional hybrid search combines structured metadata filters,
  SQLite full-text matches, OpenAI embeddings and the existing locally stored
  HNSW vector index. Existing OpenAI vectors remain available after switching
  providers; the provider change does not delete the index.
- **Claude or Gemini:** the chosen language model interprets the query and
  checks candidate relevance, using locally indexed titles, metadata, plots
  and credits; this mode does **not** claim OpenAI-style vector embeddings
  or semantic nearest-neighbour retrieval.
- **All providers:** ordinary catalogue search works independently of AI.
  Where AI is enabled, prompts and selected catalogue metadata are sent to
  the selected provider, subject to its terms and rates. Review your provider's
  privacy and billing settings. Model IDs and availability may change.

To configure AI, open **Settings → Artificial Intelligence**, choose a
provider/model, save its credentials and test the connection. For OpenAI
semantic search, build or refresh the optional embeddings index. Application
budget settings are an estimate and do not replace provider-side spending
limits. See the [AI setup and privacy guide](docs/ai-search.md).

## What's new in the current development line

| Area | Server / web | Native iOS / iPadOS |
| --- | --- | --- |
| **AI search** | OpenAI, Claude and Gemini; one selected language model; optional OpenAI embeddings retained | Configurable through compatible server settings; standard search remains local to the catalogue |
| **Network explorer** | SMB, FTP, SFTP and mounted NFS sources; browser playback/remux/transcoding | Direct SMB/NFS/FTP/SFTP access in standalone or server-paired mode, plus OneDrive; browsing and file operations |
| **Playback** | Audio tracks, subtitles and chapters; remux or FFmpeg conversion | AVC/HEVC/VC-1 where supported, selectable tracks/subtitles, compatible Atmos audio, offline video and 48/60 fps MEMC |
| **Disc navigation** | Kodi library/catalogue entries | Blu-ray ISO and BDMV folder playback, with compatible title/playlist selection and DVD IFO support |
| **Access and synchronization** | Authenticated catalogue API, server backups, Kodi sync jobs | Standalone Kodi catalogue mode or optional paired server; source management and direct Kodi remote control |

Codec, Blu-ray navigation, audio output and MEMC availability depend on the media, device and playback configuration.

### Current server and client features



The project includes **more than collection management**:

- **Web video playback:** network location browser for SMB/FTP/SFTP and configured NFS mounts, HTTP Range streaming, thumbnails sampled from actual videos, and FFmpeg-powered progressive MP4 transcoding of selected non-browser-friendly formats. Browser formats, transcoding quality and seek behaviour depend on source, codec and server CPU.
- **Cloud media:** OneDrive is currently the only supported integrated provider (including Device Code sign-in for private NAS deployments). Other cloud connectors are not exposed until supported. OneDrive OAuth tokens remain encrypted on the server. Apple's iCloud Drive remains accessible through **iOS Files**, not a server-side iCloud connector.
- **Native iOS playback:** AVPlayer plus an optional PrismCore/FFmpeg-assisted route for compatible AVC/H.264, HEVC/H.265 and VC-1 videos, track/subtitle selection, chapters, multichannel audio (including Dolby Atmos on compatible playback paths), technical diagnostics, Blu-ray ISO/BDMV playback and offline files. Importing via Files copies the video to app storage; it is not a zero-copy third-party stream.
- **iOS MEMC motion interpolation:** 48/60 fps targets with Apple VideoToolbox, Vision/Metal optical-flow or AI RIFE processing, coarse-to-fine motion estimation and artifact-protection profiles. MEMC can be disabled at any time; Dolby Vision output depends on the selected video processing path.
- **Catalogue/sync:** Kodi synchronisation and cleanup, JSON backups, multi-source libraries, personalised lists and optional conversational AI search.


### iOS evaluation, purchase and support

The iOS/iPadOS client implements **10 free full-screen video playback sessions** for local files, NAS/SMB, cloud and network sources, including MEMC motion interpolation. After those sessions, a **single non-consumable StoreKit purchase** is intended to unlock the native video player, including MEMC. Kodi remote control, cataloguing and (with a configured server/provider) AI search remain outside the paid video-player gate. Purchase availability and regional pricing depend on the actual App Store Connect product; this is **not** a claim that an in-app purchase has been approved or published.


The native **Settings → Support** section links to this project's
[home page](https://github.com/albaintor/home-media-catalog) and
[issue tracker](https://github.com/albaintor/home-media-catalog/issues).
The main collection context menu uses **Play on…** to choose a configured Kodi instance; after opening playback, the app navigates to its Kodi remote tab.


### Technology credits

Video playback uses third-party components including
[FFmpeg](https://ffmpeg.org/),
[PrismCore](https://github.com/Wenzlik/PrismCore),
[MPVKit](https://github.com/Wenzlik/MPVKit),
[AMSMB2](https://github.com/amosavian/AMSMB2),
[NFSKit](https://github.com/alexiscn/NFSKit),
[libnfs](https://github.com/sahlberg/libnfs) and
[libbluray](https://www.videolan.org/developers/libbluray.html).
Some components have **LGPL obligations**, including requirements relevant to
linking, replacement, modification, attribution and corresponding source
materials. The current iOS development configuration uses a
[PrismCore adapter and dynamic FFmpeg builds](https://github.com/albaintor/ffmpeg-apple),
but **dynamic FFmpeg alone does not establish application-wide compliance**.
See [third-party licences, usage and release obligations](THIRD_PARTY_NOTICES.md)
for component details and unresolved compliance checks.

## Get started

### iOS/iPadOS without a server

1. Build/install the mobile app from the App Store
2. Add a Kodi instance via local-network discovery or its hostname/IP address. Enable and configure Kodi's **HTTP JSON-RPC remote control**. Import the library directly from Kodi; an external MariaDB database is **not required** for the normal JSON-RPC import.
3. Alternatively, import a compatible CLZ Movies, My Movies or Filmotech XML collection. Configure media playback sources separately as needed. AI search and browser access are unavailable without a server.

### Shared web + iOS/iPadOS catalogue (optional)

1. [**Install the Docker server**](docs/installation.md) on your preferred host.
2. In the web interface, add a Kodi HTTP/JSON-RPC instance or import a supported collection XML. An optional **read-only MariaDB/MyVideos fallback** can help preserve additional Kodi editions when JSON-RPC is insufficient.
3. Pair the iOS/iPadOS client with a short-lived code from **Settings → iOS Pairing**. Keep the server and app at mutually supported API/data-model versions. Use HTTPS or a trusted VPN outside the local network.

## Documentation

- [Server installation, configuration, updates and troubleshooting](docs/installation.md)
- [iOS / Xcode Cloud setup, SwiftPM recovery and signing](docs/ios-xcode-cloud.md)
- [iOS StoreKit purchases, trial and Debug reset](docs/ios-storekit.md)
- [Features and usage](docs/features.md) · [AI search and model setup](docs/ai-search.md)
- [Home Assistant access](docs/home-assistant.md)
- [REST API](docs/api.md) · [OpenAPI schema](docs/openapi-v1.json)
- [Data model and backups](docs/model.md) · [Development and testing](docs/development.md)
- [Third-party licences, component usage and distribution obligations](THIRD_PARTY_NOTICES.md)

Home Media Catalog includes an **optional network/cloud media browser and video playback workflow**. The browser can transcode some remote video formats with FFmpeg; this consumes server CPU and is not equivalent to direct/native playback. Native iOS playback uses AVPlayer and PrismCore. Use **HTTPS or a VPN** for remote access.

[Support the project on Buy Me a Coffee](https://www.buymeacoffee.com/albaintor)


## Support

If you have any issues check first if it has not already been reported then [raise a ticket](https://github.com/albaintor/home-media-catalog/issues)



### CLZ, MyMovies and Filmotech imports

In Settings, select the collection type, choose an existing collection (or create
one), and upload its XML export. When paired with a server, the native iOS application uses the same import
endpoints: `POST /api/v1/imports/clz`, `/imports/mymovies`, and `/imports/filmotech`. In standalone mode, compatible XML is imported on-device instead.
MyMovies refers to the XML collection export from My Movies Collection Management;
Filmotech uses its XML film export, rather than a database backup or PDF report.

Each provider has a distinct icon in Settings, catalog cards and movie editions.
Imports replace only the selected collection. Matching IMDb/TMDb identifiers (or
an unambiguous title and year across providers) group editions on the same record.
Invalid, empty, unsafe XML and duplicate entry IDs leave the previous collection
intact. Local cover paths are still subject to the server's artwork access rules;
exporting XML does not upload separate image files.

The parser fixtures in `tests/fixtures/filmotech.xml` and `mymovies.xml` are
representative test documents. Validate an export from your application version
before replacing a large collection; provider-specific XML variants may differ.


### Automatic Kodi/MariaDB catalogue refresh

The server imports an empty catalogue once when possible. Later, opening
the web app or connecting from iOS requests a `/api/v1/sources/sync-stale`
check. A successful Kodi-source import is eligible for automatic refresh only
after 24 hours. JSON-RPC is the preferred import path where supported; MariaDB
is an optional read-only fallback, and may be required to preserve additional
editions not exposed by JSON-RPC.

Automatic imports also have a **durable 24-hour attempt cooldown**. If Kodi
or the configured MariaDB fallback is unavailable, the failed job's
`created_at` in the persistent jobs table prevents repeated imports on
every app or server restart. Manual **Synchronize** and **Scan Kodi**
actions are unaffected. The server does not initiate a Kodi library scan as
part of the automatic refresh.

A successful import updates the source's `last_sync`. The jobs feed shows
the latest attempt and any failure; this can be used to distinguish a simple
startup freshness check from a full import.
