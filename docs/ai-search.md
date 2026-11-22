# AI catalogue search — OpenAI, Claude, Gemini and optional semantic retrieval

Home Media Catalog works without an AI provider. Normal SQLite FTS5/title search
is never disabled, and configuration and index files are not part of JSON backups.

## Choose one AI provider (server setup)

The server supports **OpenAI**, **Anthropic Claude** and **Google Gemini**.
Both web and iOS clients use a single configured **language model** at any
given time. You do not need parallel language-model API calls. Normal
catalogue search and media browsing work without an AI provider.

In **Settings → Artificial Intelligence** (web/iOS), an administrator selects:

1. The provider (`openai`, `anthropic` or `gemini`).
2. A supported provider-specific language model ID and that provider's API key.
3. An optional monthly estimated USD budget, which is enforced by the server
   against supported model rates.

The server keeps provider keys in `DATA_DIR/ai-settings.json`
(permissions 0600) and does not return them to clients. It retains previously
configured provider keys when switching providers; clearing one selected
provider's key does not delete the others. Provider availability, model
access and pricing must be checked with the respective service.

### Retrieval depends on the selected provider

| Selected language model | Local retrieval / optional remote calls | Embeddings |
| --- | --- | --- |
| **OpenAI** | Validated filter plan, SQLite FTS5 + OpenAI semantic vector ranking + language-model relevance verification | OpenAI `text-embedding-3-small` or other supported OpenAI embedding model; persistent index |
| **Claude** | Model-generated filter plan and retrieval terms, local FTS5/metadata candidate lookup and selected Claude model relevance verification | No new remote embeddings required; existing OpenAI vectors preserved but **not used** for Claude results |
| **Gemini** | Model-generated filter plan and retrieval terms, local FTS5/metadata candidate lookup and selected Gemini model relevance verification | No new remote embeddings required; existing OpenAI vectors preserved but **not used** for Gemini results |

The **OpenAI** semantic index is stored in `DATA_DIR/ai-vectors.db`.
Changing the language provider does **not** delete or re-embed the index.
When switching back to OpenAI with the same embedding model, stored vectors
can be reused. Changing the embedding model requires indexing its distinct
vector space. The application selects **one** language model; the separate
embedding endpoint is an optional retrieval feature, not a second
conversational language model.

### Privacy and budget disclosures

A provider-based search sends the current prompt and filter context to the
selected API. Candidate validation can send metadata from the local catalogue
(including title, genre, plot/synopsis, cast/director/composer/writer credits and
ratings) to the selected provider. **Only OpenAI embedding mode** sends
catalogue metadata and episode synopses to the embeddings endpoint for
indexing; these vectors remain on the self-hosted server. Do not enable
cloud AI if that data transfer is unacceptable.

No Kodi/MariaDB password or server authentication token is intentionally
sent to the language provider as part of a search. The OpenAI index and
provider keys are outside the catalogue JSON backup; back up such files
securely if needed. Use HTTPS or a trusted VPN for remote server access.

The `/api/v1/ai/usage` dashboard estimates cost and reserves requests against
the locally configured monthly cap for listed models. Unsupported custom
model pricing is not treated as free; an unpriced model may be rejected.
**This is not a provider billing cap**, and estimates exclude taxes, other
usage, and pricing or billing-plan differences.

The sections below describe **OpenAI hybrid semantic indexing** unless
otherwise specified. Claude/Gemini users do not need to build the OpenAI
embedding index to perform the model-assisted local search.

## Indexing

On every server startup, after the catalogue is initialized, the server
automatically starts a **non-blocking, incremental reconciliation** of the
semantic index if an OpenAI API key is configured. Existing SHA-256 hashes
are compared with the current documents: only missing or changed vectors
require embeddings API calls. When the index is already current, no
embeddings API request is made. Obsolete vectors for the selected model
are removed. HTTP access for iOS/Web stays available during indexing;
an in-progress indexing task is cancelled cleanly on server shutdown.
The configured monthly API spending cap continues to apply.

