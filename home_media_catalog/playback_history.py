"""Durable local-first recent playback history shared by the web API and iOS.

The server is a replication peer, not the owner of playback events. Clients
can queue records and category clears offline, then merge on reconnect.
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator

from .auth import authenticated, writable

router = APIRouter(prefix="/api/v1/playback/history", tags=["Playback"])
_CATEGORIES = ("video", "music")
_MAX_ENTRIES = 200


class RecentEntry(BaseModel):
    id: str = Field(min_length=1, max_length=256)
    category: Literal["video", "music"]
    kind: str = Field(max_length=40)
    title: str = Field(max_length=512)
    subtitle: str = Field(default="", max_length=512)
    artworkPath: str | None = Field(default=None, max_length=2048)
    albumID: str | None = Field(default=None, max_length=256)
    position: float = Field(ge=0)
    total: float = Field(ge=0)
    updatedAt: float = Field(ge=0)

    @field_validator("position", "total", "updatedAt")
    @classmethod
    def finite_number(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("Playback values must be finite")
        return value


class HistoryState(BaseModel):
    entries: list[RecentEntry] = Field(default_factory=list, max_length=500)
    clearedAt: dict[str, float] = Field(default_factory=dict)

    @field_validator("clearedAt")
    @classmethod
    def validate_clears(cls, value: dict[str, float]) -> dict[str, float]:
        if any(
            key not in _CATEGORIES or not math.isfinite(ts) or ts < 0 for key, ts in value.items()
        ):
            raise ValueError("Invalid playback history clear marker")
        return value


def _database(request: Request) -> Path:
    data_dir = Path(request.app.state.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "playback-history.db"


def _initialize(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS recent_playbacks (
            id TEXT PRIMARY KEY, category TEXT NOT NULL,
            updated_at REAL NOT NULL, payload TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS recent_playback_clears (
            category TEXT PRIMARY KEY, cleared_at REAL NOT NULL
        )
    """)


def _snapshot(conn: sqlite3.Connection) -> dict:
    cleared = dict(
        conn.execute("SELECT category, cleared_at FROM recent_playback_clears").fetchall()
    )
    entries = [
        json.loads(row[0])
        for row in conn.execute(
            "SELECT payload FROM recent_playbacks ORDER BY updated_at DESC LIMIT ?",
            (2 * _MAX_ENTRIES,),
        )
    ]
    return {"entries": entries, "clearedAt": cleared}


def _merge(path: Path, incoming: HistoryState | None = None) -> dict:
    # SQLite BEGIN IMMEDIATE serializes concurrent replication calls, including
    # from multiple iPhones. A category clear is a durable tombstone.
    with sqlite3.connect(path, timeout=10) as conn:
        _initialize(conn)
        if incoming is not None:
            conn.execute("BEGIN IMMEDIATE")
            for category, when in incoming.clearedAt.items():
                conn.execute(
                    "INSERT INTO recent_playback_clears(category, cleared_at) "
                    "VALUES(?, ?) ON CONFLICT(category) DO UPDATE SET "
                    "cleared_at = MAX(cleared_at, excluded.cleared_at)",
                    (category, when),
                )
            clear_times = dict(
                conn.execute("SELECT category, cleared_at FROM recent_playback_clears").fetchall()
            )
            for category, cutoff in clear_times.items():
                conn.execute(
                    "DELETE FROM recent_playbacks WHERE category = ? AND updated_at <= ?",
                    (category, cutoff),
                )
            for entry in incoming.entries:
                if entry.updatedAt <= clear_times.get(entry.category, 0):
                    continue
                conn.execute(
                    "INSERT INTO recent_playbacks(id, category, updated_at, payload) "
                    "VALUES(?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                    "category = excluded.category, "
                    "updated_at = excluded.updated_at, payload = excluded.payload "
                    "WHERE excluded.updated_at > recent_playbacks.updated_at",
                    (entry.id, entry.category, entry.updatedAt, entry.model_dump_json()),
                )
            # Bound server storage without limiting the ten visible results.
            for category in _CATEGORIES:
                conn.execute(
                    "DELETE FROM recent_playbacks WHERE category = ? AND id NOT IN "
                    "(SELECT id FROM recent_playbacks WHERE category = ? "
                    "ORDER BY updated_at DESC LIMIT ?)",
                    (category, category, _MAX_ENTRIES),
                )
        return _snapshot(conn)


@router.get("", response_model=HistoryState, dependencies=[Depends(authenticated)])
def get_recent_history(request: Request):
    return _merge(_database(request))


@router.post("/sync", response_model=HistoryState, dependencies=[Depends(writable)])
def synchronize_recent_history(request: Request, data: HistoryState):
    return _merge(_database(request), data)
