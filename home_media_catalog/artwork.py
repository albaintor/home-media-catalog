"""Authenticated, cached artwork independent of Kodi's HTTP thumbnail server."""

import asyncio
import hashlib
import ipaddress
import json
import socket
import time
from pathlib import Path
from threading import Lock
from urllib.parse import unquote, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver


class ArtworkError(Exception):
    pass


class PublicResolver(AbstractResolver):
    """Validate and return the very same addresses used by the HTTP connection.

    Unlike a separate DNS preflight, this avoids a second resolution and DNS
    rebinding. Local NAS artwork must use explicit read-only mount mappings.
    """

    async def resolve(self, host, port=0, family=socket.AF_INET):
        rows = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=family, type=socket.SOCK_STREAM
        )
        results = []
        for fam, _, proto, _, address in rows:
            ip = ipaddress.ip_address(address[0])
            if not ip.is_global or ip.is_multicast:
                raise ArtworkError("Private or reserved artwork addresses are not allowed.")
            results.append(
                {
                    "hostname": host,
                    "host": address[0],
                    "port": port,
                    "family": fam,
                    "proto": proto,
                    "flags": socket.AI_NUMERICHOST,
                }
            )
        if not results:
            raise ArtworkError("The artwork hostname has no public address.")
        return results

    async def close(self):
        pass


class Artwork:
    def __init__(self, settings):
        self.settings = settings
        self.cache = settings.data_dir / "artwork"
        self.semaphore = asyncio.Semaphore(6)
        self.last_prune = 0.0
        self.write_lock = Lock()

    def _mounted(self, url: str) -> Path | None:
        decoded = unquote(url)
        for prefix, directory in sorted(
            self.settings.artwork_mounts.items(), key=lambda p: -len(p[0])
        ):
            if not prefix.endswith("/"):
                raise ArtworkError("Artwork mount prefixes must end with a slash.")
            if decoded.startswith(prefix):
                root = Path(directory).resolve()
                candidate = (root / decoded[len(prefix) :]).resolve()
                if not candidate.is_relative_to(root) or not candidate.is_file():
                    raise ArtworkError("Artwork does not exist in the configured mount.")
                return candidate
        return None

    def _cached(self, key: str):
        path = self.cache / (key + ".bin")
        meta = self.cache / (key + ".json")
        if path.is_file() and meta.is_file() and time.time() - path.stat().st_mtime < 30 * 86400:
            return path.read_bytes(), json.loads(meta.read_text())["content_type"]
        return None

    def invalidate(self, candidates: list[str]) -> None:
        with self.write_lock:
            for url in candidates:
                key = hashlib.sha256(url.encode()).hexdigest()
                for suffix in (".bin", ".json"):
                    (self.cache / (key + suffix)).unlink(missing_ok=True)

    @staticmethod
    def _image_type(data: bytes) -> str:
        if data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if data.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        if data[4:8] == b"ftyp" and data[8:12] in (b"avif", b"avis"):
            return "image/avif"
        raise ArtworkError("Unsupported image content.")

    def _save(self, key: str, data: bytes, content_type: str):
        with self.write_lock:
            self._save_locked(key, data, content_type)

    def _save_locked(self, key: str, data: bytes, content_type: str):
        self.cache.mkdir(parents=True, exist_ok=True)
        temporary = self.cache / (key + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(self.cache / (key + ".bin"))
        (self.cache / (key + ".json")).write_text(json.dumps({"content_type": content_type}))
        if time.monotonic() - self.last_prune >= 60:
            self.last_prune = time.monotonic()
            files = sorted(self.cache.glob("*.bin"), key=lambda p: p.stat().st_mtime)
            total = sum(p.stat().st_size for p in files)
            for path in files:
                if total <= 512 * 1024 * 1024:
                    break
                total -= path.stat().st_size
                path.unlink(missing_ok=True)
                path.with_suffix(".json").unlink(missing_ok=True)

    async def get(
        self, candidates: list[str], *, persist: bool = True
    ) -> tuple[bytes, str]:
        async with self.semaphore:
            for url in candidates:
                key = hashlib.sha256(url.encode()).hexdigest()
                try:
                    # Music artwork is fetched on demand and never written
                    # to the persistent server artwork cache.
                    if persist:
                        if cached := await asyncio.to_thread(self._cached, key):
                            return cached
                    mounted = await asyncio.to_thread(self._mounted, url)
                    if mounted:
                        size = (await asyncio.to_thread(mounted.stat)).st_size
                        if size > self.settings.max_artwork_bytes:
                            raise ArtworkError("Artwork file is too large.")
                        data = await asyncio.to_thread(mounted.read_bytes)
                    else:
                        parsed = urlsplit(url)
                        if (
                            parsed.scheme not in ("http", "https")
                            or not parsed.hostname
                            or parsed.username
                            or parsed.password
                            or parsed.port not in (None, 80, 443)
                        ):
                            raise ArtworkError("Unsupported artwork URL.")
                        # aiohttp skips the resolver for numeric hosts, so validate
                        # them before constructing the connector as well.
                        try:
                            ip = ipaddress.ip_address(parsed.hostname)
                        except ValueError:
                            ip = None
                        if ip and (not ip.is_global or ip.is_multicast):
                            raise ArtworkError("Private artwork addresses require a mount mapping.")
                        connector = aiohttp.TCPConnector(
                            resolver=PublicResolver(), use_dns_cache=False
                        )
                        async with aiohttp.ClientSession(
                            connector=connector,
                            timeout=aiohttp.ClientTimeout(total=12),
                            trust_env=False,
                        ) as session:
                            async with session.get(url, allow_redirects=False) as response:
                                if response.status != 200:
                                    raise ArtworkError("Artwork unavailable.")
                                if (
                                    response.content_length
                                    and response.content_length > self.settings.max_artwork_bytes
                                ):
                                    raise ArtworkError("Artwork is too large.")
                                chunks, length = [], 0
                                async for chunk in response.content.iter_chunked(65536):
                                    length += len(chunk)
                                    if length > self.settings.max_artwork_bytes:
                                        raise ArtworkError("Artwork is too large.")
                                    chunks.append(chunk)
                                data = b"".join(chunks)
                    content_type = self._image_type(data)
                    if persist:
                        await asyncio.to_thread(self._save, key, data, content_type)
                    return data, content_type
                except (ArtworkError, aiohttp.ClientError, OSError, ValueError, TimeoutError):
                    continue
        raise ArtworkError(
            "No accessible artwork. Configure a NAS artwork mount for local Kodi images."
        )