Administrators can also use the **Indexer / actualiser** button in web
settings, or **Index or refresh summaries** in iOS, to request a manual
refresh. The server builds an incremental semantic index, including
individual episode plots and their parent series titles.
It reuses unchanged vectors and deletes stale records when a full pass completes.
A document now contains credited people grouped by function (up to 30 names
per function) and available ratings on the 0–10 scale. A changed credit
or rating changes its SHA-256 digest and triggers re-embedding of that work.
This also applies to existing catalogs after upgrading: the next refresh
incrementally rebuilds only documents with changed embedding text.
A semantic search can also start index refresh automatically if the corpus is
missing or has changed since the previous scan. After importing or synchronizing
Kodi, triggering a new semantic search checks for changed episodes.

The index progress appears as `indexed / total`; the first indexing pass can
be longer and consumes embedding API tokens. Changing the embeddings model
causes an index to be built for that model. The UI exposes errors such as
a missing provider or an invalid embedding model. When the provider is absent,
ordinary searches are unaffected.

## Incremental indexing, token consumption, and spending limits

After every successful Kodi synchronization or XML collection import that
changes catalogue records, the server schedules a **debounced asynchronous**
vector refresh. Metadata enrichment and source deletion also request a refresh.
Kodi/import publication does not wait for embeddings; several close-together
changes are coalesced. The existing `ai-vectors.db` hashes prevent charges for
unchanged synopses. Watched-state changes do not affect embedding text. A modified general
or personal rating now does: only that work needs a new embedding. A personal
rating edited via the API requests an asynchronous refresh.

An administrator can set **Monthly AI spending cap (USD)** in the Web or
iOS AI settings (default **$5.00/month**; **$0** blocks all paid calls).
The cap is enforced before outbound OpenAI requests — including automatic
indexing, natural-language interpretation, embedding queries, relevance
reranking, and connection tests. Requests reserve a conservative estimated
cost before transmission; when OpenAI returns usage counters, the ledger
reconciles actual input/output/cached tokens and estimated USD cost.
For network failures where OpenAI might still have billed a request, the
reservation remains an estimate instead of being silently discarded.
Concurrent calls reserve against a single SQLite month ledger.

The dashboard, via admin-only `GET /api/v1/ai/usage`, displays the current
UTC calendar month, cost estimate, budget remaining, and request/token totals
for indexing, searching, and connection tests. The `monthly_budget_usd`
field is saved with the other settings in `PUT /api/v1/ai/settings` and
is preserved when older clients omit it. A cap is an **application-side
safety control, not a substitute for the provider's billing limits**:
pricing is based on public standard API rates, and the estimate excludes
taxes, different service tiers and external account activity. Custom
model identifiers without known prices are rejected before billable calls
rather than bypassing the cap. The ledger is held locally in
`DATA_DIR/ai-usage.db` (mode 0600) without storing access keys or prompts.

## API (authenticated)

- `GET /api/v1/ai/status`: configured state, model IDs, index status.
- `GET /api/v1/ai/settings`: same fields, administrator only.
- `PUT /api/v1/ai/settings`: administrator only. Example for OpenAI:
  `{"provider":"openai","model":"gpt-4o-mini","embedding_model":"text-embedding-3-small","api_key":"…"}`.
  For Claude or Gemini, select the provider and its language model; the
  embedding model is not required for those modes.
  Omit the key to preserve it or use `clear_api_key:true` to disable AI.
- `POST /api/v1/ai/connection/test`: administrator-only live provider test of
  the selected language model; OpenAI hybrid mode can separately test the
  embeddings model. Tests can incur billable requests. Returns safe status
  objects and error codes such as
  `invalid_key`, `model_unavailable`, or `quota_or_rate_limit`. Never returns
  the token or raw upstream error body.
- `POST /api/v1/ai/index/refresh`: administrator-only incremental full scan.
- `POST /api/v1/ai/search`: `{"prompt":"unwatched sci-fi involving a time loop"}`.
  The response includes catalogue media, structured `plan`, ranked mode,
  matched episodes (`episode_matches`), index progress, and optional note.

