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
| **Streaming Availability API (Movie of the Night)** | **Optional** when a personal API key is configured in iOS **Customize Streaming**; exact TMDB ID, FR streaming options, title/season/episode links | **Not called** by the server | Provides actual per-service destinations for Netflix, Prime Video, Disney+, Apple TV, Max and Paramount+ when listed; only subscription (not rental/addon) destinations are considered; key remains in iOS Keychain | [API docs](https://docs.movieofthenight.com/), [authentication](https://docs.movieofthenight.com/guide/authentication), [show lookup](https://docs.movieofthenight.com/resource/shows) |
| **Official Netflix Top 10 (Tudum)** | Not an iOS ID resolver | **Active**, used for the Netflix-specific ranking when available | Country-specific historical weekly rankings, **not** viewing history, provider IDs or deep links | [Netflix Tudum Top 10](https://www.netflix.com/tudum/top10), [country TSV used by HMC](https://www.netflix.com/tudum/top10/data/all-weeks-countries.tsv) |
| **Official streaming-service URLs** (Netflix, Disney+, Prime Video, Apple TV, Max, Paramount+) | **Active** for automatically resolved links, previously synchronized links and fallback title search | **Active** for validating/importing official links via API | Open a known verified title/episode URL or fall back to a provider-scoped title search. Automatic Wikidata resolution only covers supported provider identifiers; not a universal deep-link API | [Netflix](https://www.netflix.com/), [Disney+](https://www.disneyplus.com/), [Prime Video](https://www.primevideo.com/), [Apple TV](https://tv.apple.com/), [Max](https://www.max.com/), [Paramount+](https://www.paramountplus.com/) |

### Link lifecycle and limitations

- **iOS-first:** **Open on [platform]** uses the locally known URL
  (including synchronized mappings), then optionally queries **Streaming
  Availability API v4** using the exact TMDB identifier and country `fr`,
  then queries **Wikidata** for exact provider IDs. Only validated official
  HTTPS destinations are cached in `StreamingLibrary` and synchronized to
  the NAS when connected with write access. In **standalone iOS mode**,
  links persist in SQLite across app launches. When paired to a server,
  local link persistence follows **Synchronize and store the entire catalog
  locally**; with that option disabled, resolved links remain available in
  memory until the server sync but do not form a full persistent local
  provider-link snapshot. The API key is optional, stored only in the iOS
  Keychain; no manual per-title link association is required.
  When no trustworthy direct URL is available, HMC opens the provider's title
  search. Brief negative caching avoids repeated failing network requests.
- **Generic resolution:** no hardcoded TMDB-to-provider exception for
  *The Rings of Power* or any other individual work. Prime Video GTI and Prime
  Video ID links use Wikidata's official `/detail/` formatter; legacy Amazon
  Video ASIN records may open the Amazon France detail page, which is not
  guaranteed to launch Prime Video. The optional streaming API can supply a
  more appropriate Prime Video destination for the French catalogue.
- **Bidirectional sync:** `GET /api/v1/streaming/state` and
  `POST /api/v1/streaming/state/sync` merge these records by timestamp via
  `streaming_state`. A server administrator can also submit a verified URL
  using `POST /api/v1/streaming/links/import`; the next iOS state sync can
  import that mapping. A title's streaming-provider membership and its
  provider-specific URL are **different pieces of data**.
- **Episodes:** when the optional API supplies a uniquely matched episode
  (explicit episode number, or exact localized title within a matching
  season), HMC prefers its official `videoLink` when valid, then its `link`.
  The API's documented season/episode records do **not** require numeric
  indices: HMC must never assign them from unverified array order. Otherwise
  the series title page is used; the platform controls actual playback.
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

### Other streaming URL resolvers considered, **not integrated**

Wikidata and the optional Streaming Availability API are already used in iOS
as described above. The following integrations are **not** implemented:

| Candidate | Potential use | Important constraint | Reference |
| --- | --- | --- | --- |
| **JustWatch official API / widget / exports** | Regional offers and provider navigation | Official integrations are partner-oriented; no HMC integration; unofficial GraphQL access is not a stable public contract | [JustWatch partner docs](https://apis.justwatch.com/docs/) |
| **Watchmode** | Match TMDB/IMDb IDs to availability and provider destinations | Account and plan limitations; deep-link/episode features and usage rights vary | [Watchmode API](https://api.watchmode.com/docs), [pricing](https://api.watchmode.com/pricing) |

**Current iOS order:** local/synchronized URL → optional authenticated
Streaming Availability API (country France, exact TMDB ID) → Wikidata external
provider identifiers → service-scoped title search. Series and episodes use
their own direct provider URLs when supplied; otherwise the title/series
destination is used. App handoff relies on the service's supported universal
links and is not guaranteed. No server-side identifier resolution is required.

The free API account, if chosen, is created by the user with the upstream
provider; quotas, attribution and persistence rights depend on the current
service terms. This is **not** a mandatory prerequisite for offline catalogue
use or provider searches. Availability coverage and direct-link validity
are not guaranteed for every media item or region.

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
