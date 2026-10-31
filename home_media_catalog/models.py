"""Typed public API contract; secrets are never included in response models."""

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class Kind(StrEnum):
    MOVIE = "movie"
    TVSHOW = "tvshow"
    EPISODE = "episode"


class WatchStatus(StrEnum):
    WATCHED = "watched"
    UNWATCHED = "unwatched"
    IN_PROGRESS = "in_progress"
    UNKNOWN = "unknown"


class SortField(StrEnum):
    TITLE = "title"
    DATE_ADDED = "date_added"
    YEAR = "year"
    RATING = "rating"
    USER_RATING = "user_rating"
    LAST_PLAYED = "last_played"
    EPISODE = "episode"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApiError(BaseModel):
    code: str
    message: str
    details: list[dict[str, Any]] | None = None


class ErrorResponse(BaseModel):
    error: ApiError


class SourceConfig(StrictModel):
    host: str = Field(default="", max_length=253)
    port: int = Field(default=3306, ge=1, le=65535)
    username: str = Field(default="", max_length=128)
    password: SecretStr | None = None
    database: str = Field(default="", max_length=128)
    tls: bool = False
    ca_file: str | None = None
    # Optional, licensed My Movies Remote Server API (not XML or cloud access).
    remote_host: str = Field(default="", max_length=253)
    remote_port: int = Field(default=51414, ge=1, le=65535)
    remote_api_key: SecretStr | None = None
    remote_enabled: bool = False
    mariadb_write_enabled: bool = False
    # Kodi music library (AudioLibrary JSON-RPC, MyMusic MariaDB fallback).
    music_enabled: bool = False
    music_database: str = Field(default="", max_length=128)

    @field_validator("database", "music_database")
    @classmethod
    def safe_database(cls, value: str) -> str:
        if value and not all(c.isascii() and (c.isalnum() or c == "_") for c in value):
            raise ValueError("Use an alphanumeric database name, with optional underscores.")
        return value


class SourceInput(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    type: Literal["kodi", "clz", "mymovies", "filmotech", "music_csv", "discogs", "clz_music"]
    config: SourceConfig = Field(default_factory=SourceConfig)
    enabled: bool = True


class MariaDBConnectionInput(StrictModel):
    config: SourceConfig
    source_id: str | None = None


class KodiConnectionInput(StrictModel):
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=8080, ge=1, le=65535)
    websocket_port: int = Field(default=9090, ge=1, le=65535)
    username: str = Field(default="", max_length=128)
    password: SecretStr | None = None
    tls: bool = False
    player_id: str | None = None


class MariaDBDatabaseOption(BaseModel):
    name: str
    version: int


class MariaDBDatabasesResult(BaseModel):
    databases: list[MariaDBDatabaseOption]
    automatic_database: str | None = None
    selected_database: str | None = None


class ConnectionResult(BaseModel):
    success: Literal[True] = True
    kind: Literal["mariadb", "kodi", "tmdb"]
    message: str
    database: str | None = None
    available_media: list[str] = Field(default_factory=list)
    version: str | None = None
    playback_allowed: bool | None = None


class SourceView(BaseModel):
    id: str
    name: str
    type: Literal["kodi", "clz", "mymovies", "filmotech", "music_csv", "discogs", "clz_music"]
    enabled: bool
    config: dict[str, Any]
    last_sync: str | None = None
    last_error: str | None = None
    item_count: int = 0
    music_count: int = 0


class ArtworkLinks(BaseModel):
    poster: str | None = None
    fanart: str | None = None


class MediaSource(BaseModel):
    id: str
    name: str
    type: Literal["kodi", "clz", "mymovies", "filmotech"]
    type_count: int = 1


class MediaSummary(BaseModel):
    id: str
    kind: Kind
    title: str
    original_title: str | None = None
    year: int | None = None
    plot: str | None = None
    runtime_minutes: int | None = None
    rating: float | None = None
    user_rating: float | None = None
    date_added: str | None = None
    last_played: str | None = None
    watch_status: WatchStatus
    episode_count: int | None = None
    watched_episode_count: int | None = None
    play_count: int | None = None
    resume_seconds: float = 0
    total_seconds: float = 0
    playback_updated_at: str | None = None
    series_resume_progress: float | None = None
    parent_id: str | None = None
    season: int | None = None
    episode: int | None = None
    genres: list[str] = Field(default_factory=list)
    formats: list[str] = Field(default_factory=list)
    collections: list[str] = Field(default_factory=list)
    source_types: list[str] = Field(default_factory=list)
    media_sources: list[MediaSource] = Field(default_factory=list)
    # Filled only for a local cast/crew search; omitted roles mean no known
    # character/job, not that the person has no credit for the title.
    matched_roles: list[str] = Field(default_factory=list)
    artwork: ArtworkLinks


