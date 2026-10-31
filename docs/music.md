# Music Library

The catalogue holds a music library beside movies and series. It is fed by
the same kinds of sources as the video library:

| Source | How it is refreshed | Content |
| --- | --- | --- |
| **Kodi** (existing `kodi` source) | `AudioLibrary` JSON-RPC on a configured Kodi instance, with a fallback to the **MyMusic** MariaDB schema | Artists, albums, songs — playable files |
| **Discogs** (`discogs`) | Upload the CSV from *Discogs › Collection › Export* | Owned releases (CD, vinyl, cassette…) |
| **CLZ Music** (`clz_music`) | Upload the XML export of CLZ Music / Music Collector | Owned releases and their track lists |
| **CSV / JSON list** (`music_csv`) | Upload any CSV/TSV or JSON array | Your own list of CDs, vinyls… |

Physical collections describe owned media: albums and optional track lists
are catalogue entries, not playable files. The model already distinguishes
`playable` tracks so a future streaming connector (Qobuz, Deezer, Spotify…)
only needs to return the same snapshot structure with playable tracks.

## Kodi

Kodi stores music in a database separate from videos (`MyMusicNN` next to
`MyVideosNN`). No Kodi add-on is needed — the `AudioLibrary` namespace exists
since Kodi 12 — but the music sources must have been scanned in Kodi.

* **Manual refresh**: `POST /api/v1/sources/{id}/music/sync` (add
  `?mariadb_only=true` to read MyMusic without contacting Kodi).
* **Automatic**: set `config.music_enabled=true` on the Kodi source. Each
  video synchronisation then also refreshes the music library. A music error
  never fails or rolls back the video import; it is reported as
  `result.music_error` on the job.
* **MariaDB fallback**: the same host/username/password as the MyVideos
  fallback are used. The newest visible `MyMusicNN` schema is selected unless
  `config.music_database` names one. The account needs `SELECT` on
  `artistview`, `albumview`, `songview`, `album_artist` and `art`.
* Kodi versions differ in the properties they accept. When a recent property
  (`sortname`, `releasetype`, `bitrate`…) is rejected, the request is retried
  once with a conservative property list.

## Physical collections

Create a source of type `discogs`, `clz_music` or `music_csv`, then upload the
export with `POST /api/v1/imports/music/{type}` (multipart: `source_id`,
`file`, 50 MiB max). Each upload replaces that source's previous snapshot.

Generic lists need at least an **artist** and an **album/title** column.
Recognised headers (English or French, case and accents ignored): year /
année, format / support, label, catalog number / référence, barcode /
code-barres, genre, country / pays, notes, rating, media condition / état du
disque, sleeve condition / état de la pochette, date added, purchase date /
date d'achat, discs, tracks / pistes (`A1 Title | A2 Title | B1 Title`).

Carriers are normalised to `Digital`, `CD`, `SACD`, `Vinyl`, `Cassette`,
`MiniDisc`, `DVD-Audio`, `Blu-ray Audio`, `Reel-to-reel`, `Streaming` or
`Other`; the original wording is kept in `format_detail` (e.g. `2xLP, 180g`).
Ratings out of 5 (Discogs) are stored on Kodi's 10-point scale.

## Matching

* **Artists** match on MusicBrainz ID, otherwise on the folded name (case,
  accents, `&`/`and`, a leading *The*, Discogs `(2)` suffixes are ignored).
* **Albums** gather *editions* — the FLAC rip from Kodi, the CD and the vinyl
  from a collection — on MusicBrainz release-group ID, otherwise on album
  artist + title with edition qualifiers removed (`(Remastered 2011)`,
  `[Deluxe Edition]`…). Kodi metadata wins, collections fill gaps; formats
  and genres are combined.
* **Tracks** are never merged across sources.

Album and artist IDs remain stable across resynchronisations as long as one
source still contains them.

## API

| Endpoint | Purpose |
| --- | --- |
| `GET /api/v1/music/status` | Counts per kind, per format and per source, plus `revision` |
| `GET /api/v1/music/facets` | Genres, formats and years for filters |
| `GET /api/v1/music/search?q=` | Artists, albums and tracks matching a query |
| `GET /api/v1/music/artists` · `/{id}` | Artists (`q`, `genre`, `offset`, `limit`); detail with albums and appearances |
| `GET /api/v1/music/albums` · `/{id}` | Albums (`q`, `artist_id`, `genre`, `format`, `source_type`, `playable`, `physical`, `year`, `sort`, `order`); detail with editions and tracks |
| `GET /api/v1/music/tracks` · `/{id}` | Tracks (`q`, `album_id`, `artist_id`, `genre`, `playable`, `sort`, `order`) |
| `GET /api/v1/music/export/{artists,albums,tracks}?after=&limit=` | Keyset pages (≤ 5000) for native offline mirrors |
| `GET /api/v1/music/{albums,artists}/{id}/artwork/{thumb,fanart}` | Cached cover art, via Kodi's `/image/` for embedded covers |
| `POST /api/v1/players/{player_id}/music-stream` | Short-lived byte-range URL for one song (`{"track_id"}`) |
| `POST /api/v1/players/{player_id}/music/play` | Play a song, album or artist on Kodi |

Native clients mirror the library with `music/export`: they request pages
until `next` is `null`, and restart when `revision` changes between pages
(a synchronisation was published meanwhile). `music/status.revision` tells a
client whether its mirror is stale without downloading anything else.

Searches use dedicated FTS5 indexes (artist, album + label + catalog number +
barcode, title + artist + album). Paged listings use indexed `ORDER BY`; the
export uses integer keyset cursors, so 500 000 tracks never need to fit in a
single response.

## Storage

Music tables live in the same SQLite file (`music_artists`,
`music_artist_sources`, `music_albums`, `music_album_artists`,
`music_album_editions`, `music_tracks` and three FTS tables). They are
additive: `PRAGMA user_version` is unchanged and no migration is needed.
JSON backups include them; older backups without music remain importable.
