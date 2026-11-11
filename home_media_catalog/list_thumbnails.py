"""Persistent, versioned list-cover mosaics.

The image is generated once per list revision, kept under the configured data
directory and served to web/iOS without repeating image composition. Existing
lists are materialized on first request; edits schedule regeneration after the
response. Old revisions can never overwrite a newer version.
"""

import asyncio
import io
import logging
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError

from .artwork import ArtworkError

LOGGER = logging.getLogger(__name__)

WIDTH, HEIGHT, GAP = 360, 216, 4
BACKGROUND = (25, 33, 46)


def _regions(count: int, width: int = WIDTH, height: int = HEIGHT) -> list[tuple[int, int, int, int]]:
    if count == 1:
        return [(0, 0, width, height)]
    if count == 2:
        left = (width - GAP) // 2
        return [(0, 0, left, height), (left + GAP, 0, width - left - GAP, height)]
    if count == 3:
        left = (width - GAP) // 2
        top = (height - GAP) // 2
        return [
            (0, 0, left, top),
            (left + GAP, 0, width - left - GAP, top),
            ((width - left) // 2, top + GAP, left, height - top - GAP),
        ]
    left, top = (width - GAP) // 2, (height - GAP) // 2
    return [
        (0, 0, left, top),
        (left + GAP, 0, width - left - GAP, top),
        (0, top + GAP, left, height - top - GAP),
        (left + GAP, top + GAP, width - left - GAP, height - top - GAP),
    ]


def compose(images: list[bytes | None], *, width: int = WIDTH, height: int = HEIGHT) -> bytes:
    """Fill each tile even when its original poster is unavailable."""
    canvas = Image.new("RGB", (width, height), BACKGROUND)
    if not images:
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle((width * 2 // 5, height // 4, width * 3 // 5, height * 3 // 4), radius=10, outline=(125, 153, 177), width=4)
        draw.line((width * 9 // 20, height // 3, width * 11 // 20, height // 3), fill=(125, 153, 177), width=4)
        draw.line((width * 9 // 20, height // 2, width * 11 // 20, height // 2), fill=(125, 153, 177), width=4)
    else:
        for data, (x, y, w, h) in zip(images[:4], _regions(len(images[:4]), width, height), strict=False):
            if data:
                try:
                    with Image.open(io.BytesIO(data)) as source:
                        if source.width * source.height > 40_000_000:
                            raise ValueError("Poster is too large.")
                        tile = ImageOps.fit(
                            ImageOps.exif_transpose(source).convert("RGB"),
                            (w, h),
                            method=Image.Resampling.LANCZOS,
                        )
                    canvas.paste(tile, (x, y))
                    continue
                except (UnidentifiedImageError, OSError, ValueError):
                    pass
            draw = ImageDraw.Draw(canvas)
            draw.rectangle((x, y, x + w - 1, y + h - 1), fill=(37, 49, 65))
            draw.rounded_rectangle(
                (x + w // 4, y + h // 4, x + 3 * w // 4, y + 3 * h // 4),
                radius=8,
                outline=(111, 135, 160),
                width=3,
            )

    output = io.BytesIO()
    canvas.save(output, format="JPEG", quality=85, optimize=True)
    return output.getvalue()


class ListThumbnails:
    def __init__(self, store, artwork, data_dir: Path):
        self.store = store
        self.artwork = artwork
        self.directory = data_dir / "list-thumbnails"
        self._locks: dict[str, asyncio.Lock] = {}

    def _path(self, list_id: str, revision: int) -> Path:
        # A list is always retrieved by ID from the database before this path
        # is built; avoid trusting URL segments as filesystem path components.
        return self.directory / f"{list_id}-{revision}-layout2.jpg"

    def thumbnail_input(self, list_id: str):
        return self.store.list_thumbnail_input(list_id)

    def composite(self, images: list[bytes | None]) -> bytes:
        return compose(images)

    async def ensure(self, list_id: str, revision: int) -> bytes | None:
        """Read already-generated image; generate at most once for a revision."""
        if revision < 0:
            return None
        lock = self._locks.setdefault(list_id, asyncio.Lock())
        async with lock:
            source = await asyncio.to_thread(self.thumbnail_input, list_id)
            if source is None or source["revision"] != revision:
                return None
            path = self._path(list_id, revision)
            if path.is_file():
                return await asyncio.to_thread(path.read_bytes)

            async def poster(candidates: list[str]) -> bytes | None:
                if not candidates:
                    return None
                try:
                    image, _mime = await self.artwork.get(candidates)
                    return image
                except ArtworkError:
                    return None

            # At most 7 seconds for all four posters, never 4 sequential
            # network timeouts; missing artwork is drawn as a placeholder.
            try:
                images = await asyncio.wait_for(
                    asyncio.gather(*(poster(c) for c in source["posters"])),
                    timeout=7,
                )
            except TimeoutError:
                images = [None] * len(source["posters"])
            result = await asyncio.to_thread(self.composite, images)
            # An import or list edit may have changed the revision while posters
            # were downloaded. Never cache a stale image under a newer URL.
            current = await asyncio.to_thread(self.thumbnail_input, list_id)
            if current is None or current["revision"] != revision:
                return None
            # If candidate posters failed to load (including a network timeout),
            # serve a temporary placeholder but do not persist it as an immutable
            # cover for this revision. A later request can recover the artwork.
            # A member with no poster metadata is also incomplete: a later
            # sync can supply its poster without changing collection membership.
            # Do not freeze a placeholder as the final revisioned image.
            missing_download = any(image is None for image in images)
            if not missing_download:
                await asyncio.to_thread(self._save, list_id, revision, result)
            return result

    async def warm(self, list_id: str) -> None:
        source = await asyncio.to_thread(self.thumbnail_input, list_id)
        if source is not None:
            try:
                await self.ensure(list_id, source["revision"])
            except (OSError, ValueError):
                LOGGER.warning("Unable to cache list thumbnail %s", list_id, exc_info=True)

    def _save(self, list_id: str, revision: int, data: bytes) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(list_id, revision)
        temporary = self.directory / f".{uuid4().hex}.tmp"
        try:
            temporary.write_bytes(data)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        self.remove_old(list_id, keep=path)

    def remove_old(self, list_id: str, keep: Path | None = None) -> None:
        if not self.directory.exists():
            return
        for path in self.directory.glob(f"{list_id}-*.jpg"):
            if path != keep:
                path.unlink(missing_ok=True)

    async def delete(self, list_id: str) -> None:
        lock = self._locks.setdefault(list_id, asyncio.Lock())
        async with lock:
            await asyncio.to_thread(self.remove_old, list_id)


class CollectionThumbnails(ListThumbnails):
    """Persistent 2:3 mosaics with the same race-free revision checks as lists."""

    def __init__(self, store, artwork, data_dir: Path):
        super().__init__(store, artwork, data_dir)
        self.directory = data_dir / "collection-thumbnails"

    def _path(self, list_id: str, revision: int) -> Path:
        # Invalidate covers cached with the old, potentially empty layout.
        return self.directory / f"{list_id}-{revision}-layout4.jpg"

    def thumbnail_input(self, collection_id: str):
        return self.store.collection_thumbnail_input(collection_id)

    def composite(self, images: list[bytes | None]) -> bytes:
        return compose(images, width=300, height=450)