## Conversational refinement and result lists

On iOS, follow-up prompts reuse the current search session. The client sends
the previously returned `plan` as `previous_plan` and, when the server
advertises `ai_refine_results`, up to 60 currently displayed media IDs as
`previous_ids`. The server reads candidate metadata from its own database;
the client does not supply trusted titles or arbitrary search results.

The model distinguishes three operations:

| Operation | Example | Result |
| --- | --- | --- |
| `filter` | "Retire Valérian de la liste" | Keeps the other displayed titles and removes matching items. |
| `expand` | "Ajoutes-en d'autres" | Keeps the current selection and searches for additional titles using the previous search plan; duplicate IDs are excluded. |
| `search` | "Cherche plutôt des films de Miyazaki" | Runs a fresh search, with conversational criteria interpreted by the server. |

Example follow-up request:

```json
{
  "prompt": "Retire Valérian de la liste",
  "previous_plan": {
    "kind": "movie",
    "watch_status": null,
    "genre": null,
    "format": null,
    "year_min": null,
    "year_max": null,
    "rating_min": null,
    "keywords": "",
    "semantic_query": "science-fiction",
    "assistant_message": ""
  },
  "previous_ids": ["existing-media-id-1", "existing-media-id-2"]
}
```

The response carries `items`, `plan`, `mode` (`refine`, `expand`,
`hybrid` or `filters`), `assistant_message` and relevant search metadata.
The IDs in the example are placeholders, not valid catalogue IDs.

The iOS **Undo last refinement** action restores the previous result snapshot
and conversation state without sending another request. **New search** clears
the current results, history and previous criteria. A failed network request
leaves the prior results intact.

The interface displays at most 60 results. Expanding a full list cannot
display more than 60 simultaneously; it is not unlimited pagination.
**Add all AI results to a list** adds the currently displayed IDs only, to
an existing or new list, using the server's `bulk_list_add` capability.
This action requires administrator access. When a server does not advertise
`ai_refine_results`, the iOS client omits `previous_ids` and falls back to
the earlier plan-only refinement, rather than sending an unsupported field.
The discovery endpoint advertises optional capabilities, independently of the
mobile data-contract version.

## Ranking and episode results

A structured plan is validated server-side; the model never supplies executable
SQL. Named actors, directors, composers and writers are matched by their
**actual credit category** using a dedicated indexed SQLite lookup, not
only by semantic similarity. The extracted `person_name` and
`person_role` are optional and can be combined with plot constraints.
Likewise, `rating_min` filters the general rating and
`user_rating_min` filters the personal rating numerically in SQLite;
missing ratings never satisfy minimum-note filters. Semantic cosine similarity and SQLite FTS5 title/plot matches are blended,
then exact year, genre, format, rating and watch constraints are applied through
the existing catalogue database. Only titles already imported into the server
can be returned. A matching episode is shown as a result under its parent show,
with season/episode number and a synopsis excerpt; the original episode ID is
retained for navigation.

The semantic score is approximate and reflects the quality of imported
synopses and credited metadata. Search scoring is run outside the FastAPI
event loop. SQLite `ai-vectors.db` remains the authoritative index:
for fewer than 2,000 eligible vectors, a cached NumPy matrix performs exact
cosine search. From 2,000 vectors, USearch provides an in-memory **HNSW ANN**
index (cosine, float32). The graph is lazily recreated from SQLite when
the indexed hashes or scoped document selection change, or after a server restart;
it need not be backed up. If NumPy/USearch is unavailable, the code falls
back to exact cosine ranking. HNSW is approximate and can miss some
low-ranked neighbors; role/rating constraints remain exact SQLite predicates. A lexical title/name request uses FTS5; plot/theme queries require an
indexed embedding. Initial results show at most 60 works; the index is refreshed
incrementally. The server limits AI requests to 10/minute per client IP and
3 concurrent upstream searches.

