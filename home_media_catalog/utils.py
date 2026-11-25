"""Source normalization without assumptions about a running Kodi client."""

import re
import unicodedata
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from html import unescape
from typing import Any


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# Time of the change on the editing device (X-Modified-At), set per request.
client_modified_at: ContextVar[str | None] = ContextVar("client_modified_at", default=None)


def modification_time() -> str:
    """Device time of a replayed edit, bounded so a wrong clock cannot
    place a change in the future or before the last year."""
    current = datetime.now(UTC)
    value = client_modified_at.get()
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed and parsed.tzinfo and current - timedelta(days=365) <= parsed <= current:
            return parsed.astimezone(UTC).isoformat(timespec="seconds")
    return current.isoformat(timespec="seconds")


def fold(value: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", value.casefold()) if not unicodedata.combining(c)
    )


def plain_text(value: str | None) -> str:
    # Metadata is rendered as text, never as source-supplied HTML.
    return unescape(re.sub(r"<[^>]+>", " ", value or "")).strip()


def number(value: Any, default: float = 0) -> float:
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else default
    except (TypeError, ValueError):
        return default


def positive(value: Any) -> float | None:
    result = number(value)
    return result if result > 0 else None


def integer(value: Any) -> int | None:
    result = positive(value)
    return int(result) if result is not None else None


def date_value(value: Any) -> str | None:
    if value in (None, "", "0000-00-00", "0000-00-00 00:00:00"):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    # Kodi SQL timestamps have no timezone. Preserve them as naive source times.
    return dt.isoformat(timespec="seconds")


def imdb_id(value: Any) -> str | None:
    if value is None:
        return None
    match = re.fullmatch(r"(?:tt)?(\d{1,12})", str(value).strip())
    return "tt" + match[1].zfill(7) if match else None


def identifiers(values: dict[str, Any]) -> dict[str, str]:
    result = {}
    for key, value in values.items():
        key = key.lower()
        if key == "imdb":
            if normalized := imdb_id(value):
                result[key] = normalized
        elif key in ("tmdb", "tvdb") and str(value).isdigit() and int(value) > 0:
            result[key] = str(int(value))
    return result


def formats(values: list[str]) -> list[str]:
    names = {"blu-ray": "Blu-ray", "blu-ray 3d": "Blu-ray 3D", "4k uhd": "Blu-ray 4K", "dvd": "DVD"}
    return sorted({names.get(v.casefold().strip(), v.strip()) for v in values if v.strip()})
