"""Connector boundary for music libraries.

Video connectors produce ``Record`` rows; music connectors produce a
``MusicSnapshot`` of artists, albums (one "edition" per source item) and
tracks. Every music source — Kodi's AudioLibrary, a MyMusic MariaDB schema,
a physical CD/vinyl collection export or, later, a streaming service —
returns the same structure, so matching and publication are shared.

``playable`` marks tracks a client can actually play (a Kodi file or, in the
future, a streaming reference). Physical collections describe owned media:
their albums and optional track lists are catalogue entries, not files.
"""

from dataclasses import dataclass, field
from typing import Any

# Canonical physical/digital carrier names shared by the server and iOS.
MUSIC_FORMATS = (
    "Digital",
    "CD",
    "SACD",
    "Vinyl",
    "Cassette",
    "MiniDisc",
    "DVD-Audio",
    "Blu-ray Audio",
    "Reel-to-reel",
    "Streaming",
    "Other",
)


@dataclass
class MusicArtistRecord:
    external_key: str
    name: str
    sort_name: str | None = None
    mbid: str | None = None
    genres: list[str] = field(default_factory=list)
    biography: str | None = None
    thumb: list[str] = field(default_factory=list)
    fanart: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MusicAlbumRecord:
    external_key: str
    title: str
    artists: list[str] = field(default_factory=list)
    artist_keys: list[str] = field(default_factory=list)
    display_artist: str | None = None
    year: int | None = None
    release_date: str | None = None
    genres: list[str] = field(default_factory=list)
    mbid: str | None = None
    release_group_mbid: str | None = None
    release_type: str | None = None  # album, ep, single, compilation, live…
    compilation: bool = False
    label: str | None = None
    rating: float | None = None
    user_rating: float | None = None
    play_count: int | None = None
    last_played: str | None = None
    date_added: str | None = None
    thumb: list[str] = field(default_factory=list)
    fanart: list[str] = field(default_factory=list)
    # Carrier of this source item: "Digital" for Kodi files, "CD"/"Vinyl"…
    # for a physical collection. ``format_detail`` keeps the source wording
    # ("2×LP, 180 g, Gatefold").
    format: str = "Digital"
    format_detail: str | None = None
    disc_count: int | None = None
    catalog_number: str | None = None
    barcode: str | None = None
    country: str | None = None
    discogs_id: str | None = None
    media_condition: str | None = None
    sleeve_condition: str | None = None
    notes: str | None = None
    purchase_date: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MusicTrackRecord:
    external_key: str
    title: str
    album_key: str | None = None
    artists: list[str] = field(default_factory=list)
    display_artist: str | None = None
    track: int | None = None
    disc: int | None = None
    position: str | None = None  # vinyl side positions such as "A1"
    duration_seconds: float | None = None
    year: int | None = None
    genres: list[str] = field(default_factory=list)
    file: str | None = None
    playable: bool = False
    mbid: str | None = None
    rating: float | None = None
    user_rating: float | None = None
    play_count: int | None = None
    last_played: str | None = None
    date_added: str | None = None
    # Technical properties when the source exposes them.
    codec: str | None = None
    bitrate: int | None = None  # kbit/s
    sample_rate: int | None = None  # Hz
    channels: int | None = None
    bits_per_sample: int | None = None
    replay_gain: dict[str, float] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MusicSnapshot:
    artists: list[MusicArtistRecord] = field(default_factory=list)
    albums: list[MusicAlbumRecord] = field(default_factory=list)
    tracks: list[MusicTrackRecord] = field(default_factory=list)
    report: dict[str, Any] = field(default_factory=dict)


def canonical_music_format(value: str | None) -> str:
    """Map free-form carrier descriptions (Discogs, CLZ, CSV) to MUSIC_FORMATS."""
    text = str(value or "").strip()
    folded = text.casefold()
    if not folded:
        return "Other"
    checks = (
        ("SACD", ("sacd", "super audio")),
        ("Blu-ray Audio", ("blu-ray audio", "bd-a", "pure audio")),
        ("DVD-Audio", ("dvd-a", "dvd-audio", "dvd audio")),
        ("Vinyl", ("vinyl", "vinyle", "lp", '12"', '10"', '7"', "45 rpm", "flexi-disc", "acetate")),
        ("CD", ("cd", "cdr", "cd-r", "compact disc", "hdcd", "shm-cd")),
        ("Cassette", ("cassette", "cass", "k7", "tape")),
        ("MiniDisc", ("minidisc", "mini disc", "md")),
        ("Reel-to-reel", ("reel",)),
        ("Streaming", ("stream", "spotify", "deezer", "qobuz", "tidal", "apple music")),
        ("Digital", ("file", "digital", "flac", "mp3", "aac", "alac", "wav", "numérique")),
    )
    tokens = folded.replace(",", " ").replace(";", " ").replace("×", "x").split()
    for canonical, needles in checks:
        for needle in needles:
            if " " in needle or '"' in needle:
                if needle.strip() in folded:
                    return canonical
            elif any(token == needle or token.endswith("x" + needle) for token in tokens):
                return canonical
    for canonical in MUSIC_FORMATS:
        if folded == canonical.casefold():
            return canonical
    return "Other"