class EditionView(BaseModel):
    id: str
    source_id: str
    source_name: str
    source_type: str
    external_key: str
    identifiers: dict[str, str]
    metadata: dict[str, Any]
    watched: bool | None = None
    play_count: int | None = None
    resume_seconds: float = 0
    total_seconds: float = 0
    date_added: str | None = None

    last_played: str | None = None
    playback_updated_at: str | None = None


class PlaybackResumeInput(StrictModel):
    edition_id: str = Field(min_length=1, max_length=128)
    position_seconds: float = Field(ge=0, le=10000000, allow_inf_nan=False)
    total_seconds: float = Field(ge=0, le=10000000, allow_inf_nan=False)
    updated_at: str = Field(min_length=20, max_length=40)


class CastMember(BaseModel):
    name: str
    role: str | None = None
    order: int | None = None
    category: str = "cast"
    imdb_id: str | None = None
    tmdb_id: str | None = None
    links: dict[str, str] = Field(default_factory=dict)
    thumbnail: str | None = None


class PersonalInput(StrictModel):
    user_rating: float | None = Field(default=None, ge=0, le=10)
    watched: bool | None = None


class EpisodeWatchInput(StrictModel):
    watched: bool
    season: int | None = Field(default=None, ge=0, le=10000)
    up_to_episode: int | None = Field(default=None, ge=0, le=100000)


class EpisodeWatchResult(BaseModel):
    total: int
    updated: int
    watched: bool
    season: int | None = None
    up_to_episode: int | None = None


class PersonalView(BaseModel):
    user_rating: float | None = None
    watched: bool | None = None


class WritebackResult(BaseModel):
    method: Literal["jsonrpc", "mariadb", "mymovies"] | None = None
    attempts: list[str] = Field(default_factory=list)
    source_id: str
    source_name: str
    source_type: str
    status: Literal["updated", "unavailable", "failed"]
    message: str | None = None


class SourceWritebackState(BaseModel):
    media_id: str | None = None
    media_title: str | None = None
    source_id: str
    source_name: str
    source_type: str
    icon: str
    status: Literal["updated", "pending", "failed", "unavailable"]
    method: str | None = None
    updated_at: str
    pending_fields: list[str] = Field(default_factory=list)
    message: str | None = None


class PendingWriteback(BaseModel):
    id: str
    media_id: str
    media_title: str
    source_id: str
    source_name: str
    source_type: str
    icon: str
    changes: dict[str, float | bool]
    created_at: str
    updated_at: str
    retry_count: int
    last_error: str | None = None


class WritebackStatus(BaseModel):
    pending_count: int
    pending: list[PendingWriteback]
    recent: list[SourceWritebackState]


class ReplayResult(BaseModel):
    pending_id: str
    media_id: str
    result: WritebackResult


class ReplayResponse(BaseModel):
    results: list[ReplayResult]
    pending_count: int


