# External data services: streaming, barcodes and disc metadata

[← Overview](../README.md) · [Features](features.md) · [API](api.md)

This page is the **central inventory** of external information sources used by
Home Media Catalog (HMC), including sources called directly by the iOS client.
The server and the iOS app have different integrations: a service being listed
here does **not** mean that both applications call it. This inventory is based on
server `main` and iOS `streaming` as of **21 November 2026**.

**Status legend:** **Active** = implemented in the indicated code path;
**Optional** = implemented but used only with a key, user setting or fallback;
**Local** = no third-party request; **Investigated / not implemented** = a
possible integration, not an active resolver. No claim about live service
availability, contractual authorization or current free-tier quotas is implied.

## Streaming titles, availability and direct-opening links

| Service / reference | iOS | Server/web | What HMC uses it for | API / documentation |
| --- | --- | --- | --- | --- |
| **TMDB (The Movie Database)** | **Active** (on-device API token for standalone discovery) | **Active** (configured `TMDB_API_TOKEN`) | Movie/series IDs, posters, episodes, regional SVOD provider availability and search; **does not return a reliable Netflix/Disney+/Prime Video content ID or direct provider playback URL** | [TMDB API](https://developer.themoviedb.org/docs/getting-started), [watch providers](https://developer.themoviedb.org/reference/tv-series-watch-providers) |
| **Wikidata external provider identifiers** | **Active**: device queries an exact TMDB movie/series ID via SPARQL (`P4947` / `P4983`) and resolves supported Netflix (`P1874`), Prime Video (`P14462` GTI, `P14440`/`P8055`), Disney+ (`P13902`/`P7596`/`P7595`) and Apple TV (`P9751`/`P9586`) links | **Not used** for this streaming resolution | Automatic iOS-first provider-link discovery; unambiguous provider IDs are mapped to validated official URLs and locally cached, then synced when authorized. Availability and universal-link app handoff remain provider-dependent | [Wikidata Query Service](https://query.wikidata.org/), [property P14440](https://www.wikidata.org/wiki/Property:P14440) |
| **Official Netflix Top 10 (Tudum)** | Not an iOS ID resolver | **Active**, used for the Netflix-specific ranking when available | Country-specific historical weekly rankings, **not** viewing history, provider IDs or deep links | [Netflix Tudum Top 10](https://www.netflix.com/tudum/top10), [country TSV used by HMC](https://www.netflix.com/tudum/top10/data/all-weeks-countries.tsv) |
| **Official streaming-service URLs** (Netflix, Disney+, Prime Video, Apple TV, Max, Paramount+) | **Active** for automatically resolved links, previously synchronized links and fallback title search | **Active** for validating/importing official links via API | Open a known verified title/episode URL or fall back to a provider-scoped title search. Automatic Wikidata resolution only covers supported provider identifiers; not a universal deep-link API | [Netflix](https://www.netflix.com/), [Disney+](https://www.disneyplus.com/), [Prime Video](https://www.primevideo.com/), [Apple TV](https://tv.apple.com/), [Max](https://www.max.com/), [Paramount+](https://www.paramountplus.com/) |

### Link lifecycle and limitations

- **iOS-first:** tapping **Open on [platform]** checks the device's known
  `link:movie:…`, `link:tv:…` and optional episode records, then attempts
  an **automatic Wikidata lookup using the exact TMDB ID**. A single valid
  provider ID is converted to an official destination, retained in the local
  `StreamingLibrary` journal subject to the catalogue-storage preference,
  and synchronized to the server when connected with write access. The manual
  "Associate copied streaming link" UI has been removed. A lookup miss, HTTP
  failure or unsupported provider leads to a normal title search. Failed
  lookups are remembered briefly in device memory to avoid repeated requests.
- **Verified built-in title exception:** the iOS app includes a reviewed
  official Prime Video title URL for *The Lord of the Rings: The Rings of Power*
  (TMDB TV `84773` → `https://www.primevideo.com/detail/0TUVXIO58IUNEPNBF8363Z7YGL`).
  It is preferred over a previously stored Amazon Video ASIN-based URL,
  cached with provenance `verified_official_catalogue` and synchronized
  through the normal streaming state journal. This is a **single verified
  entry**, not an automatic Prime Video ID conversion for every series. Prime's
  title page includes a season selector; exact episode playback is not guaranteed.
- **Bidirectional sync:** `GET /api/v1/streaming/state` and
  `POST /api/v1/streaming/state/sync` merge these records by timestamp via
  `streaming_state`. A server administrator can also submit a verified URL
  using `POST /api/v1/streaming/links/import`; the next iOS state sync can
  import that mapping. A title's streaming-provider membership and its
  provider-specific URL are **different pieces of data**.
- **Episodes:** an episode-specific URL is preferred when known. Otherwise
  HMC opens the known series URL or provider title search. **Opening a
  series URL does not guarantee automatic resume or selection of the next
  unwatched episode.** That choice depends on the service's own application.
  The direct link may open an app through iOS universal links or remain in
  a browser; neither behavior nor direct playback is universally guaranteed.
- **Privacy and accuracy:** never manufacture Netflix/Prime/etc. IDs from TMDB
  IDs; validate the host and content path before storing a shared URL. A link
  can become obsolete or be unavailable in France. A universal link is not
  proof that content is included in the user's subscription.

### Additional streaming URL resolvers considered, **not integrated**

Wikidata resolution is implemented for the supported provider properties above.
The following **additional** APIs are not yet called by HMC:

| Candidate | Potential use | Important constraint | Reference |
| --- | --- | --- | --- |
| **Streaming Availability API (Movie of the Night)** | Query a TMDB/IMDb identifier for country/service streaming options and direct destinations | API key, quotas, attribution, licensing and storage rights must be checked before integration | [API docs](https://docs.movieofthenight.com/), [show lookup](https://docs.movieofthenight.com/resource/shows), [authentication](https://docs.movieofthenight.com/guide/authentication) |
| **JustWatch official API / widget / exports** | Regional offers and provider navigation | Official integrations are partner-oriented; no HMC integration; unofficial GraphQL access is not a stable public contract | [JustWatch partner docs](https://apis.justwatch.com/docs/) |
| **Watchmode** | Match TMDB/IMDb IDs to availability and provider destinations | Account and plan limitations; deep-link/episode features and usage rights vary | [Watchmode API](https://api.watchmode.com/docs), [pricing](https://api.watchmode.com/pricing) |

**Current iOS order:** known local/synchronized link → exact TMDB-to-Wikidata
mapping for supported properties → provider title search. **Potential future
enhancement:** add a licensed streaming-link API between Wikidata and search,
subject to access and storage rights. The server does not perform the automatic
lookup on behalf of iOS.

For example, Wikidata associates TMDB TV ID `84773` (*The Lord of the Rings:
The Rings of Power*) with the Amazon Video ASIN `B09QH98YG1`. A Prime Video mobile GTI link, if supplied, is preferred over a regular
`/detail/` ID; otherwise the
documented Amazon Video detail URL may be opened. A valid Amazon Video URL
does **not** guarantee that iOS opens the Prime Video app instead of a browser,
or that it selects a particular season or episode.

## Physical-disc barcode and technical metadata lookup

An EAN/UPC/GTIN identifies a **physical product edition**, not the canonical
film/series. HMC validates GTIN checksums, handles UPC-E/UPC-A/EAN aliases and
tries exact code matches. External titles, cover images, release metadata,
audio/subtitle formats and HDR fields are not uniformly available.

| Provider / data source | iOS | Server/web | Data and behavior | Reference |
| --- | --- | --- | --- | --- |
| **Existing local catalogue + barcode cache** | **Local**, first | **Local** cache/cross-references | Previously known editions and accepted results; preferred over unnecessary online requests | HMC internal storage |
| **Imported offline disc index** | **Local**, optional | Not currently a server lookup source | User-imported JSON built from TheDiscDB open dataset; exact GTIN matches, technical tracks when present | [TheDiscDB data repository](https://github.com/TheDiscDb/data), [iOS offline-index instructions](https://github.com/albaintor/home-media-catalog-ios/blob/streaming/docs/physical-disc-index.md) |
| **TheDiscDB live GraphQL** | **Active** when online barcode lookup enabled | **Not implemented** in server barcode lookup | Specialist disc edition, release, region, disc structure, video/audio/subtitle tracks and IMDb reference where supplied; exact GTIN matching | [TheDiscDB](https://thediscdb.com/), [GraphQL endpoint](https://thediscdb.com/graphql), [open data](https://github.com/TheDiscDb/data) |
| **UPCMDB** | **Active**, first wave for UPC-A | **Active**, first wave for UPC-A | Film/disc-specific title, year, format and sometimes technical detail/IMDb ID | [UPCMDB API endpoint](https://upcmdb.com/api/v1/lookup/) |
| **UPCitemdb** | **Active**, first wave | **Active**, first wave | General product descriptions, categories and sometimes pictures; must confirm disc relevance | [UPCitemdb API](https://www.upcitemdb.com/api/explorer#!/lookup/get_trial_lookup) |
| **Bibliothèque nationale de France (BnF), SRU** | **Active**, first wave when EAN-13 available | **Active**, first wave when EAN-13 available | French bibliographic/video edition records, EAN and descriptive metadata | [BnF API / SRU](https://api.bnf.fr/), [SRU endpoint](https://catalogue.bnf.fr/api/SRU) |
| **Open Products Facts** | **Active**, additional fallback | **Active**, only after initial miss | General product title/category; filters non-video products. Source licensing applies to reuse | [Open Products Facts](https://world.openproductsfacts.org/), [API](https://world.openproductsfacts.org/data) |
| **Wikidata product identifiers** | **Active**, additional fallback | **Active**, only after initial miss | Exact product code via Wikidata property **P3962** and descriptive labels. **Not** streaming content IDs | [Wikidata Query Service](https://query.wikidata.org/), [P3962](https://www.wikidata.org/wiki/Property:P3962) |
| **Sudoc / ABES** | **Active**, additional fallback | **Not implemented** | ISBN/EAN-to-bibliographic-record resolution for physical-video editions | [Sudoc](https://www.sudoc.fr/), [EAN → PPN](https://www.sudoc.fr/services/ean2ppn/) |
| **Barcode Lookup API** | **Not implemented** as an iOS source | **Optional**, only after first-wave miss and with `BARCODELOOKUP_API_KEY` | General barcode product metadata | [Barcode Lookup API](https://www.barcodelookup.com/api) |
| **Tavily Search API** | **Optional**, personal API key | **Not implemented** in server lookup | Supplemental web search for precise barcode/edition metadata when the structured sources are inconclusive | [Tavily](https://docs.tavily.com/) |
| **Serper** | **Optional**, personal API key | **Not implemented** in server lookup | Alternative search-engine results for edition matching | [Serper](https://serper.dev/) |
| **Brave Search API** | **Optional**, personal API key and explicit storage-rights setting | **Not implemented** in server lookup | Alternative search-engine results for edition matching | [Brave Search API](https://brave.com/search/api/) |
| **Apple Vision barcode detection / back-cover OCR** | **Active**, **on-device** | Not applicable | Recognize barcode and printed audio/subtitle/HDR information from disc packaging; no third-party barcode service needed for local capture | [Vision framework](https://developer.apple.com/documentation/vision/) |
| **On-device Apple text interpretation (where supported)** | **Optional / on-device** | Not applicable | Structure recognized back-cover text into edition fields; does not upload scanned text to an external AI provider | [Apple Foundation Models](https://developer.apple.com/documentation/foundationmodels) |
| **TMDB** | **Active**, with API token | **Active**, with API token | Resolve/enrich the **underlying film or series** after identifying an edition, and obtain episode information/posters; **not** a retail-barcode database | [TMDB API](https://developer.themoviedb.org/) |

### Actual lookup order

**iOS (`streaming` branch):** local catalogue/offline index → cached lookup →
parallel UPCMDB, UPCitemdb, BnF and TheDiscDB requests → Open Products Facts,
Wikidata and Sudoc if the first wave lacks sufficiently reliable disc evidence →
optional configured Tavily/Serper/Brave search when identity or technical
metadata remain incomplete. A response is evaluated against product/edition
evidence; a mere text match is not proof of a particular pressing.

**Server (`main`):** UPCMDB + UPCitemdb + BnF → on an initial miss:
Open Products Facts and Wikidata, plus Barcode Lookup if configured. The
server does **not** currently call TheDiscDB, Sudoc or the iOS's configured
web-search APIs. Both paths independently support TMDB title matching.

**Offline mode:** the iOS offline index is constructed using
`scripts/build_disc_index.py` and imported explicitly by the user.
Disabling online barcode lookups prevents the barcode service requests, but
a separate optional TMDB enrichment may still require Internet access.
Product-index completeness, translated audio-language extraction and
exact-edition metadata are **not guaranteed**.

## Configuration, security and source code

- **On-device privacy:** search API keys are saved in the iOS Keychain rather
  than published to the NAS. Barcodes and packaging may be sent to the
  external barcode lookup endpoints when online lookup is enabled; local
  scanning/OCR and offline-index lookup do not require that transmission.
  Check each external source's terms and data-reuse restrictions before
  shipping/caching third-party images or catalog entries.
- **Server credentials:** `TMDB_API_TOKEN` is configured on the server and
  `BARCODELOOKUP_API_KEY` enables its optional product lookup; never embed
  private keys in the repository or public docs.
- **Quota policy:** plans, rate limits and licensing change. Follow providers'
  linked documentation instead of treating any previously observed free-tier
  allowance or persistence rights as permanent.
- **Implementation references:** [iOS barcode and enrichment pipeline](https://github.com/albaintor/home-media-catalog-ios/blob/streaming/HomeMediaCatalog/Library/CatalogInternalAdd.swift),
  [iOS streaming state and provider URL handling](https://github.com/albaintor/home-media-catalog-ios/blob/streaming/HomeMediaCatalog/Streaming/StreamingCore.swift),
  [server barcode pipeline](../home_media_catalog/internal_catalog.py),
  [server streaming routes](../home_media_catalog/streaming.py),
  [server TMDB enrichment](../home_media_catalog/enrichment.py).

This inventory should be updated whenever a provider is added, removed, made
mandatory or used for a new kind of identifier.