A missing or broken provider is surfaced to the user: the server never silently
pretends that a semantic result was computed. FTS5 and the standard catalogue
remain available without an AI API key.


## Personalized AI recommendations for streaming

The iOS Streaming tab has a sparkles action that opens a dedicated Streaming
AI search screen. This uses the existing AI provider, selected model, server
budget, rate limit and credentials. Compatible servers advertise the
ai_streaming_recommendations capability in GET /api/v1/discovery.

Authenticated endpoint: POST /api/v1/ai/streaming/search.
For example, the body may be:
{"prompt":"Recommend Korean shows I have not watched","kind":"tv","services":["netflix","rakuten_viki"],"region":"FR","excluded_ids":[]}.
An optional previous_prompt provides follow-up search context.

The server combines catalogue watched statuses and ratings with the independent
personal:movie:{tmdb_id} and personal:tv:{tmdb_id} streaming journal entries.
Personal overrides take precedence. Rated, fully watched, and partially watched
works are excluded from unwatched recommendations. iOS also excludes pending
local-first edits before showing the results.

A small set of liked and disliked examples drives TMDB genre affinity, matching
with TMDB subscription-scoped Discover and recommendations for favorite titles.
Every candidate ultimately shown has been checked against the selected country's
flatrate watch-provider offers. The configured language model verifies that
shortlisted candidate synopses genuinely match the prompt and preference history.
Public TMDB responses and provider membership are cached briefly. The service
does not index the entire external streaming catalogue with embeddings.

Results use TMDB movie/TV identities (not catalogue UUIDs) and include
service_keys, relevance_reasons, profile_count and an explanatory message.
Opening a result uses the existing iOS streaming detail and player links.

Limitations and privacy: no watched state recorded in HMC is not proof a title
was never viewed in a third-party app. Streaming availability can change and
does not guarantee playback. This first version uses one shared catalogue
preference profile, not multiple independent family profiles. A provider-based
search sends selected historical ratings, titles and metadata to the active
AI provider; credentials remain server-side.

## iOS entry point

Ordinary title and metadata search uses Apple's native SwiftUI `searchable`
modifier **on the existing catalogue grid/list**, not an independent Search
tab. Its query updates the catalogue's standard filters; the same results
are displayed in the main catalogue. On iOS 26 the system chooses its
standard bottom-aligned search presentation on iPhone.

The **sparkles** button in the catalogue's **top toolbar** opens a dedicated
AI search page with a standard SwiftUI `NavigationLink` (no popup).
That page owns its native multiline prompt and Search action. Prompts are
interpreted, matched with metadata filters and semantic similarity, and
optionally refined in subsequent turns. The AI session is owned by the
connected catalogue tab and is preserved when navigating to media details
and back. Matched episodes open by their exact ID.

A media detail now first retrieves the local server resource directly with
`GET /api/v1/media/{mediaID}`; the former automatic TMDB enrichment could
delay the first display for seconds. Users can still explicitly refresh
TMDB metadata from the detail view.

To verify credentials on Web or iOS, save the selected provider,
language model and API key, then run the available connection test.
OpenAI semantic mode additionally checks its configured embeddings model.
A valid key does not guarantee availability, quota or model access.

## Search precision and verified relevance

A vector embedding is only a retrieval heuristic, not proof of a creator's
style. Semantic searches now apply a stricter minimum similarity and ask the
configured language model to validate shortlisted film/episode descriptions
against the entire original user request. This relevance validation transmits
the candidate titles, genres and synopsis excerpts to OpenAI at search time,
in addition to the indexing-time embeddings requests. This helps reject false matches
(such as a generic fantasy comedy for a Miyazaki-inspired animation request).
The response exposes `relevance_reasons` with grounded brief explanations for
retained media IDs. Weak candidates are omitted; no result is preferable to a
misleading match. The additional relevance pass makes a second language-model
API call and has corresponding cost. Structured catalogue constraints such
as watched status remain enforced by SQLite.