class PlaybackLocationInput(StrictModel):
    """Share/provider descriptor; never send SMB, FTP or OAuth credentials."""

    name: str = Field(min_length=1, max_length=120)
    kind: Literal[
        "smb",
        "nfs",
        "ftp",
        "onedrive",
        "google_drive",
        "dropbox",
        "box",
        "pcloud",
        "icloud",
        "upnp",
        "sftp",
        "http",
        "local",
    ]
    target: str = Field(default="", max_length=2048)
    host: str = Field(default="", max_length=255)
    hostname: str = Field(default="", max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    share: str = Field(default="", max_length=1024)
    username: str = Field(default="", max_length=255)
    workgroup: str = Field(default="", max_length=120)
    password: SecretStr | None = Field(default=None, exclude=True)

    @field_validator("name")
    @classmethod
    def clean_name(cls, name: str) -> str:
        name = name.strip()
        if not name:
            raise ValueError("Name must not be blank.")
        return name

    @field_validator("hostname")
    @classmethod
    def clean_hostname(cls, hostname: str) -> str:
        # This is an exact Kodi-path hostname alias, never a network target.
        hostname = hostname.strip().rstrip(".")
        if any(char.isspace() or char in "/\\:@?#" for char in hostname):
            raise ValueError("Hostname must be a name, not a URL, address or path.")
        return hostname

    @field_validator("target")
    @classmethod
    def no_embedded_credentials(cls, target: str) -> str:
        from urllib.parse import urlsplit

        candidate = urlsplit(target.strip())
        if candidate.username is not None or candidate.password is not None:
            raise ValueError("Credentials must not be embedded in the location.")
        return target.strip()


class PlaybackLocationView(StrictModel):
    id: str
    name: str
    kind: str
    target: str
    host: str = ""
    hostname: str = ""
    port: int | None = None
    share: str = ""
    username: str = ""
    workgroup: str = ""
    has_password: bool = False
    created_at: str
    updated_at: str


class ListInput(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)

    @field_validator("name")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("List name must not be blank.")
        return value.strip()


class ListView(BaseModel):
    id: str
    thumbnail: str | None = None
    name: str
    description: str
    revision: int
    item_count: int
    watched_count: int = 0
    created_at: str


class ListItemInput(StrictModel):
    media_id: str


class ListBulkAddInput(StrictModel):
    media_ids: list[str] = Field(min_length=1, max_length=200)


class ListBulkAddResult(StrictModel):
    added: int
    already_present: int
    total_requested: int


class ListOrderInput(StrictModel):
    media_ids: list[str] = Field(max_length=10000)
    revision: int = Field(ge=0)


class MediaDetail(MediaSummary):
    identifiers: dict[str, list[str]]
    editions: list[EditionView]
    cast: list[CastMember] = Field(default_factory=list)
    people: list[CastMember] = Field(default_factory=list)
    trailers: list[str] = Field(default_factory=list)
    personal: PersonalView = Field(default_factory=PersonalView)
    writeback: list[WritebackResult] = Field(default_factory=list)
    writeback_state: list[SourceWritebackState] = Field(default_factory=list)
    lists: list[ListView] = Field(default_factory=list)
    tmdb_available: bool = False
    season_posters_cached: bool = False
    enriched_at: str | None = None


class SeasonSummary(BaseModel):
    season: int
    episode_count: int
    watched_count: int
    poster: str | None = None


class MediaPage(BaseModel):
    items: list[MediaSummary]
    total: int
    limit: int
    offset: int
    has_more: bool


class ListPage(MediaPage):
    revision: int


class Facets(BaseModel):
    genres: list[str]
    formats: list[str]
    collections: list[str]
    years: list[int]
    sources: list[SourceView]


class JobView(BaseModel):
    id: str
    source_id: str
    status: Literal["pending", "running", "completed", "failed"]
    operation: Literal[
        "kodi_sync", "kodi_scan", "kodi_clean", "collection_import", "music_sync", "music_import"
    ] = "kodi_sync"
    created_at: str
    finished_at: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    progress_phase: str | None = None
    progress_current: int = 0
    progress_total: int = 0
    overall_percent: int = Field(default=0, ge=0, le=100)


class PlayerInput(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    source_id: str
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=8080, ge=1, le=65535)
    username: str = Field(default="", max_length=128)
    password: SecretStr | None = None
    tls: bool = False
    websocket_port: int = Field(default=9090, ge=1, le=65535)
    mac: str | None = Field(default=None, max_length=32)

    @field_validator("mac")
    @classmethod
    def validate_mac(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().replace("-", "").replace(":", "").lower()
        if normalized and (len(normalized) != 12 or any(c not in "0123456789abcdef" for c in normalized)):
            raise ValueError("Enter a 12-digit hexadecimal MAC address.")
        return ":".join(normalized[i:i + 2] for i in range(0, len(normalized), 2))


class PlayerView(BaseModel):
    id: str
    name: str
    source_id: str
    host: str
    port: int
    username: str
    tls: bool
    has_password: bool
    websocket_port: int = 9090
    mac: str = ""


class PlayInput(StrictModel):
    media_id: str
    edition_id: str | None = None
    resume: bool = True


class PlayResult(BaseModel):
    player_id: str
    media_id: str
    edition_id: str
    status: Literal["started"] = "started"


class PairingRequest(StrictModel):
    role: Literal["admin", "read"] = "admin"


class PairingRedeem(StrictModel):
    code: str = Field(pattern=r"^[0-9]{8}$")
    device_name: str = Field(min_length=1, max_length=80)


class PairingResult(BaseModel):
    code: str
    expires_in: int
    role: Literal["admin", "read"]


class SessionInput(StrictModel):
    token: SecretStr


class SessionView(BaseModel):
    role: Literal["admin", "read"]
    csrf_token: str | None = None
    access_method: Literal["session", "bearer", "ingress", "local"] = "session"


class MobileCompatibility(BaseModel):
    """Compatibility of mobile DTOs, independent of server release numbers."""

    min_contract: int = Field(ge=1)
    max_contract: int = Field(ge=1)


class CatalogueStatus(BaseModel):
    version: str
    data_model_version: int
    api_version: str
    mobile_compatibility: MobileCompatibility
    works: int
    editions: int
    counts: dict[str, int]
    sources: list[SourceView]
    tmdb_available: bool = False


# --------------------------------------------------------------------- music


class MusicArtist(BaseModel):
    id: str
    name: str
    sort_name: str = ""
    mbid: str | None = None
    genres: list[str] = Field(default_factory=list)
    album_count: int = 0
    track_count: int = 0
    thumb: str | None = None
    fanart: str | None = None


class MusicAlbum(BaseModel):
    id: str
    title: str
    artist: str = ""
    artist_id: str | None = None
    artist_ids: list[str] | None = None
    year: int | None = None
    release_type: str | None = None
    compilation: bool = False
    label: str | None = None
    genres: list[str] = Field(default_factory=list)
    rating: float | None = None
    user_rating: float | None = None
    play_count: int | None = None
    last_played: str | None = None
    date_added: str | None = None
    formats: list[str] = Field(default_factory=list)
    source_types: list[str] = Field(default_factory=list)
    track_count: int = 0
    duration_seconds: float = 0
    playable: bool = False
    owned_physical: bool = False
    thumb: str | None = None
    fanart: str | None = None
    updated_at: str | None = None


class MusicTrack(BaseModel):
    id: str
    title: str
    artist: str = ""
    artist_id: str | None = None
    album: str = ""
    album_id: str | None = None
    edition_id: str | None = None
    source_id: str
    disc: int | None = None
    track: int | None = None
    position: str | None = None
    duration_seconds: float | None = None
    year: int | None = None
    genres: list[str] = Field(default_factory=list)
    playable: bool = False
    format: str = "Digital"
    file: str | None = None
    rating: float | None = None
    user_rating: float | None = None
    play_count: int | None = None
    last_played: str | None = None
    date_added: str | None = None
    audio: dict[str, Any] = Field(default_factory=dict)


class MusicEdition(BaseModel):
    id: str
    source_id: str
    source_name: str
    source_type: str
    format: str
    format_detail: str | None = None
    label: str | None = None
    catalog_number: str | None = None
    barcode: str | None = None
    country: str | None = None
    discogs_id: str | None = None
    mbid: str | None = None
    release_group_mbid: str | None = None
    release_date: str | None = None
    year: int | None = None
    disc_count: int | None = None
    media_condition: str | None = None
    sleeve_condition: str | None = None
    notes: str | None = None
    purchase_date: str | None = None
    kodi_id: int | None = None


class MusicAlbumDetail(MusicAlbum):
    artists: list[dict[str, str]] = Field(default_factory=list)
    editions: list[MusicEdition] = Field(default_factory=list)
    tracks: list[MusicTrack] = Field(default_factory=list)


class MusicArtistDetail(MusicArtist):
    biography: str | None = None
    albums: list[MusicAlbum] = Field(default_factory=list)
    appears_on: list[MusicAlbum] = Field(default_factory=list)


class MusicArtistPage(BaseModel):
    items: list[MusicArtist]
    total: int
    offset: int
    limit: int


class MusicAlbumPage(BaseModel):
    items: list[MusicAlbum]
    total: int
    offset: int
    limit: int


class MusicTrackPage(BaseModel):
    items: list[MusicTrack]
    total: int
    offset: int
    limit: int


class MusicSearchResult(BaseModel):
    artists: list[MusicArtist]
    albums: list[MusicAlbum]
    tracks: list[MusicTrack]


class MusicStatus(BaseModel):
    artists: int
    albums: int
    tracks: int
    playable_tracks: int
    physical_albums: int
    formats: dict[str, int]
    sources: list[dict[str, Any]]
    revision: int


class MusicFacets(BaseModel):
    genres: list[str]
    formats: list[str]
    years: list[int]


class MusicExportPage(BaseModel):
    items: list[dict[str, Any]]
    next: int | None = None
    total: int
    revision: int


class MusicPlayInput(StrictModel):
    track_id: str | None = None
    album_id: str | None = None
    artist_id: str | None = None
    shuffle: bool = False
