"""Optional exact-ID fallback for music artwork when Kodi HTTP is unavailable.

Use only MusicBrainz release-group UUIDs stored by the catalogue; no artist
or title fuzzy searches, no user-supplied destination URLs.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit
from uuid import UUID

import httpx

_MAX_BYTES = 8 * 1024 * 1024
_ALLOWED_HOSTS = {"coverartarchive.org", "archive.org"}
_UUID_PATTERN = re.compile(r"^[0-9a-fA-F-]{36}$")


def _trusted(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    return (
        parts.scheme == "https"
        and (host in _ALLOWED_HOSTS or host.endswith(".archive.org"))
        and parts.username is None and parts.password is None
        and parts.port in (None, 443)
        and not parts.fragment
    )


async def exact_musicbrainz_cover(release_group: str | None) -> bytes | None:
    if not release_group or not _UUID_PATTERN.fullmatch(release_group):
        return None
    try:
        mbid = str(UUID(release_group))
    except (TypeError, ValueError):
        return None
    url = f"https://coverartarchive.org/release-group/{mbid}/front-500"
    timeout = httpx.Timeout(connect=3, read=7, write=3, pool=3)
    try:
        async with httpx.AsyncClient(
            timeout=timeout, trust_env=False, follow_redirects=False,
            headers={"User-Agent": "HomeMediaCatalog/0.2 (music artwork)"}
        ) as client:
            for _ in range(4):
                if not _trusted(url):
                    return None
                async with client.stream("GET", url) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            return None
                        url = urljoin(url, location)
                        continue
                    if response.status_code != 200:
                        return None
                    if (
                        response.headers.get("content-length", "").isdigit()
                        and int(response.headers["content-length"]) > _MAX_BYTES
                    ):
                        return None
                    data = bytearray()
                    async for chunk in response.aiter_bytes(64 * 1024):
                        data.extend(chunk)
                        if len(data) > _MAX_BYTES:
                            return None
                    image = bytes(data)
                    if image.startswith(bytes((0xFF, 0xD8, 0xFF))) or image.startswith(
                        bytes((0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A))
                    ):
                        return image
                    return None
    except (httpx.HTTPError, OSError, ValueError):
        return None
    return None
