"""Transactional local catalogue. SQLite never shares Kodi's schema or writes to MariaDB."""

import json
import re
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from .connectors.base import Record
from .connectors.collection_xml import COLLECTION_TYPES, SOURCE_TYPES
from .media_formats import canonical_format, canonical_source_format
from .people import category as credit_category
from .people import person_view, youtube_url
from .utils import fold, now

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 1, config TEXT NOT NULL DEFAULT '{}',
 last_sync TEXT, last_error TEXT
);
CREATE TABLE IF NOT EXISTS works (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
 sort_title TEXT NOT NULL DEFAULT '', original_title TEXT, year INTEGER, plot TEXT,
 runtime_minutes INTEGER, rating REAL, user_rating REAL, date_added TEXT, last_played TEXT,
 watch_status TEXT NOT NULL DEFAULT 'unknown', play_count INTEGER, resume_seconds REAL DEFAULT 0,
 total_seconds REAL DEFAULT 0, parent_id TEXT REFERENCES works(id) ON DELETE SET NULL,
 season INTEGER, episode INTEGER, genres TEXT DEFAULT '[]', formats TEXT DEFAULT '[]',
 collections TEXT DEFAULT '[]', source_types TEXT DEFAULT '[]', poster TEXT DEFAULT '[]',
 fanart TEXT DEFAULT '[]', search_text TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS records (
 id TEXT PRIMARY KEY, work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
 source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
 external_key TEXT NOT NULL, payload TEXT NOT NULL,
 UNIQUE(source_id,external_key)
);
CREATE INDEX IF NOT EXISTS records_work ON records(work_id);
CREATE INDEX IF NOT EXISTS records_source ON records(source_id);
-- Playback resumes belong to an edition, not a movie title or Kodi player.
-- The user position survives catalogue rescans and never edits source imports.
CREATE TABLE IF NOT EXISTS playback_resume (
 work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
 edition_id TEXT NOT NULL REFERENCES records(id) ON DELETE CASCADE,
 position_seconds REAL NOT NULL,
 total_seconds REAL NOT NULL,
 updated_at TEXT NOT NULL,
 PRIMARY KEY(work_id,edition_id)
);
CREATE INDEX IF NOT EXISTS playback_resume_updated ON playback_resume(updated_at);
CREATE TABLE IF NOT EXISTS identifiers (
 work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
 kind TEXT NOT NULL, namespace TEXT NOT NULL, value TEXT NOT NULL,
 PRIMARY KEY(work_id,namespace,value)
);
CREATE INDEX IF NOT EXISTS identifier_lookup ON identifiers(kind,namespace,value);
CREATE INDEX IF NOT EXISTS work_sort_title ON works(sort_title,id);
CREATE INDEX IF NOT EXISTS work_date ON works(date_added,id);
CREATE INDEX IF NOT EXISTS work_kind_watch ON works(kind,watch_status);
CREATE INDEX IF NOT EXISTS work_parent_kind_season ON works(parent_id,kind,season,episode);
CREATE VIRTUAL TABLE IF NOT EXISTS works_fts USING fts5(work_id UNINDEXED, search_text, tokenize='unicode61 remove_diacritics 2');
CREATE VIRTUAL TABLE IF NOT EXISTS works_title_fts USING fts5(work_id UNINDEXED, titles, tokenize='unicode61 remove_diacritics 2');
CREATE INDEX IF NOT EXISTS work_kind_year ON works(kind,year);
CREATE TABLE IF NOT EXISTS catalogue_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS players (
 id TEXT PRIMARY KEY, name TEXT NOT NULL,
 source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
 config TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
 status TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT, result TEXT, error TEXT,
 operation TEXT NOT NULL DEFAULT 'kodi_sync',
 progress_phase TEXT, progress_current INTEGER NOT NULL DEFAULT 0,
 progress_total INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS personal (
 work_id TEXT PRIMARY KEY REFERENCES works(id) ON DELETE CASCADE,
 user_rating REAL, watched INTEGER
);
CREATE TABLE IF NOT EXISTS work_person_ids (
 work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
 namespace TEXT NOT NULL, person_id TEXT NOT NULL,
 PRIMARY KEY(namespace,person_id,work_id)
);
CREATE INDEX IF NOT EXISTS work_person_ids_by_work ON work_person_ids(work_id);
CREATE TABLE IF NOT EXISTS work_person_names (
 work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
 name_fold TEXT NOT NULL,
 tmdb_id TEXT NOT NULL DEFAULT '',
 imdb_id TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(work_id,name_fold,tmdb_id,imdb_id)
);
CREATE INDEX IF NOT EXISTS work_person_names_by_name ON work_person_names(name_fold,work_id);
CREATE TABLE IF NOT EXISTS work_person_roles (
 work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
 name_fold TEXT NOT NULL, category TEXT NOT NULL,
 PRIMARY KEY(work_id,name_fold,category)
);
CREATE INDEX IF NOT EXISTS work_person_roles_by_name ON work_person_roles(name_fold,category,work_id);
CREATE TABLE IF NOT EXISTS device_tokens (
 token_hash TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS enrichment (
 work_id TEXT PRIMARY KEY REFERENCES works(id) ON DELETE CASCADE,
 payload TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS playback_locations (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
 target TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS playback_location_options (
 location_id TEXT PRIMARY KEY REFERENCES playback_locations(id) ON DELETE CASCADE,
 public_config TEXT NOT NULL DEFAULT '{}', password_cipher TEXT
);
CREATE TABLE IF NOT EXISTS playback_oauth_apps (
 provider TEXT PRIMARY KEY, client_id TEXT NOT NULL,
 secret_cipher TEXT, redirect_url TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS playback_cloud_credentials (
 location_id TEXT PRIMARY KEY REFERENCES playback_locations(id) ON DELETE CASCADE,
 provider TEXT NOT NULL, token_cipher TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lists (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
 revision INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS list_items (
 list_id TEXT NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
 work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
 position INTEGER NOT NULL, PRIMARY KEY(list_id,work_id)
);
CREATE INDEX IF NOT EXISTS list_order ON list_items(list_id,position);
-- Per-media detail queries use EXISTS (... WHERE work_id=?); without
-- this index SQLite scans list memberships for every imported detail.
CREATE INDEX IF NOT EXISTS list_items_by_work ON list_items(work_id,list_id);
CREATE TABLE IF NOT EXISTS writeback_audit (
 id TEXT PRIMARY KEY, work_id TEXT REFERENCES works(id) ON DELETE SET NULL,
 source_id TEXT REFERENCES sources(id) ON DELETE SET NULL,
 source_name TEXT NOT NULL, source_type TEXT NOT NULL, created_at TEXT NOT NULL,
 status TEXT NOT NULL, method TEXT, attempts TEXT NOT NULL, fields TEXT NOT NULL,
 message TEXT
);
CREATE INDEX IF NOT EXISTS writeback_audit_by_work ON writeback_audit(work_id,created_at DESC);
CREATE INDEX IF NOT EXISTS writeback_audit_by_source ON writeback_audit(source_id,created_at DESC);
CREATE TABLE IF NOT EXISTS writeback_pending (
 id TEXT PRIMARY KEY,
 work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
 source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
 changes TEXT NOT NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 retry_count INTEGER NOT NULL DEFAULT 0,
 last_error TEXT,
 UNIQUE(work_id,source_id)
);
CREATE INDEX IF NOT EXISTS writeback_pending_by_date ON writeback_pending(updated_at DESC);
PRAGMA user_version = 2;
"""


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def person_ids(work_id: str, members: Any) -> list[tuple[str, str, str]]:
    """Extract person identities; never confuse media IDs with cast IDs."""
    if not isinstance(members, list):
        return []
    identities = []
    for member in members:
        if not isinstance(member, dict):
            continue
        for namespace, key, pattern in (
            ("tmdb", "tmdb_id", r"[0-9]+"),
            ("imdb", "imdb_id", r"nm[0-9]+"),
        ):
            candidate = str(member.get(key) or "")
            if re.fullmatch(pattern, candidate):
                identities.append((namespace, candidate, work_id))
    return identities


def person_names(work_id: str, members: Any) -> list[tuple[str, str, str, str]]:
    """Index credit names together with identity claims, if available."""
    if not isinstance(members, list):
        return []
    credits = []
    for member in members:
        if not isinstance(member, dict) or not isinstance(member.get("name"), str):
            continue
        name = fold(member["name"].strip())
        if not name:
            continue
        tmdb = str(member.get("tmdb_id") or "")
        imdb = str(member.get("imdb_id") or "")
        credits.append(
            (
                work_id,
                name,
                tmdb if re.fullmatch(r"[0-9]+", tmdb) else "",
                imdb if re.fullmatch(r"nm[0-9]+", imdb) else "",
            )
        )
    return credits


def person_roles(work_id: str, members: Any) -> list[tuple[str, str, str]]:
    """Extract normalized names and canonical credit categories."""
    if not isinstance(members, list):
        return []
    result = []
    for member in members:
        if not isinstance(member, dict) or not isinstance(member.get("name"), str):
            continue
        name = fold(member["name"].strip())
        if not name:
            continue
        role = str(member.get("category") or "")
        role = credit_category(
            role if role not in ("crew", "") else str(member.get("role") or role)
        )
        result.append((work_id, name, role))
    return result


class Store:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        with self.connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > 2:
                raise RuntimeError(
                    "Catalogue schema is newer than this application; do not downgrade."
                )
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            # Music tables are additive and independent from video works.
            from .music import MusicCatalogue

            MusicCatalogue.initialize(conn)
            # Remove obsolete separate-writer secrets on existing installations.
            for row in conn.execute("SELECT id,config FROM sources WHERE type='kodi'"):
                saved = json.loads(row["config"])
                if "write_username" in saved or "write_password" in saved:
                    saved.pop("write_username", None)
                    saved.pop("write_password", None)
                    conn.execute(
                        "UPDATE sources SET config=? WHERE id=?", (encode(saved), row["id"])
                    )
            # Backfill the new title-only index for existing installations without
            # requiring users to reimport their Kodi or CLZ data.
            if not conn.execute("SELECT 1 FROM works_title_fts LIMIT 1").fetchone():
                conn.executemany(
                    "INSERT INTO works_title_fts(work_id,titles) VALUES(?,?)",
                    (
                        (
                            row["id"],
                            fold(" ".join(filter(None, (row["title"], row["original_title"])))),
                        )
                        for row in conn.execute("SELECT id,title,original_title FROM works")
                    ),
                )
            # Populate the indexed person identities once for existing NAS catalogues.
            # This avoids rescanning Kodi/TMDB and preserves manually imported credits.
            if not conn.execute(
                "SELECT 1 FROM catalogue_meta WHERE key='person_ids_index_v1'"
            ).fetchone():
                for record in conn.execute("SELECT work_id,payload FROM records"):
                    metadata = json.loads(record["payload"]).get("metadata", {})
                    if not isinstance(metadata, dict):
                        continue
                    for key in ("people", "cast"):
                        conn.executemany(
                            "INSERT OR IGNORE INTO work_person_ids(namespace,person_id,work_id)"
                            " VALUES(?,?,?)",
                            person_ids(record["work_id"], metadata.get(key)),
                        )
                for record in conn.execute("SELECT work_id,payload FROM enrichment"):
                    conn.executemany(
                        "INSERT OR IGNORE INTO work_person_ids(namespace,person_id,work_id)"
                        " VALUES(?,?,?)",
                        person_ids(record["work_id"], json.loads(record["payload"]).get("people")),
                    )
                conn.execute(
                    "INSERT INTO catalogue_meta(key,value) VALUES('person_ids_index_v1',?)",
                    (now(),),
                )
            # Rebuild the small normalized credit-name index once for databases
            # populated before name-assisted searches were introduced.
            if not conn.execute(
                "SELECT 1 FROM catalogue_meta WHERE key='person_names_index_v1'"
            ).fetchone():
                for record in conn.execute("SELECT work_id,payload FROM records"):
                    metadata = json.loads(record["payload"]).get("metadata", {})
                    if isinstance(metadata, dict):
                        for key in ("people", "cast"):
                            conn.executemany(
                                "INSERT OR IGNORE INTO work_person_names"
                                "(work_id,name_fold,tmdb_id,imdb_id) VALUES(?,?,?,?)",
                                person_names(record["work_id"], metadata.get(key)),
                            )
                for record in conn.execute("SELECT work_id,payload FROM enrichment"):
                    conn.executemany(
                        "INSERT OR IGNORE INTO work_person_names"
                        "(work_id,name_fold,tmdb_id,imdb_id) VALUES(?,?,?,?)",
                        person_names(
                            record["work_id"], json.loads(record["payload"]).get("people")
                        ),
                    )
                conn.execute(
                    "INSERT INTO catalogue_meta(key,value) VALUES('person_names_index_v1',?)",
                    (now(),),
                )
            # Backfill role-aware names without requiring a Kodi resynchronization.
            if not conn.execute(
                "SELECT 1 FROM catalogue_meta WHERE key='person_roles_index_v1'"
            ).fetchone():
                for record in conn.execute("SELECT work_id,payload FROM records"):
                    metadata = json.loads(record["payload"]).get("metadata") or {}
                    if not isinstance(metadata, dict):
                        continue
                    for field in ("people", "cast"):
                        conn.executemany(
                            "INSERT OR IGNORE INTO work_person_roles VALUES(?,?,?)",
                            person_roles(record["work_id"], metadata.get(field)),
                        )
                    directors = metadata.get("director") or ""
                    if isinstance(directors, str):
                        conn.executemany(
                            "INSERT OR IGNORE INTO work_person_roles VALUES(?,?,?)",
                            (
                                (record["work_id"], fold(name.strip()), "director")
                                for name in directors.split(" / ")
                                if name.strip()
                            ),
                        )
                for record in conn.execute("SELECT work_id,payload FROM enrichment"):
                    conn.executemany(
                        "INSERT OR IGNORE INTO work_person_roles VALUES(?,?,?)",
                        person_roles(
                            record["work_id"], json.loads(record["payload"]).get("people")
                        ),
                    )
                conn.execute(
                    "INSERT INTO catalogue_meta(key,value) VALUES('person_roles_index_v1',?)",
                    (now(),),
                )
            # Compatible migration for existing NAS installations (schema v2).
            job_columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
            for column, definition in (
                ("progress_phase", "TEXT"),
                ("progress_current", "INTEGER NOT NULL DEFAULT 0"),
                ("progress_total", "INTEGER NOT NULL DEFAULT 0"),
                ("operation", "TEXT NOT NULL DEFAULT 'kodi_sync'"),
            ):
                if column not in job_columns:
                    conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {definition}")
            # Label pre-migration collection jobs correctly, without changing their history.
            conn.execute(
                "UPDATE jobs SET operation='collection_import' WHERE operation='kodi_sync' "
                "AND source_id IN (SELECT id FROM sources WHERE type!='kodi')"
            )
            conn.execute(
                "UPDATE jobs SET status='failed',finished_at=?,error='Application restarted before the job completed' WHERE status IN ('pending','running')",
                (now(),),
            )
            # Reconcile pre-existing separate Kodi/CLZ works once when upgrading.
            # v2 includes strong ID matches even when titles or years differ.
            if not conn.execute(
                "SELECT 1 FROM catalogue_meta WHERE key='identity_matching_v2'"
            ).fetchone():
                self._reconcile_titles(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO catalogue_meta(key,value) VALUES('identity_matching_v2',?)",
                    (now(),),
                )
            # Rebuild affected works from their CLZ source records once, so an
            # existing NAS database gets physical 4K UHD disc badges on upgrade.
            if not conn.execute(
                "SELECT 1 FROM catalogue_meta WHERE key='uhd_disc_formats_v1'"
            ).fetchone():
                old_uhd_works = [
                    row["work_id"]
                    for row in conn.execute(
                        "SELECT DISTINCT r.work_id FROM records r "
                        "JOIN sources s ON s.id=r.source_id "
                        "WHERE s.type='clz' AND r.payload LIKE '%4K UHD%'"
                    )
                ]
                for work_id in old_uhd_works:
                    self._rebuild(conn, work_id)
                conn.execute(
                    "INSERT INTO catalogue_meta(key,value) VALUES('uhd_disc_formats_v1',?)",
                    (now(),),
                )
        self.path.chmod(0o600)


    # Maintenance is intentionally scoped to the Home Media Catalog SQLite
    # catalogue. It never issues SQL DELETE against Kodi/MyVideos MariaDB.
    @staticmethod
    def _endpoint(config: dict, *, kodi: bool = False) -> str:
        host = str(config.get("host") or "").strip().lower().rstrip(".")
        if not host:
            return ""
        port = int(config.get("port") or (8080 if kodi else 3306))
        return f"{host}:{port}" + (":https" if config.get("tls") else ":http") if kodi else f"{host}:{port}"

    @classmethod
    def _duplicate_source_map(cls, conn: sqlite3.Connection) -> dict[str, str]:
        sources = [dict(r) for r in conn.execute("SELECT rowid,* FROM sources ORDER BY rowid")]
        players = [dict(r) for r in conn.execute("SELECT source_id,config FROM players")]
        endpoints = defaultdict(set)
        for row in players:
            endpoint = cls._endpoint(json.loads(row["config"]), kodi=True)
            if endpoint:
                endpoints[row["source_id"]].add(endpoint)
        duplicates = {}
        canonical: list[dict] = []
        for source in sources:
            kind = source["type"]
            config = json.loads(source["config"])
            matched = []
            for existing in canonical:
                if existing["type"] != kind:
                    continue
                if kind != "kodi":
                    matched.append(existing)
                    continue
                maria = cls._endpoint(config)
                other_maria = cls._endpoint(json.loads(existing["config"]))
                if (maria and maria == other_maria) or endpoints[source["id"]].intersection(
                    endpoints[existing["id"]]
                ):
                    matched.append(existing)
            # An ambiguous Kodi endpoint cannot be reconciled safely.
            if len(matched) == 1:
                duplicates[source["id"]] = matched[0]["id"]
            else:
                canonical.append(source)
        return duplicates

    def catalogue_maintenance_preview(self) -> dict[str, Any]:
        with self.connect() as conn:
            mappings = self._duplicate_source_map(conn)
            orphan_works = conn.execute(
                "SELECT COUNT(*) FROM works w WHERE NOT EXISTS("
                "SELECT 1 FROM records r WHERE r.work_id=w.id)"
            ).fetchone()[0]
            duplicate_records = sum(
                conn.execute(
                    "SELECT COUNT(*) FROM records r WHERE r.source_id=? "
                    "AND EXISTS(SELECT 1 FROM records k WHERE k.source_id=? "
                    "AND k.external_key=r.external_key)",
                    (old, canonical),
                ).fetchone()[0]
                for old, canonical in mappings.items()
            )
            return {
                "duplicate_sources": len(mappings),
                "duplicate_records": duplicate_records,
                "orphan_works": orphan_works,
                "orphan_players": conn.execute(
                    "SELECT COUNT(*) FROM players p WHERE NOT EXISTS "
                    "(SELECT 1 FROM sources s WHERE s.id=p.source_id)"
                ).fetchone()[0],
                "orphan_records": conn.execute(
                    "SELECT COUNT(*) FROM records r WHERE NOT EXISTS "
                    "(SELECT 1 FROM sources s WHERE s.id=r.source_id) "
                    "OR NOT EXISTS(SELECT 1 FROM works w WHERE w.id=r.work_id)"
                ).fetchone()[0],
                "sources": conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
                "works": conn.execute("SELECT COUNT(*) FROM works").fetchone()[0],
            }

    def maintain_catalogue(
        self, *, reset: bool = False, reset_content: bool = False
    ) -> dict[str, Any]:
        if reset and reset_content:
            raise ValueError("Choose a single catalogue maintenance operation.")
        if reset_content:
            # Retain configured sources, Kodi instances, passwords and list
            # definitions. Foreign-key cascades remove records, personal data,
            # pending writebacks and list memberships with the media works.
            # The external Kodi / MyVideos database is never modified.
            with self.connect() as conn:
                conn.execute("DELETE FROM works")
                conn.execute("DELETE FROM jobs")
                conn.execute("DELETE FROM catalogue_meta")
                conn.execute("DELETE FROM writeback_audit")
                conn.execute("DELETE FROM works_fts")
                conn.execute("DELETE FROM works_title_fts")
                conn.execute("UPDATE sources SET last_sync=NULL, last_error=NULL")
                if conn.execute("PRAGMA foreign_key_check").fetchone():
                    raise RuntimeError("Catalogue reset failed integrity checks; rolled back.")
            return {
                "status": "content_reset",
                "preserved": ["sources", "players", "lists", "device_tokens",
                              "playback_locations", "playback_cloud_credentials",
                              "playback_oauth_apps"],
            }
        if reset:
            with self.connect() as conn:
                # Preserve server credentials/device tokens, playback network
                # locations, cloud credentials, OAuth apps and other settings.
                for table in ("sources", "works", "lists", "jobs", "catalogue_meta"):
                    conn.execute(f"DELETE FROM {table}")
                conn.execute("DELETE FROM writeback_audit")
                conn.execute("DELETE FROM works_fts")
                conn.execute("DELETE FROM works_title_fts")
            return {"status": "reset",
                    "preserved": ["device_tokens", "playback_locations",
                                  "playback_cloud_credentials", "playback_oauth_apps"]}
        removed_sources = 0
        skipped_ambiguous = 0
        collapsed_records = 0
        with self.connect() as conn:
            mappings = self._duplicate_source_map(conn)
            for old_id, keep_id in mappings.items():
                # Canonical server source configuration and UUID win.
                # Colliding external keys are duplicates of the same imported
                # source. Preserve personal values and playlist membership.
                rows = conn.execute(
                    "SELECT id,work_id,external_key FROM records WHERE source_id=?",
                    (old_id,),
                ).fetchall()
                for record in rows:
                    same = conn.execute(
                        "SELECT work_id FROM records WHERE source_id=? AND external_key=?",
                        (keep_id, record["external_key"]),
                    ).fetchone()
                    if same:
                        keep_work, drop_work = same["work_id"], record["work_id"]
                        if keep_work != drop_work:
                            current = conn.execute(
                                "SELECT user_rating,watched FROM personal WHERE work_id=?",
                                (drop_work,),
                            ).fetchone()
                            if current:
                                conn.execute(
                                    "INSERT OR IGNORE INTO personal(work_id,user_rating,watched) VALUES(?,?,?)",
                                    (keep_work, current["user_rating"], current["watched"]),
                                )
                                conn.execute(
                                    "UPDATE personal SET user_rating=COALESCE(user_rating,?),"
                                    "watched=COALESCE(watched,?) WHERE work_id=?",
                                    (current["user_rating"], current["watched"], keep_work),
                                )
                            conn.execute(
                                "INSERT OR IGNORE INTO list_items(list_id,work_id,position) "
                                "SELECT list_id,?,position FROM list_items WHERE work_id=?",
                                (keep_work, drop_work),
                            )
                        conn.execute("DELETE FROM records WHERE id=?", (record["id"],))
                        collapsed_records += 1
                    else:
                        conn.execute(
                            "UPDATE records SET source_id=? WHERE id=?", (keep_id, record["id"])
                        )
                for player in conn.execute(
                    "SELECT id,config FROM players WHERE source_id=?", (old_id,)
                ).fetchall():
                    endpoint = self._endpoint(json.loads(player["config"]), kodi=True)
                    existing = [
                        self._endpoint(json.loads(item["config"]), kodi=True)
                        for item in conn.execute(
                            "SELECT config FROM players WHERE source_id=?", (keep_id,)
                        )
                    ]
                    if endpoint and endpoint in existing:
                        conn.execute("DELETE FROM players WHERE id=?", (player["id"],))
                    else:
                        conn.execute(
                            "UPDATE players SET source_id=? WHERE id=?", (keep_id, player["id"])
                        )
                # Keep old queued writebacks when there is no corresponding
                # canonical work/source pair. A collision is retained in the
                # canonical entry, rather than replacing an already pending edit.
                for row in conn.execute(
                    "SELECT id,work_id,changes FROM writeback_pending WHERE source_id=?", (old_id,)
                ).fetchall():
                    collision = conn.execute(
                        "SELECT id,changes FROM writeback_pending WHERE work_id=? AND source_id=?",
                        (row["work_id"], keep_id),
                    ).fetchone()
                    if collision:
                        existing = json.loads(collision["changes"])
                        for key, value in json.loads(row["changes"]).items():
                            existing.setdefault(key, value)
                        conn.execute("UPDATE writeback_pending SET changes=? WHERE id=?",
                                     (encode(existing), collision["id"]))
                        conn.execute("DELETE FROM writeback_pending WHERE id=?", (row["id"],))
                    else:
                        conn.execute("UPDATE writeback_pending SET source_id=? WHERE id=?",
                                     (keep_id, row["id"]))
                conn.execute("UPDATE writeback_audit SET source_id=? WHERE source_id=?",
                             (keep_id, old_id))
                conn.execute("DELETE FROM sources WHERE id=?", (old_id,))
                removed_sources += 1
            # SQLite FK cascades clean normal orphans. Legacy databases can
            # also contain dangling rows written while FK checks were disabled.
            conn.execute("DELETE FROM records WHERE source_id NOT IN (SELECT id FROM sources) "
                         "OR work_id NOT IN (SELECT id FROM works)")
            conn.execute("DELETE FROM players WHERE source_id NOT IN (SELECT id FROM sources)")
            conn.execute("DELETE FROM list_items WHERE list_id NOT IN (SELECT id FROM lists) "
                         "OR work_id NOT IN (SELECT id FROM works)")
            self._prune(conn)
            # Make search indexes consistent after collapsing duplicate works.
            for table in ("works_fts", "works_title_fts"):
                conn.execute(f"DELETE FROM {table} WHERE work_id NOT IN (SELECT id FROM works)")
            stale = conn.execute("PRAGMA foreign_key_check").fetchall()
            if stale:
                raise RuntimeError("Foreign-key errors remain; maintenance rolled back.")
        return {
            "status": "cleaned",
            "merged_sources": removed_sources,
            "merged_records": collapsed_records,
            "ambiguous_sources_skipped": skipped_ambiguous,
        }

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _playback_location_view(conn, row):
        value = dict(row)
        option = conn.execute(
            "SELECT public_config,password_cipher FROM playback_location_options "
            "WHERE location_id=?",
            (value["id"],),
        ).fetchone()
        value.update(json.loads(option["public_config"]) if option else {})
        value["has_password"] = bool(option and option["password_cipher"])
        return value

    def playback_locations(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM playback_locations ORDER BY name COLLATE NOCASE,id"
            ).fetchall()
            return [self._playback_location_view(conn, row) for row in rows]

    def playback_location(self, location_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM playback_locations WHERE id=?", (location_id,)
            ).fetchone()
            return self._playback_location_view(conn, row) if row else None

    def save_playback_location(
        self,
        data: dict[str, Any],
        location_id: str | None = None,
        password_cipher: str | None = None,
    ) -> dict[str, Any]:
        public = {k: data.get(k) for k in ("host", "hostname", "port", "share", "username", "workgroup")}
        timestamp = now()
        with self.connect() as conn:
            if location_id is None:
                location_id = str(uuid4())
                conn.execute(
                    "INSERT INTO playback_locations "
                    "(id,name,kind,target,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (location_id, data["name"], data["kind"], data["target"], timestamp, timestamp),
                )
            else:
                current = conn.execute(
                    "SELECT kind FROM playback_locations WHERE id=?", (location_id,)
                ).fetchone()
                if current is None:
                    raise KeyError(location_id)
                conn.execute(
                    "UPDATE playback_locations SET name=?,kind=?,target=?,updated_at=? WHERE id=?",
                    (data["name"], data["kind"], data["target"], timestamp, location_id),
                )
                if current["kind"] != data["kind"]:
                    conn.execute(
                        "DELETE FROM playback_cloud_credentials WHERE location_id=?",
                        (location_id,),
                    )
            conn.execute(
                "INSERT INTO playback_location_options(location_id,public_config,password_cipher) "
                "VALUES(?,?,?) ON CONFLICT(location_id) DO UPDATE SET "
                "public_config=excluded.public_config, "
                "password_cipher=COALESCE(excluded.password_cipher,"
                "playback_location_options.password_cipher)",
                (location_id, encode(public), password_cipher),
            )
            row = conn.execute(
                "SELECT * FROM playback_locations WHERE id=?", (location_id,)
            ).fetchone()
            return self._playback_location_view(conn, row)

    def delete_playback_location(self, location_id: str) -> None:
        with self.connect() as conn:
            deleted = conn.execute("DELETE FROM playback_locations WHERE id=?", (location_id,))
            if deleted.rowcount == 0:
                raise KeyError(location_id)

    def set_playback_cloud_token(self, location_id: str, provider: str, encrypted: str):
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO playback_cloud_credentials(location_id,provider,token_cipher,updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(location_id) DO UPDATE SET "
                "provider=excluded.provider,token_cipher=excluded.token_cipher,"
                "updated_at=excluded.updated_at",
                (location_id, provider, encrypted, now()),
            )

    def playback_oauth_apps(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT provider,client_id,redirect_url,"
                    "(secret_cipher IS NOT NULL) AS has_secret,updated_at "
                    "FROM playback_oauth_apps ORDER BY provider"
                )
            ]

    def playback_oauth_app(self, provider: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM playback_oauth_apps WHERE provider=?", (provider,)
            ).fetchone()
            return dict(row) if row else None

    def set_playback_oauth_app(
        self,
        provider: str,
        client_id: str,
        redirect_url: str,
        secret_cipher: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO playback_oauth_apps"
                "(provider,client_id,secret_cipher,redirect_url,updated_at)"
                "VALUES(?,?,?,?,?) ON CONFLICT(provider) DO UPDATE SET "
                "client_id=excluded.client_id,redirect_url=excluded.redirect_url,"
                "secret_cipher=CASE WHEN playback_oauth_apps.client_id<>excluded.client_id "
                "THEN excluded.secret_cipher ELSE COALESCE(excluded.secret_cipher,"
                "playback_oauth_apps.secret_cipher) END,"
                "updated_at=excluded.updated_at",
                (provider, client_id, secret_cipher, redirect_url, now()),
            )

    def playback_cloud_credentials(self, location_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT provider,token_cipher FROM playback_cloud_credentials WHERE location_id=?",
                (location_id,),
            ).fetchone()
            return dict(row) if row else None

    def playback_cloud_status(self, location_id: str) -> bool:
        with self.connect() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM playback_cloud_credentials WHERE location_id=?",
                    (location_id,),
                ).fetchone()
                is not None
            )

    def delete_playback_cloud_token(self, location_id: str):
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM playback_cloud_credentials WHERE location_id=?", (location_id,)
            )

    def device_tokens(self) -> list[dict[str, str]]:
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT token_hash,name,role,created_at FROM device_tokens ORDER BY created_at DESC"
                )
            ]

    def register_device_token(self, token_hash: str, name: str, role: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO device_tokens(token_hash,name,role,created_at) VALUES(?,?,?,?)",
                (token_hash, name, role, now()),
            )

    def revoke_device_token(self, token_hash: str) -> bool:
        with self.connect() as conn:
            return bool(
                conn.execute("DELETE FROM device_tokens WHERE token_hash=?", (token_hash,)).rowcount
            )

    def sources(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT s.*,COUNT(r.id) AS item_count,"
                "(SELECT COUNT(*) FROM music_album_editions e WHERE e.source_id=s.id) AS music_count "
                "FROM sources s LEFT JOIN records r ON r.source_id=s.id GROUP BY s.id ORDER BY s.name"
            ).fetchall()
            return [self._source_view(r) for r in rows]

    @staticmethod
    def _source_view(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        config = json.loads(value["config"])
        config["has_password"] = bool(config.pop("password", ""))
        config["has_remote_api_key"] = bool(config.pop("remote_api_key", ""))
        value["config"] = config
        value["enabled"] = bool(value["enabled"])
        return value

    def source(self, source_id: str, *, private: bool = False) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT s.*, (SELECT COUNT(*) FROM records r WHERE r.source_id=s.id) AS item_count,"
                " (SELECT COUNT(*) FROM music_album_editions e WHERE e.source_id=s.id) AS music_count"
                " FROM sources s WHERE id=?",
                (source_id,),
            ).fetchone()
            if row is None:
                return None
            if private:
                value = dict(row)
                value["config"] = json.loads(value["config"])
                value["enabled"] = bool(value["enabled"])
                return value
            return self._source_view(row)

    def save_source(self, data: dict[str, Any], source_id: str | None = None) -> dict[str, Any]:
        source_id = source_id or str(uuid4())
        config = dict(data.get("config", {}))
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT type,config FROM sources WHERE id=?", (source_id,)
            ).fetchone()
            if existing and existing["type"] != data["type"]:
                raise ValueError("The source type cannot be changed.")
            config.pop("write_username", None)
            config.pop("write_password", None)
            old_config = json.loads(existing["config"]) if existing else {}
            for secret in ("password", "remote_api_key"):
                if config.get(secret) is None:
                    config[secret] = old_config.get(secret, "")
            conn.execute(
                "INSERT INTO sources(id,name,type,enabled,config) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,enabled=excluded.enabled,config=excluded.config",
                (
                    source_id,
                    data["name"],
                    data["type"],
                    bool(data.get("enabled", True)),
                    encode(config),
                ),
            )
        return self.source(source_id)

    def delete_source(self, source_id: str) -> None:
        with self.connect() as conn:
            affected = [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT work_id FROM records WHERE source_id=?", (source_id,)
                )
            ]
            from .music import MusicCatalogue

            MusicCatalogue(self).remove_source(conn, source_id)
            conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
            for work_id in affected:
                self._rebuild(conn, work_id)
            self._prune(conn)

    def has_multiple_kodi_movie_versions(self, source_id: str) -> bool:
        """Protect SQL-imported movie editions not enumerable by standard JSON-RPC."""
        with self.connect() as conn:
            keys = [
                row[0]
                for row in conn.execute(
                    "SELECT external_key FROM records WHERE source_id=? "
                    "AND external_key LIKE 'movie:%:%'",
                    (source_id,),
                )
            ]
        movies = set()
        for key in keys:
            movie_id = key.split(":")[1]
            if movie_id in movies:
                return True
            movies.add(movie_id)
        return False

    def apply_snapshot(self, source_id: str, records: list[Record]) -> dict[str, Any]:
        """Atomic replacement per source; stable edition IDs across subsequent imports."""
        with self.connect() as conn:
            source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
            if not source:
                raise ValueError("Source no longer exists.")
            old = {
                r["external_key"]: r
                for r in conn.execute("SELECT * FROM records WHERE source_id=?", (source_id,))
            }
            # Only changed/removed works need their metadata and FTS rebuilt.
            # A no-op snapshot must not rewrite thousands of SQLite rows.
            affected = set()
            seen = set()
            resolved = {}
            inserted = updated = unchanged = conflicts = reassigned = 0
            # Parents first ensures episode links are available in the same import.
            for record in sorted(records, key=lambda r: r.kind == "episode"):
                if record.external_key in seen:
                    raise ValueError("The source snapshot contains duplicate identifiers.")
                seen.add(record.external_key)
                payload = asdict(record)
                existing = old.get(record.external_key)
                if existing:
                    work_id, record_id = existing["work_id"], existing["id"]
                    # Identifiers can change after a rescrape: never overwrite metadata
                    # on a previously linked CLZ work if the Kodi IDs were recycled.
                    previous = json.loads(existing["payload"])
                    changed_identity = previous["kind"] != record.kind or any(
                        k in record.identifiers and v != record.identifiers[k]
                        for k, v in previous.get("identifiers", {}).items()
                    )
                    if changed_identity:
                        work_id, conflict = self._match_work(
                            conn, record, exclude=work_id, source_type=source["type"]
                        )
                        conflicts += conflict
                        affected.add(existing["work_id"])
                        reassigned += work_id != existing["work_id"]
                    updated += 1
                else:
                    work_id, conflict = self._match_work(conn, record, source_type=source["type"])
                    record_id = str(uuid4())
                    conflicts += conflict
                    inserted += 1
                resolved[record.external_key] = work_id
                if record.parent_key:
                    payload["parent_id"] = resolved.get(record.parent_key)
                    if not payload["parent_id"]:
                        parent = conn.execute(
                            "SELECT work_id FROM records WHERE source_id=? AND external_key=?",
                            (source_id, record.parent_key),
                        ).fetchone()
                        payload["parent_id"] = parent[0] if parent else None
                encoded = encode(payload)
                if existing and existing["work_id"] == work_id and existing["payload"] == encoded:
                    unchanged += 1
                    continue
                affected.add(work_id)
                conn.execute(
                    "INSERT INTO records(id,work_id,source_id,external_key,payload) VALUES(?,?,?,?,?) ON CONFLICT(source_id,external_key) DO UPDATE SET work_id=excluded.work_id,payload=excluded.payload",
                    (record_id, work_id, source_id, record.external_key, encoded),
                )
                # Register immediately so two physical editions in this same import
                # can resolve to one work. Rebuild removes obsolete identifiers.
                for namespace, value in record.identifiers.items():
                    conn.execute(
                        "INSERT OR IGNORE INTO identifiers VALUES(?,?,?,?)",
                        (work_id, record.kind, namespace, value),
                    )
            deleted = 0
            removed_work_ids = set()
            removed_titles = []
            removed_title_set = set()
            for key, row in old.items():
                if key not in seen:
                    conn.execute("DELETE FROM records WHERE id=?", (row["id"],))
                    removed_work_ids.add(row["work_id"])
                    affected.add(row["work_id"])
                    title = json.loads(row["payload"]).get("title") or key
                    if title not in removed_title_set:
                        removed_title_set.add(title)
                        if len(removed_titles) < 20:
                            removed_titles.append(title)
                    deleted += 1
            # Assert a complete source snapshot, including removals. A failure
            # rolls back the transaction rather than publishing a stale edition.
            actual_keys = {
                row[0]
                for row in conn.execute(
                    "SELECT external_key FROM records WHERE source_id=?", (source_id,)
                )
            }
            if actual_keys != seen:
                raise RuntimeError("Snapshot reconciliation mismatch; transaction rolled back.")
            for work_id in affected:
                self._rebuild(conn, work_id)
            if deleted or reassigned:
                self._prune(conn)
            # Includes legacy works imported before title matching existed. Only
            # perform the scan if the source snapshot has actual changes.
            if affected:
                self._reconcile_titles(conn)

            # An absent Kodi edition may have been the last source of a work,
            # or one of several editions of a film also owned in CLZ.
            removed_works = kept_clz_works = 0
            if source["type"] == "kodi":
                for work_id in removed_work_ids:
                    remaining_types = {
                        row[0]
                        for row in conn.execute(
                            "SELECT DISTINCT s.type FROM records r "
                            "JOIN sources s ON s.id=r.source_id WHERE r.work_id=?",
                            (work_id,),
                        )
                    }
                    if not remaining_types:
                        removed_works += 1
                    elif remaining_types == {"clz"}:
                        kept_clz_works += 1

            conn.execute(
                "UPDATE sources SET last_sync=?,last_error=NULL WHERE id=?", (now(), source_id)
            )
            return {
                "imported": len(records),
                "created_editions": inserted,
                "updated_editions": updated,
                "unchanged_editions": unchanged,
                "rebuilt_works": len(affected),
                "removed_editions": deleted,
                "removed_titles": removed_titles,
                "removed_titles_total": len(removed_title_set),
                "removed_works": removed_works,
                "kept_clz_works": kept_clz_works,
                "source_records_before": len(old),
                "source_records_after": len(actual_keys),
                "matching_conflicts": conflicts,
            }

    @staticmethod
    def _title_keys(title: str | None, original: str | None = None) -> set[str]:
        return {
            key
            for name in (title, original)
            if name and (key := " ".join(re.findall(r"\w+", fold(name))))
        }

    @staticmethod
    def _ids_compatible(first: dict[str, set[str]], second: dict[str, set[str]]) -> bool:
        return not any(
            namespace in second and not values.intersection(second[namespace])
            for namespace, values in first.items()
        )

    @staticmethod
    def _work_ids(conn: sqlite3.Connection, work_id: str) -> dict[str, set[str]]:
        result: dict[str, set[str]] = defaultdict(set)
        for row in conn.execute(
            "SELECT namespace,value FROM identifiers WHERE work_id=?", (work_id,)
        ):
            result[row["namespace"]].add(row["value"])
        return result

    @classmethod
    def _match_work(
        cls,
        conn: sqlite3.Connection,
        record: Record,
        exclude: str | None = None,
        source_type: str | None = None,
    ) -> tuple[str, int]:
        candidates = set()
        for namespace, value in record.identifiers.items():
            candidates.update(
                r[0]
                for r in conn.execute(
                    "SELECT work_id FROM identifiers WHERE kind=? AND namespace=? AND value=?",
                    (record.kind, namespace, value),
                )
                if r[0] != exclude
            )
        new_ids = {key: {value} for key, value in record.identifiers.items()}
        if len(candidates) == 1:
            candidate = next(iter(candidates))
            if cls._ids_compatible(new_ids, cls._work_ids(conn, candidate)):
                return candidate, 0

        # No common external ID: only an *unambiguous* exact title (localized
        # or original) AND a matching year can join entries from different providers.
        # Never use this fallback after conflicting/ambiguous identifier matches.
        if (
            not candidates
            and source_type in SOURCE_TYPES
            and record.kind == "movie"
            and record.year is not None
        ):
            keys = cls._title_keys(record.title, record.original_title)
            matches = []
            if keys:
                for row in conn.execute(
                    "SELECT id,title,original_title FROM works WHERE kind=? AND year=?",
                    (record.kind, record.year),
                ):
                    if row["id"] == exclude or not keys.intersection(
                        cls._title_keys(row["title"], row["original_title"])
                    ):
                        continue
                    kinds = {
                        entry[0]
                        for entry in conn.execute(
                            "SELECT DISTINCT s.type FROM records r "
                            "JOIN sources s ON s.id=r.source_id WHERE r.work_id=?",
                            (row["id"],),
                        )
                    }
                    if source_type in kinds or not kinds.intersection(SOURCE_TYPES):
                        continue
                    if cls._ids_compatible(new_ids, cls._work_ids(conn, row["id"])):
                        matches.append(row["id"])
            if len(matches) == 1:
                return matches[0], 0

        work_id = str(uuid4())
        conn.execute("INSERT INTO works(id,kind) VALUES(?,?)", (work_id, record.kind))
        return work_id, int(bool(candidates))

    @classmethod
    def _reconcile_titles(cls, conn: sqlite3.Connection) -> int:
        """Safely reunite works from different providers using IDs or titles/years.

        Skip ambiguous titles, conflicting identifiers and same-provider pairs.
        Preserve notes, watch overrides, playlist membership and the surviving ID.
        """
        rows = conn.execute(
            "SELECT id,title,original_title,year FROM works WHERE kind='movie' AND year IS NOT NULL"
        ).fetchall()
        if not rows:
            return 0
        types: dict[str, set[str]] = defaultdict(set)
        for row in conn.execute(
            "SELECT DISTINCT r.work_id,s.type FROM records r JOIN sources s ON s.id=r.source_id"
        ):
            types[row["work_id"]].add(row["type"])
        lookup: dict[tuple[int, str], set[str]] = defaultdict(set)
        for row in rows:
            if len(types[row["id"]]) != 1 or not types[row["id"]].issubset(SOURCE_TYPES):
                continue
            for key in cls._title_keys(row["title"], row["original_title"]):
                lookup[(row["year"], key)].add(row["id"])
        title_pairs = set()
        for ids in lookup.values():
            if len(ids) != 2:
                continue
            left, right = sorted(ids)
            if types[left] != types[right]:
                title_pairs.add((left, right))

        # A strong IMDb/TMDb/TVDb identity must also reconcile works that were
        # imported years ago with different titles or years. Never merge an
        # identifier with more than two separate work identities.
        identities: dict[tuple[str, str, str], set[str]] = defaultdict(set)
        for row in conn.execute(
            "SELECT i.kind,i.namespace,i.value,i.work_id FROM identifiers i "
            "JOIN works w ON w.id=i.work_id"
        ):
            if len(types[row["work_id"]]) == 1 and types[row["work_id"]].issubset(SOURCE_TYPES):
                identities[(row["kind"], row["namespace"], row["value"])].add(row["work_id"])
        id_pairs = set()
        for ids in identities.values():
            if len(ids) == 2:
                left, right = sorted(ids)
                if types[left] != types[right]:
                    id_pairs.add((left, right))

        # Ambiguous matches are never resolved arbitrarily. Identifier matches
        # take precedence over title/year matches and can resolve ambiguity.
        from collections import Counter

        def unambiguous(pairs):
            counts = Counter(work_id for pair in pairs for work_id in pair)
            return sorted(pair for pair in pairs if all(counts[work_id] == 1 for work_id in pair))

        used = set()
        merged = 0
        for left, right in unambiguous(id_pairs) + unambiguous(title_pairs):
            if left in used or right in used:
                continue
            if not cls._ids_compatible(
                cls._work_ids(conn, left), cls._work_ids(conn, right)
            ) or not cls._ids_compatible(cls._work_ids(conn, right), cls._work_ids(conn, left)):
                continue
            # Keep the identity already referenced by playlists, then imported collections.
            priority = {}
            for work_id in (left, right):
                priority[work_id] = (
                    conn.execute(
                        "SELECT COUNT(*) FROM list_items WHERE work_id=?", (work_id,)
                    ).fetchone()[0],
                    int(
                        conn.execute(
                            "SELECT 1 FROM personal WHERE work_id=?", (work_id,)
                        ).fetchone()
                        is not None
                    ),
                    int(bool(types[work_id].intersection(COLLECTION_TYPES))),
                )
            keep = max((left, right), key=lambda work_id: priority[work_id])
            drop = right if keep == left else left
            affected_lists = [
                row[0]
                for row in conn.execute("SELECT list_id FROM list_items WHERE work_id=?", (drop,))
            ]
            conn.execute(
                "INSERT OR IGNORE INTO list_items(list_id,work_id,position) "
                "SELECT list_id,?,position FROM list_items WHERE work_id=?",
                (keep, drop),
            )
            if affected_lists:
                conn.executemany(
                    "UPDATE lists SET revision=revision+1 WHERE id=?",
                    [(list_id,) for list_id in affected_lists],
                )
            former = conn.execute(
                "SELECT user_rating,watched FROM personal WHERE work_id=?", (drop,)
            ).fetchone()
            if former:
                conn.execute(
                    "INSERT OR IGNORE INTO personal(work_id,user_rating,watched) VALUES(?,?,?)",
                    (keep, former["user_rating"], former["watched"]),
                )
                conn.execute(
                    "UPDATE personal SET user_rating=COALESCE(user_rating,?),"
                    "watched=COALESCE(watched,?) WHERE work_id=?",
                    (former["user_rating"], former["watched"], keep),
                )
            conn.execute("UPDATE records SET work_id=? WHERE work_id=?", (keep, drop))
            conn.execute("DELETE FROM identifiers WHERE work_id=?", (drop,))
            conn.execute("DELETE FROM works_fts WHERE work_id=?", (drop,))
            conn.execute("DELETE FROM works_title_fts WHERE work_id=?", (drop,))
            conn.execute("DELETE FROM works WHERE id=?", (drop,))
            cls._rebuild(conn, keep)
            used.update((left, right))
            merged += 1
        return merged

    @staticmethod
    def _rebuild(conn: sqlite3.Connection, work_id: str) -> None:
        rows = conn.execute(
            "SELECT r.payload,s.type FROM records r JOIN sources s ON s.id=r.source_id WHERE r.work_id=?",
            (work_id,),
        ).fetchall()
        conn.execute("DELETE FROM identifiers WHERE work_id=?", (work_id,))
        conn.execute("DELETE FROM work_person_ids WHERE work_id=?", (work_id,))
        conn.execute("DELETE FROM work_person_names WHERE work_id=?", (work_id,))
        conn.execute("DELETE FROM work_person_roles WHERE work_id=?", (work_id,))
        conn.execute("DELETE FROM works_fts WHERE work_id=?", (work_id,))
        conn.execute("DELETE FROM works_title_fts WHERE work_id=?", (work_id,))
        if not rows:
            return
        entries = [(json.loads(row["payload"]), row["type"]) for row in rows]
        entries.sort(
            key=lambda p: (p[1] != "kodi", not p[0].get("metadata", {}).get("is_default", True))
        )
        primary = entries[0][0]

        def choose(name: str):
            return next(
                (p[name] for p, _ in entries if p.get(name) not in (None, "", [], {})), None
            )

        known_watch = [p for p, _ in entries if p.get("watched") is not None]
        kodi_watch = [p for p, t in entries if t == "kodi" and p.get("watched") is not None]
        progress = next(
            (p for p in kodi_watch if not p["watched"] and p.get("resume_seconds", 0) > 0), None
        )
        partial_show = any(
            p.get("metadata", {}).get("watched_episodes", 0) and not p["watched"]
            for p in known_watch
        )
        status = (
            "watched"
            if any(p["watched"] for p in known_watch)
            else "in_progress"
            if progress or partial_show
            else "unwatched"
            if known_watch
            else "unknown"
        )
        playback = progress or (kodi_watch[0] if kodi_watch else {})
        genres = sorted({v for p, _ in entries for v in p.get("genres", [])}, key=fold)
        fmts = sorted(
            {
                canonical_source_format(v, source_type)
                for p, source_type in entries
                for v in p.get("formats", [])
            },
            key=fold,
        )
        collections = sorted({v for p, _ in entries for v in p.get("collections", [])}, key=fold)
        types = sorted({t for _, t in entries})
        search_text = " ".join(
            str(p.get(n) or "") for p, _ in entries for n in ("title", "original_title", "plot")
        )
        search_text += " " + " ".join(genres + collections)
        search_text += " " + " ".join(
            str(p.get("metadata", {}).get(n) or "")
            for p, _ in entries
            for n in ("director", "show_title", "tags", "cast", "people")
        )
        enrichment = conn.execute(
            "SELECT payload FROM enrichment WHERE work_id=?", (work_id,)
        ).fetchone()
        if enrichment:
            search_text += " " + " ".join(
                p["name"] for p in json.loads(enrichment[0]).get("people", [])
            )
        for payload, _ in entries:
            metadata = payload.get("metadata") or {}
            if isinstance(metadata, dict):
                for key in ("people", "cast"):
                    conn.executemany(
                        "INSERT OR IGNORE INTO work_person_ids(namespace,person_id,work_id)"
                        " VALUES(?,?,?)",
                        person_ids(work_id, metadata.get(key)),
                    )
                    conn.executemany(
                        "INSERT OR IGNORE INTO work_person_names"
                        "(work_id,name_fold,tmdb_id,imdb_id) VALUES(?,?,?,?)",
                        person_names(work_id, metadata.get(key)),
                    )
                    conn.executemany(
                        "INSERT OR IGNORE INTO work_person_roles VALUES(?,?,?)",
                        person_roles(work_id, metadata.get(key)),
                    )
        for payload, _ in entries:
            directors = (payload.get("metadata") or {}).get("director") or ""
            if isinstance(directors, str):
                conn.executemany(
                    "INSERT OR IGNORE INTO work_person_roles VALUES(?,?,?)",
                    (
                        (work_id, fold(name.strip()), "director")
                        for name in directors.split(" / ")
                        if name.strip()
                    ),
                )
        if enrichment:
            conn.executemany(
                "INSERT OR IGNORE INTO work_person_roles VALUES(?,?,?)",
                person_roles(work_id, json.loads(enrichment[0]).get("people")),
            )
            conn.executemany(
                "INSERT OR IGNORE INTO work_person_ids(namespace,person_id,work_id) VALUES(?,?,?)",
                person_ids(work_id, json.loads(enrichment[0]).get("people")),
            )
            conn.executemany(
                "INSERT OR IGNORE INTO work_person_names"
                "(work_id,name_fold,tmdb_id,imdb_id) VALUES(?,?,?,?)",
                person_names(work_id, json.loads(enrichment[0]).get("people")),
            )
        posters = list(dict.fromkeys(v for p, _ in entries for v in p.get("poster", [])))
        fanarts = list(dict.fromkeys(v for p, _ in entries for v in p.get("fanart", [])))
        values = {
            "kind": primary["kind"],
            "title": choose("title"),
            "sort_title": fold(choose("sort_title") or choose("title") or ""),
            **{
                n: choose(n)
                for n in (
                    "original_title",
                    "year",
                    "plot",
                    "runtime_minutes",
                    "rating",
                    "user_rating",
                    "date_added",
                    "parent_id",
                    "season",
                    "episode",
                )
            },
            "last_played": max(
                (p["last_played"] for p, _ in entries if p.get("last_played")), default=None
            ),
            "watch_status": status,
            "play_count": max((p.get("play_count") or 0 for p in known_watch), default=None),
            "resume_seconds": playback.get("resume_seconds", 0),
            "total_seconds": playback.get("total_seconds", 0),
            "genres": encode(genres),
            "formats": encode(fmts),
            "collections": encode(collections),
            "source_types": encode(types),
            "poster": encode(posters),
            "fanart": encode(fanarts),
            "search_text": fold(search_text),
        }
        personal = conn.execute("SELECT * FROM personal WHERE work_id=?", (work_id,)).fetchone()
        if personal:
            if personal["user_rating"] is not None:
                values["user_rating"] = personal["user_rating"]
            if personal["watched"] is not None:
                values["watch_status"] = "watched" if personal["watched"] else "unwatched"
                values["resume_seconds"] = 0
        conn.execute(
            "UPDATE works SET " + ",".join(f"{n}=?" for n in values) + " WHERE id=?",
            (*values.values(), work_id),
        )
        conn.execute(
            "INSERT INTO works_fts(work_id,search_text) VALUES(?,?)",
            (work_id, values["search_text"]),
        )
        title_text = " ".join(
            str(p.get(name) or "") for p, _ in entries for name in ("title", "original_title")
        )
        conn.execute(
            "INSERT INTO works_title_fts(work_id,titles) VALUES(?,?)",
            (work_id, fold(title_text)),
        )
        for payload, _ in entries:
            for namespace, value in payload.get("identifiers", {}).items():
                conn.execute(
                    "INSERT OR IGNORE INTO identifiers VALUES(?,?,?,?)",
                    (work_id, primary["kind"], namespace, value),
                )

    @staticmethod
    def _prune(conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE lists SET revision=revision+1 WHERE id IN (SELECT i.list_id FROM list_items i WHERE NOT EXISTS(SELECT 1 FROM records r WHERE r.work_id=i.work_id))"
        )
        conn.execute(
            "DELETE FROM works_fts WHERE work_id IN (SELECT id FROM works w WHERE NOT EXISTS(SELECT 1 FROM records r WHERE r.work_id=w.id))"
        )
        conn.execute(
            "DELETE FROM works_title_fts WHERE work_id IN (SELECT id FROM works w WHERE NOT EXISTS(SELECT 1 FROM records r WHERE r.work_id=w.id))"
        )
        conn.execute(
            "DELETE FROM works WHERE NOT EXISTS(SELECT 1 FROM records r WHERE r.work_id=works.id)"
        )

    @staticmethod
    def _summary(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        for name in ("genres", "formats", "collections", "source_types"):
            value[name] = json.loads(value[name])
        value["formats"] = sorted({canonical_format(v) for v in value["formats"]}, key=fold)
        value["artwork"] = {
            name: f"/api/v1/media/{value['id']}/artwork/{name}" if json.loads(value[name]) else None
            for name in ("poster", "fanart")
        }
        for name in ("poster", "fanart", "search_text", "sort_title"):
            value.pop(name, None)
        return value

    @staticmethod
    def _sources_for_works(conn: sqlite3.Connection, work_ids: list[str]) -> dict[str, list[dict]]:
        """Fetch exact source names in a single query for a page (not one query per item)."""
        if not work_ids:
            return {}
        slots = ",".join("?" for _ in work_ids)
        type_counts = {
            row["type"]: row["count"]
            for row in conn.execute("SELECT type,COUNT(*) AS count FROM sources GROUP BY type")
        }
        rows = conn.execute(
            "SELECT DISTINCT r.work_id,s.id,s.name,s.type "
            "FROM records r JOIN sources s ON s.id=r.source_id "
            f"WHERE r.work_id IN ({slots}) "
            "ORDER BY s.type,s.name COLLATE NOCASE,s.id",
            work_ids,
        )
        found: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            found[row["work_id"]].append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "type": row["type"],
                    "type_count": type_counts[row["type"]],
                }
            )
        return found

    @staticmethod
    def _series_episode_progress(
        conn: sqlite3.Connection, series_ids: list[str]
    ) -> dict[str, dict[str, int]]:
        """Count imported episodes in one indexed query for each catalogue page.

        Use the effective watched state of every episode (including personal
        overrides), not stale Kodi series-level watch metadata.
        """
        if not series_ids:
            return {}
        placeholders = ",".join("?" for _ in series_ids)
        rows = conn.execute(
            "SELECT parent_id,COUNT(*) AS episode_count,"
            " SUM(CASE WHEN watch_status='watched' THEN 1 ELSE 0 END) AS watched_episode_count"
            " FROM works WHERE kind='episode' AND parent_id IN ("
            + placeholders
            + ") GROUP BY parent_id",
            series_ids,
        )
        return {
            row["parent_id"]: {
                "episode_count": row["episode_count"],
                "watched_episode_count": row["watched_episode_count"],
            }
            for row in rows
        }

    @staticmethod
    def _latest_playback_resumes(
        conn: sqlite3.Connection, work_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Load newest resume per work in one query for a catalogue page."""
        if not work_ids:
            return {}
        placeholders = ",".join("?" for _ in work_ids)
        rows = conn.execute(
            "SELECT work_id,position_seconds,total_seconds,updated_at"
            " FROM playback_resume WHERE work_id IN (" + placeholders + ")"
            " ORDER BY updated_at DESC,edition_id",
            work_ids,
        )
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            latest.setdefault(row["work_id"], dict(row))
        return latest

    @staticmethod
    def _apply_resume_to_summary(
        item: dict[str, Any], resume: dict[str, Any] | None
    ) -> dict[str, Any]:
        if resume is None:
            return item
        item["resume_seconds"] = resume["position_seconds"]
        item["total_seconds"] = resume["total_seconds"]
        item["playback_updated_at"] = resume["updated_at"]
        if (
            item["watch_status"] != "watched"
            and 0 < resume["position_seconds"] < resume["total_seconds"]
        ):
            item["watch_status"] = "in_progress"
        return item

    @classmethod
    def _summaries_with_sources(
        cls, conn: sqlite3.Connection, rows: list[sqlite3.Row]
    ) -> list[dict[str, Any]]:
        ids = [row["id"] for row in rows]
        by_work = cls._sources_for_works(conn, ids)
        series_ids = [row["id"] for row in rows if row["kind"] == "tvshow"]
        progress = cls._series_episode_progress(conn, series_ids)
        resumes = cls._latest_playback_resumes(conn, ids)
        # Series posters must also reflect a partially played, still-unwatched
        # episode, even before Kodi increments its watched-episode counter.
        series_resumes: dict[str, dict[str, Any]] = {}
        if series_ids:
            placeholders = ",".join("?" for _ in series_ids)
            episodes = conn.execute(
                "SELECT e.parent_id,p.position_seconds,p.total_seconds,p.updated_at"
                " FROM playback_resume p JOIN works e ON e.id=p.work_id"
                " WHERE e.kind='episode' AND e.watch_status!='watched'"
                " AND e.parent_id IN (" + placeholders + ")"
                " AND p.position_seconds>0 AND p.total_seconds>p.position_seconds"
                " ORDER BY p.updated_at DESC,p.edition_id",
                series_ids,
            )
            for episode in episodes:
                series_resumes.setdefault(episode["parent_id"], dict(episode))
        items = []
        for row in rows:
            item = cls._apply_resume_to_summary(
                cls._summary(row), resumes.get(row["id"])
            )
            item.update(progress.get(row["id"], {}))
            item["media_sources"] = by_work.get(row["id"], [])
            partial = series_resumes.get(row["id"])
            if partial and item["watch_status"] != "watched":
                item["watch_status"] = "in_progress"
                item["playback_updated_at"] = partial["updated_at"]
                count = item.get("episode_count") or 0
                if count:
                    fraction = partial["position_seconds"] / partial["total_seconds"]
                    item["series_resume_progress"] = min(
                        1.0, ((item.get("watched_episode_count") or 0) + fraction) / count
                    )
            items.append(item)
        return items

    @staticmethod
    def _matched_person_roles(
        conn: sqlite3.Connection,
        work_ids: list[str],
        *,
        person_name: str | None,
        person_tmdb_id: str | None,
        person_imdb_id: str | None,
    ) -> dict[str, list[str]]:
        """Extract credits for one person in a page, without N detail queries.

        Work-person identity and name matching follow the same rules as the
        indexed search. Credits without an ID are eligible only when no
        same-name credit claims an incompatible ID on this title.
        """
        if not work_ids:
            return {}
        normalized = fold(person_name.strip()) if person_name else ""
        namespace = (
            "tmdb_id"
            if person_tmdb_id is not None
            else ("imdb_id" if person_imdb_id is not None else None)
        )
        identity = person_tmdb_id if person_tmdb_id is not None else person_imdb_id
        credits: dict[str, list[dict]] = defaultdict(list)
        placeholders = ",".join("?" for _ in work_ids)
        for row in conn.execute(
            f"SELECT work_id,payload FROM records WHERE work_id IN ({placeholders})",
            work_ids,
        ):
            metadata = json.loads(row["payload"]).get("metadata") or {}
            if not isinstance(metadata, dict):
                continue
            for field in ("people", "cast"):
                members = metadata.get(field)
                if isinstance(members, list):
                    credits[row["work_id"]].extend(
                        person for person in members if isinstance(person, dict)
                    )
        for row in conn.execute(
            f"SELECT work_id,payload FROM enrichment WHERE work_id IN ({placeholders})",
            work_ids,
        ):
            members = json.loads(row["payload"]).get("people")
            if isinstance(members, list):
                credits[row["work_id"]].extend(
                    person for person in members if isinstance(person, dict)
                )

        results = {}
        for work_id, members in credits.items():
            conflict = bool(
                namespace
                and normalized
                and any(
                    fold(member.get("name", "")) == normalized
                    and str(member.get(namespace) or "") not in ("", identity)
                    for member in members
                    if isinstance(member.get("name"), str)
                )
            )
            roles = []
            for member in members:
                name = member.get("name")
                if not isinstance(name, str):
                    continue
                named = bool(normalized and fold(name.strip()) == normalized)
                if namespace:
                    known = str(member.get(namespace) or "")
                    matched = known == identity or (named and not known and not conflict)
                else:
                    matched = named
                if not matched:
                    continue
                role = member.get("role")
                if isinstance(role, str) and role.strip() and role.strip() not in roles:
                    roles.append(role.strip())
            if roles:
                results[work_id] = roles
        return results

    def ai_documents(self) -> list[dict[str, Any]]:
        """One embedding per work, including credited people and both available ratings."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT w.id,w.kind,w.title,w.plot,w.genres,w.parent_id,w.season,"
                "w.episode,w.watch_status,w.rating,w.user_rating,p.title AS series_title "
                "FROM works w LEFT JOIN works p ON p.id=w.parent_id "
                "WHERE w.kind IN ('movie','tvshow','episode') ORDER BY w.id"
            ).fetchall()
            credits: dict[str, set[tuple[str, str]]] = defaultdict(set)
            for row in conn.execute("SELECT work_id,payload FROM records"):
                metadata = json.loads(row["payload"]).get("metadata") or {}
                if not isinstance(metadata, dict):
                    continue
                for field in ("people", "cast"):
                    members = metadata.get(field)
                    if isinstance(members, list):
                        for member in members:
                            if not isinstance(member, dict):
                                continue
                            name = member.get("name")
                            if not isinstance(name, str) or not name.strip():
                                continue
                            role = str(member.get("category") or "")
                            role = credit_category(
                                role
                                if role not in ("crew", "")
                                else str(member.get("role") or role)
                            )
                            credits[row["work_id"]].add((role, name.strip()))
                directors = metadata.get("director") or ""
                if isinstance(directors, str):
                    credits[row["work_id"]].update(
                        ("director", name.strip())
                        for name in directors.split(" / ")
                        if name.strip()
                    )
            for row in conn.execute("SELECT work_id,payload FROM enrichment"):
                members = json.loads(row["payload"]).get("people") or []
                for member in members:
                    if not isinstance(member, dict):
                        continue
                    name = member.get("name")
                    if not isinstance(name, str) or not name.strip():
                        continue
                    role = str(member.get("category") or "")
                    role = credit_category(
                        role if role not in ("crew", "") else str(member.get("role") or role)
                    )
                    credits[row["work_id"]].add((role, name.strip()))

        result = []
        labels = {
            "cast": "Acteurs",
            "director": "Réalisateurs",
            "composer": "Compositeurs",
            "writer": "Scénaristes",
        }
        for row in rows:
            series = row["series_title"] or ""
            season, episode = row["season"], row["episode"]
            label = (
                f"{series} S{season:02d}E{episode:02d} {row['title']}"
                if row["kind"] == "episode" and season is not None and episode is not None
                else row["title"]
            )
            genres = ", ".join(json.loads(row["genres"] or "[]"))
            plot = (row["plot"] or "")[:5000]
            roles = sorted(credits.get(row["id"], set()), key=lambda item: (item[0], fold(item[1])))
            grouped: dict[str, list[str]] = defaultdict(list)
            for role, name in roles:
                if role in labels:
                    grouped[role].append(name)
            details = [
                f"{labels[role]}: {', '.join(grouped[role][:30])}"
                for role in labels
                if grouped[role]
            ]
            if row["rating"] is not None:
                details.append(f"Note générale: {row['rating']}/10")
            if row["user_rating"] is not None:
                details.append(f"Note personnelle: {row['user_rating']}/10")
            text = f"{label}. Genres: {genres}. Synopsis: {plot}"
            if details:
                text += ". " + ". ".join(details)
            result.append(
                {
                    "id": row["id"],
                    "kind": row["kind"],
                    "parent_id": row["parent_id"],
                    "title": row["title"],
                    "series_title": series,
                    "season": season,
                    "episode": episode,
                    "watch_status": row["watch_status"],
                    "plot": plot,
                    "rating": row["rating"],
                    "user_rating": row["user_rating"],
                    "credits": roles,
                    "text": text.strip(),
                }
            )
        return result

    def ai_person_ids(self, person_name: str | None, person_role: str | None) -> set[str]:
        """SQL-indexed lookup for exact credited person and/or function."""
        if not person_name and not person_role:
            return set()
        conditions = []
        params = []
        if person_name:
            conditions.append("name_fold=?")
            params.append(fold(person_name.strip()))
        if person_role:
            conditions.append("category=?")
            params.append(person_role)
        with self.connect() as conn:
            return {
                row[0]
                for row in conn.execute(
                    "SELECT work_id FROM work_person_roles WHERE " + " AND ".join(conditions),
                    params,
                )
            }

    def ai_lexical_hits(self, text: str, limit: int = 200) -> list[str]:
        """FTS5 indexes individual episode plots as well as films and series."""
        words = re.findall(r"\w+", text, flags=re.UNICODE)[:10]
        if not words:
            return []
        expression = " AND ".join('"' + word.replace('"', '""') + '"*' for word in words)
        with self.connect() as conn:
            return [
                row["work_id"]
                for row in conn.execute(
                    "SELECT work_id FROM works_fts WHERE works_fts MATCH ? "
                    "ORDER BY bm25(works_fts) LIMIT ?",
                    (expression, limit),
                )
            ]

    def media(
        self,
        *,
        q: str = "",
        deep_search: bool = False,
        person_tmdb_id: str | None = None,
        person_imdb_id: str | None = None,
        person_name: str | None = None,
        person_role: str | None = None,
        kind: str | None = None,
        watch_status: str | None = None,
        genre: str | None = None,
        format: str | list[str] | None = None,
        collection: str | None = None,
        source_type: str | None = None,
        source_id: str | None = None,
        parent_id: str | None = None,
        season: int | None = None,
        year: int | None = None,
        year_min: int | None = None,
        year_max: int | None = None,
        rating_min: float | None = None,
        user_rating_min: float | None = None,
        ids: list[str] | None = None,
        recent_days: int | None = None,
        sort: str = "date_added",
        order: str = "desc",
        limit: int = 60,
        offset: int = 0,
    ) -> dict[str, Any]:
        clauses, params = [], []
        # By default, the public catalogue displays works, not their
        # constituent episodes. Explicit episode/parent queries remain available.
        if kind is None and parent_id is None:
            clauses.append("w.kind IN ('movie','tvshow')")
        if ids is not None:
            if len(ids) > 500:
                raise ValueError("Too many selected media IDs.")
            if not ids:
                clauses.append("0=1")
            else:
                clauses.append("w.id IN (" + ",".join("?" for _ in ids) + ")")
                params.extend(ids)
        # Most Kodi cast credits have a name but no TMDB/IMDb identifier.
        # Match known IDs first, then the same normalized credit name only
        # where there is no conflicting identifier for that named credit.
        # A dedicated index keeps this predictable for large local catalogues.
        if person_tmdb_id is not None or person_imdb_id is not None:
            namespace = "tmdb" if person_tmdb_id is not None else "imdb"
            identity = person_tmdb_id if person_tmdb_id is not None else person_imdb_id
            if person_name and person_name.strip():
                normalized = fold(person_name.strip())
                identity_column = "tmdb_id" if namespace == "tmdb" else "imdb_id"
                clauses.append(
                    "(w.id IN (SELECT work_id FROM work_person_ids"
                    " WHERE namespace=? AND person_id=?)"
                    " OR w.id IN (SELECT p.work_id FROM work_person_names p"
                    " WHERE p.name_fold=? AND (p." + identity_column + "=?"
                    " OR (p." + identity_column + "=''"
                    " AND NOT EXISTS (SELECT 1 FROM work_person_names other"
                    " WHERE other.work_id=p.work_id AND other.name_fold=p.name_fold"
                    " AND other." + identity_column + "<>'')))))"
                )
                params.extend((namespace, identity, normalized, identity))
            else:
                clauses.append(
                    "w.id IN (SELECT work_id FROM work_person_ids "
                    "WHERE namespace=? AND person_id=?)"
                )
                params.extend((namespace, identity))
        elif person_name and person_name.strip():
            # Name-only cast searches should not match unrelated plot/title text.
            clauses.append("w.id IN (SELECT work_id FROM work_person_names WHERE name_fold=?)")
            params.append(fold(person_name.strip()))
        if person_role:
            if person_role not in ("cast", "director", "composer", "writer"):
                raise ValueError("Invalid credit role.")
            clauses.append(
                "w.id IN (SELECT work_id FROM work_person_roles WHERE category=?"
                + (" AND name_fold=?" if person_name else "")
                + ")"
            )
            params.append(person_role)
            if person_name:
                params.append(fold(person_name.strip()))
        words = re.findall(r"\w+", q, flags=re.UNICODE)
        if words:
            index = "works_fts" if deep_search else "works_title_fts"
            full_name = fold(q.strip().strip('"')) if deep_search and len(words) > 1 else ""
            # On an advanced name search, prefer exact credits from the
            # normalized person index. A film with "John" in a title and
            # "Williams" in a plot must not be treated as a John Williams film.
            matching_credit = False
            if full_name and not (person_name or person_tmdb_id or person_imdb_id):
                with self.connect() as conn:
                    matching_credit = (
                        conn.execute(
                            "SELECT 1 FROM work_person_names WHERE name_fold=? LIMIT 1",
                            (full_name,),
                        ).fetchone()
                        is not None
                    )
            if matching_credit:
                clauses.append("w.id IN (SELECT work_id FROM work_person_names WHERE name_fold=?)")
                params.append(full_name)
                person_name = q.strip().strip('"')
            else:
                clauses.append(f"w.id IN (SELECT work_id FROM {index} WHERE {index} MATCH ?)")
                # Multiword proper names (e.g. "John Williams") must stay
                # together instead of accidentally matching one token in the
                # title and another in the plot. Other keyword searches
                # intentionally keep flexible AND semantics: "équipe cité"
                # must find a synopsis saying "équipe découvre une cité".
                is_proper_name = (
                    deep_search and len(words) > 1 and all(word[0].isupper() for word in words)
                )
                if is_proper_name:
                    params.append('"' + " ".join(words).replace('"', '""') + '"')
                else:
                    params.append(
                        " AND ".join('"' + word.replace('"', '""') + '"*' for word in words)
                    )
        # A resume bookmark lives separately from imported Kodi snapshots.
        # Apply that effective state to watched filters, not just the poster UI.
        active_playback = (
            "(EXISTS(SELECT 1 FROM playback_resume p WHERE p.work_id=w.id"
            " AND p.updated_at=(SELECT MAX(p2.updated_at) FROM playback_resume p2"
            " WHERE p2.work_id=w.id)"
            " AND p.position_seconds>0 AND p.position_seconds<p.total_seconds)"
            " OR (w.kind='tvshow' AND EXISTS("
            " SELECT 1 FROM playback_resume p JOIN works e ON e.id=p.work_id"
            " WHERE e.parent_id=w.id AND e.kind='episode'"
            " AND e.watch_status!='watched' AND p.position_seconds>0"
            " AND p.position_seconds<p.total_seconds)))"
        )
        for name, value in (
            ("kind", kind),
            ("watch_status", watch_status),
            ("parent_id", parent_id),
            ("season", season),
            ("year", year),
        ):
            if value is None:
                continue
            if name == "watch_status" and value == "in_progress":
                clauses.append(
                    "(w.watch_status='in_progress' OR "
                    "(w.watch_status!='watched' AND " + active_playback + "))"
                )
            elif name == "watch_status" and value == "unwatched":
                clauses.append(
                    "(w.watch_status='unwatched' AND NOT " + active_playback + ")"
                )
            else:
                clauses.append(f"w.{name}=?")
                params.append(value)
        for name, value in (
            ("genres", genre),
            ("collections", collection),
            ("source_types", source_type),
        ):
            if value is not None:
                clauses.append(
                    f"EXISTS(SELECT 1 FROM json_each(w.{name}) WHERE value=? COLLATE NOCASE)"
                )
                params.append(value)
        if year_min is not None:
            clauses.append("w.year>=?")
            params.append(year_min)
        if year_max is not None:
            clauses.append("w.year<=?")
            params.append(year_max)
        if rating_min is not None:
            clauses.append("w.rating>=?")
            params.append(rating_min)
        if user_rating_min is not None:
            clauses.append("w.user_rating>=?")
            params.append(user_rating_min)
        requested_formats = [format] if isinstance(format, str) else (format or [])
        if len(requested_formats) > 20 or any(
            not isinstance(label, str) or not 1 <= len(label.strip()) <= 120
            for label in requested_formats
        ):
            raise ValueError("Invalid format filters.")
        for label in dict.fromkeys(
            canonical_source_format(value, "clz")
            if value.strip().casefold() == "4k uhd"
            else canonical_format(value)
            for value in requested_formats
        ):
            aliases = ("Blu-ray 4K", "4K UHD") if label == "Blu-ray 4K" else (label,)
            placeholders = ",".join("?" for _ in aliases)
            clauses.append(
                f"EXISTS(SELECT 1 FROM json_each(w.formats) "
                f"WHERE value COLLATE NOCASE IN ({placeholders}))"
            )
            params.extend(aliases)
        if source_id:
            clauses.append("EXISTS(SELECT 1 FROM records r WHERE r.work_id=w.id AND r.source_id=?)")
            params.append(source_id)
        if recent_days:
            clauses.append("w.date_added>=?")
            params.append(
                (datetime.now(UTC) - timedelta(days=recent_days)).isoformat(timespec="seconds")[:19]
            )
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        fields = {
            "title": "sort_title",
            "date_added": "date_added",
            "year": "year",
            "rating": "rating",
            "user_rating": "user_rating",
            "last_played": "last_played",
            "episode": "episode",
        }
        if (
            sort not in fields
            or order not in ("asc", "desc")
            or not 1 <= limit <= 200
            or offset < 0
        ):
            raise ValueError("Invalid pagination or sort option.")
        column = fields[sort]
        with self.connect() as conn:
            conn.execute("BEGIN")  # count and page see the same snapshot
            total = conn.execute("SELECT COUNT(*) FROM works w" + where, params).fetchone()[0]
            rows = conn.execute(
                "SELECT w.* FROM works w"
                + where
                + f" ORDER BY w.{column} IS NULL,w.{column} {order},w.id ASC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            items = self._summaries_with_sources(conn, rows)
            if person_name or person_tmdb_id or person_imdb_id:
                roles = self._matched_person_roles(
                    conn,
                    [row["id"] for row in rows],
                    person_name=person_name,
                    person_tmdb_id=person_tmdb_id,
                    person_imdb_id=person_imdb_id,
                )
                for item in items:
                    item["matched_roles"] = roles.get(item["id"], [])
            return {
                "items": items,
                "total": total,
                "limit": limit,
                "offset": offset,
                "has_more": offset + len(rows) < total,
            }

    def seasons(self, parent_id: str) -> list[dict[str, int]] | None:
        """Compact TV season index. Includes specials (season 0) if present."""
        with self.connect() as conn:
            show = conn.execute("SELECT kind FROM works WHERE id=?", (parent_id,)).fetchone()
            if not show or show["kind"] != "tvshow":
                return None
            rows = conn.execute(
                "SELECT season,COUNT(*) AS episode_count,"
                " SUM(CASE WHEN watch_status='watched' THEN 1 ELSE 0 END) AS watched_count"
                " FROM works WHERE kind='episode' AND parent_id=? AND season IS NOT NULL"
                " GROUP BY season ORDER BY season",
                (parent_id,),
            ).fetchall()
            cached = conn.execute(
                "SELECT payload FROM enrichment WHERE work_id=?", (parent_id,)
            ).fetchone()
            posters = json.loads(cached["payload"]).get("season_posters", {}) if cached else {}
            return [
                {
                    **dict(row),
                    "poster": (
                        f"/api/v1/media/{parent_id}/seasons/{row['season']}/poster"
                        if isinstance(posters, dict) and posters.get(str(row["season"]))
                        else None
                    ),
                }
                for row in rows
            ]

    def set_episode_watch_status(
        self,
        parent_id: str,
        watched: bool,
        season: int | None = None,
        up_to_episode: int | None = None,
    ) -> dict[str, Any] | None:
        """Change all episodes, one season, or episodes through an inclusive cutoff.

        An episode cutoff never affects another season or later episodes.
        Only changed effective watch states receive local overrides.
        """
        if up_to_episode is not None and season is None:
            raise ValueError("A season is required when selecting an episode cutoff.")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            show = conn.execute("SELECT kind FROM works WHERE id=?", (parent_id,)).fetchone()
            if not show or show["kind"] != "tvshow":
                return None
            condition = " AND season=?" if season is not None else ""
            params: tuple[Any, ...] = (parent_id, season) if season is not None else (parent_id,)
            if up_to_episode is not None:
                condition += " AND episode<=?"
                params += (up_to_episode,)
            rows = conn.execute(
                "SELECT id,watch_status FROM works "
                "WHERE kind='episode' AND parent_id=?" + condition + " ORDER BY season,episode,id",
                params,
            ).fetchall()
            changed_ids = [
                row["id"] for row in rows if (row["watch_status"] == "watched") != watched
            ]
            for episode_id in changed_ids:
                conn.execute(
                    "INSERT INTO personal(work_id,watched) VALUES(?,?) "
                    "ON CONFLICT(work_id) DO UPDATE SET watched=excluded.watched",
                    (episode_id, int(watched)),
                )
                self._rebuild(conn, episode_id)
        return {
            "total": len(rows),
            "updated": len(changed_ids),
            "ids": changed_ids,
            "watched": watched,
            "season": season,
            "up_to_episode": up_to_episode,
        }

    def season_poster(self, parent_id: str, season: int) -> list[str]:
        """Read a validated server-cached TMDB season poster candidate."""
        if season < 0 or season > 10000:
            return []
        with self.connect() as conn:
            row = conn.execute(
                "SELECT e.payload FROM enrichment e JOIN works w ON w.id=e.work_id"
                " WHERE w.id=? AND w.kind='tvshow'",
                (parent_id,),
            ).fetchone()
            if not row:
                return []
            posters = json.loads(row["payload"]).get("season_posters", {})
            candidate = posters.get(str(season)) if isinstance(posters, dict) else None
            if isinstance(candidate, str) and re.fullmatch(
                r"https://image\.tmdb\.org/t/p/w342/[A-Za-z0-9_.-]+", candidate
            ):
                return [candidate]
            return []

    def save_playback_resume(
        self, work_id: str, edition_id: str, position: float,
        total: float, updated_at: str
    ) -> dict[str, Any] | None:
        """Monotonic last-write-wins by client UTC timestamp, per edition.

        A delayed offline retry cannot overwrite a more recently recorded
        resume on this server. Never mutate the imported Kodi record payload.
        """
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            match = conn.execute(
                "SELECT 1 FROM records WHERE id=? AND work_id=?",
                (edition_id, work_id),
            ).fetchone()
            if not match:
                return None
            conn.execute(
                "INSERT INTO playback_resume(work_id,edition_id,position_seconds,total_seconds,updated_at)"
                " VALUES(?,?,?,?,?) ON CONFLICT(work_id,edition_id) DO UPDATE SET"
                " position_seconds=excluded.position_seconds,"
                " total_seconds=excluded.total_seconds,updated_at=excluded.updated_at"
                " WHERE excluded.updated_at >= playback_resume.updated_at",
                (work_id, edition_id, position, total, updated_at),
            )
            row = conn.execute(
                "SELECT edition_id,position_seconds,total_seconds,updated_at"
                " FROM playback_resume WHERE work_id=? AND edition_id=?",
                (work_id, edition_id),
            ).fetchone()
            return dict(row) if row else None

    def detail(self, work_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            conn.execute("BEGIN")
            return self._detail_in_transaction(conn, work_id)

    def details_bulk(self, work_ids: list[str]) -> list[dict[str, Any]]:
        """Return details from one consistent, indexed SQLite read transaction."""
        if len(work_ids) > 100:
            raise ValueError("Too many requested media details.")
        with self.connect() as conn:
            conn.execute("BEGIN")
            return [
                {
                    **detail,
                    "writeback_state": self._writeback_state_in_transaction(conn, work_id),
                }
                for work_id in dict.fromkeys(work_ids)
                if (detail := self._detail_in_transaction(conn, work_id)) is not None
            ]

    def _detail_in_transaction(
        self, conn: sqlite3.Connection, work_id: str
    ) -> dict[str, Any] | None:
        row = conn.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone()
        if row is None:
            return None
        value = self._summaries_with_sources(conn, [row])[0]
        value["media_sources"] = self._sources_for_works(conn, [work_id]).get(work_id, [])
        ids: dict[str, list[str]] = defaultdict(list)
        for identity in conn.execute(
            "SELECT namespace,value FROM identifiers WHERE work_id=? ORDER BY namespace,value",
            (work_id,),
        ):
            ids[identity[0]].append(identity[1])
        saved_resumes = {
            row["edition_id"]: dict(row)
            for row in conn.execute(
                "SELECT edition_id,position_seconds,total_seconds,updated_at"
                " FROM playback_resume WHERE work_id=?", (work_id,),
            )
        }
        editions = []
        for record in conn.execute(
            "SELECT r.*,s.name,s.type FROM records r JOIN sources s ON s.id=r.source_id WHERE work_id=? ORDER BY s.type DESC,r.external_key",
            (work_id,),
        ):
            payload = json.loads(record["payload"])
            editions.append(
                {
                    "id": record["id"],
                    "source_id": record["source_id"],
                    "source_name": record["name"],
                    "source_type": record["type"],
                    "external_key": record["external_key"],
                    "identifiers": payload["identifiers"],
                    "metadata": {
                        **payload["metadata"],
                        "formats": payload["formats"],
                        "collections": payload["collections"],
                    },
                    **{
                        n: payload.get(n)
                        for n in (
                            "watched",
                            "play_count",
                            "resume_seconds",
                            "total_seconds",
                            "date_added",
                            "last_played",
                        )
                    },
                    **({
                        "resume_seconds": saved_resumes[record["id"]]["position_seconds"],
                        "total_seconds": saved_resumes[record["id"]]["total_seconds"],
                        "playback_updated_at": saved_resumes[record["id"]]["updated_at"],
                    } if record["id"] in saved_resumes else {}),
                }
            )
        # The most recently updated edition determines the summary resume.
        # Never merge positions from different files/versions.
        if saved_resumes:
            newest = max(saved_resumes.values(), key=lambda item: item["updated_at"])
            self._apply_resume_to_summary(value, newest)
        source_people = []
        for edition in editions:
            metadata = edition["metadata"]
            source_people.extend(metadata.get("people", metadata.get("cast", [])))
        cached = conn.execute("SELECT * FROM enrichment WHERE work_id=?", (work_id,)).fetchone()
        enriched = json.loads(cached["payload"]) if cached else {}
        # Preserve source roles, fill portraits/IDs from matching TMDB names.
        people = []
        seen = set()
        for member in source_people + enriched.get("people", []):
            key = (
                fold(member["name"]),
                member.get("category", "cast"),
            )
            if key in seen:
                if key[1] != "cast" and member.get("role"):
                    previous = next(
                        p for p in people if (fold(p["name"]), p.get("category", "cast")) == key
                    )
                    roles = list(
                        dict.fromkeys(filter(None, [previous.get("role"), member["role"]]))
                    )
                    previous["role"] = " · ".join(roles)
                continue
            seen.add(key)
            match = next(
                (
                    p
                    for p in enriched.get("people", [])
                    if fold(p["name"]) == key[0]
                    and not (
                        p.get("tmdb_id")
                        and member.get("tmdb_id")
                        and p["tmdb_id"] != member["tmdb_id"]
                    )
                ),
                {},
            )
            merged = {**match, **member}
            for field in ("imdb_id", "tmdb_id"):
                merged[field] = member.get(field) or match.get(field)
            merged["photos"] = list(
                dict.fromkeys([*(member.get("photos") or []), *(match.get("photos") or [])])
            )
            people.append(merged)
        people = [person_view(p, work_id, index) for index, p in enumerate(people)]
        trailers = list(
            dict.fromkeys(
                url
                for candidate in [
                    *(t for e in editions for t in e["metadata"].get("trailers", [])),
                    *enriched.get("trailers", []),
                ]
                if (url := youtube_url(candidate))
            )
        )
        personal = conn.execute(
            "SELECT user_rating,watched FROM personal WHERE work_id=?", (work_id,)
        ).fetchone()
        return {
            **value,
            "identifiers": dict(ids),
            "editions": editions,
            "people": people,
            "cast": [p for p in people if p["category"] == "cast"],
            "trailers": trailers,
            "personal": dict(personal) if personal else {},
            "lists": self._lists(conn, work_id),
            "enriched_at": cached["updated_at"] if cached else None,
            "season_posters_cached": "season_posters" in enriched,
        }

    def update_personal(self, work_id: str, changes: dict) -> bool:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM works WHERE id=?", (work_id,)).fetchone():
                return False
            conn.execute("INSERT OR IGNORE INTO personal(work_id) VALUES(?)", (work_id,))
            for name, value in changes.items():
                if name not in ("user_rating", "watched"):
                    raise ValueError("Unknown personal field.")
                conn.execute(f"UPDATE personal SET {name}=? WHERE work_id=?", (value, work_id))
            self._rebuild(conn, work_id)
        return True

    def save_enrichment(self, work_id: str, payload: dict) -> bool:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM works WHERE id=?", (work_id,)).fetchone():
                return False
            conn.execute(
                "INSERT INTO enrichment VALUES(?,?,?) ON CONFLICT(work_id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",
                (work_id, encode(payload), now()),
            )
            self._rebuild(conn, work_id)
        return True

    @staticmethod
    def _lists(conn, work_id=None):
        where = (
            " WHERE EXISTS(SELECT 1 FROM list_items i WHERE i.list_id=l.id AND i.work_id=?)"
            if work_id
            else ""
        )
        return [
            {
                **dict(r),
                "thumbnail": f"/api/v1/lists/{r['id']}/thumbnails/{r['revision']}.jpg",
            }
            for r in conn.execute(
                "SELECT l.*,"
                "(SELECT COUNT(*) FROM list_items i WHERE i.list_id=l.id) AS item_count,"
                "(SELECT COUNT(*) FROM list_items i JOIN works w ON w.id=i.work_id "
                "WHERE i.list_id=l.id AND w.watch_status='watched') AS watched_count "
                "FROM lists l" + where + " ORDER BY l.name COLLATE NOCASE,l.id",
                (work_id,) if work_id else (),
            )
        ]

    def lists(self):
        with self.connect() as conn:
            return self._lists(conn)

    def save_list(self, data, list_id=None):
        with self.connect() as conn:
            if list_id:
                result = conn.execute(
                    "UPDATE lists SET name=?,description=?,revision=revision+1 WHERE id=?",
                    (data["name"], data["description"], list_id),
                )
                if not result.rowcount:
                    raise KeyError("Unknown list.")
            else:
                list_id = str(uuid4())
                conn.execute(
                    "INSERT INTO lists(id,name,description,created_at) VALUES(?,?,?,?)",
                    (list_id, data["name"], data["description"], now()),
                )
            return next(item for item in self._lists(conn) if item["id"] == list_id)

    def delete_list(self, list_id):
        with self.connect() as conn:
            if not conn.execute("DELETE FROM lists WHERE id=?", (list_id,)).rowcount:
                raise KeyError("Unknown list.")

    def change_list_item(self, list_id, work_id, *, remove=False):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM lists WHERE id=?", (list_id,)).fetchone():
                raise KeyError("Unknown list.")
            if remove:
                count = conn.execute(
                    "DELETE FROM list_items WHERE list_id=? AND work_id=?", (list_id, work_id)
                ).rowcount
            else:
                if not conn.execute("SELECT 1 FROM works WHERE id=?", (work_id,)).fetchone():
                    raise KeyError("Unknown media.")
                count = conn.execute(
                    "INSERT OR IGNORE INTO list_items VALUES(?,?,COALESCE((SELECT MAX(position)+1 FROM list_items WHERE list_id=?),0))",
                    (list_id, work_id, list_id),
                ).rowcount
            if count:
                conn.execute("UPDATE lists SET revision=revision+1 WHERE id=?", (list_id,))

    def add_list_items(self, list_id: str, media_ids: list[str]) -> dict[str, int]:
        """Atomically add existing works to a list, preserving result order.

        Repeated IDs and items already on the list are ignored. A missing work
        aborts the transaction instead of producing an incomplete collection.
        """
        ids = list(dict.fromkeys(media_ids))
        if not 1 <= len(ids) <= 200:
            raise ValueError("Supply between 1 and 200 media IDs.")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM lists WHERE id=?", (list_id,)).fetchone():
                raise KeyError("Unknown list.")
            placeholders = ",".join("?" for _ in ids)
            existing = {
                row[0]
                for row in conn.execute(f"SELECT id FROM works WHERE id IN ({placeholders})", ids)
            }
            if len(existing) != len(ids):
                raise KeyError("Unknown media.")
            already = {
                row[0]
                for row in conn.execute(
                    f"SELECT work_id FROM list_items WHERE list_id=? "
                    f"AND work_id IN ({placeholders})",
                    (list_id, *ids),
                )
            }
            pending = [work_id for work_id in ids if work_id not in already]
            if pending:
                first = conn.execute(
                    "SELECT COALESCE(MAX(position)+1,0) FROM list_items WHERE list_id=?",
                    (list_id,),
                ).fetchone()[0]
                conn.executemany(
                    "INSERT INTO list_items(list_id,work_id,position) VALUES(?,?,?)",
                    ((list_id, work_id, first + i) for i, work_id in enumerate(pending)),
                )
                conn.execute("UPDATE lists SET revision=revision+1 WHERE id=?", (list_id,))
            return {
                "added": len(pending),
                "already_present": len(ids) - len(pending),
                "total_requested": len(ids),
            }

    def reorder_list(self, list_id, media_ids, revision):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT revision FROM lists WHERE id=?", (list_id,)).fetchone()
            if not row:
                raise KeyError("Unknown list.")
            if row[0] != revision:
                raise RuntimeError("This list changed. Reload it before reordering.")
            current = {
                r[0]
                for r in conn.execute("SELECT work_id FROM list_items WHERE list_id=?", (list_id,))
            }
            if len(media_ids) != len(set(media_ids)) or set(media_ids) != current:
                raise ValueError("Supply every current media ID exactly once.")
            conn.executemany(
                "UPDATE list_items SET position=? WHERE list_id=? AND work_id=?",
                ((index, list_id, mid) for index, mid in enumerate(media_ids)),
            )
            conn.execute("UPDATE lists SET revision=revision+1 WHERE id=?", (list_id,))

    def list_thumbnail_input(self, list_id: str) -> dict[str, Any] | None:
        """Snapshot the revision and first four poster candidates in manual order."""
        with self.connect() as conn:
            row = conn.execute("SELECT revision FROM lists WHERE id=?", (list_id,)).fetchone()
            if row is None:
                return None
            candidates = conn.execute(
                "SELECT w.poster FROM list_items i JOIN works w ON w.id=i.work_id "
                "WHERE i.list_id=? ORDER BY i.position,w.id LIMIT 4",
                (list_id,),
            ).fetchall()
            return {
                "revision": row["revision"],
                "posters": [json.loads(record["poster"]) for record in candidates],
            }

    def list_page(self, list_id, *, sort="position", order="asc", limit=60, offset=0):
        columns = {
            "position": "i.position",
            "title": "w.sort_title",
            "year": "w.year",
            "rating": "w.rating",
            "user_rating": "w.user_rating",
            "date_added": "w.date_added",
        }
        if (
            sort not in columns
            or order not in ("asc", "desc")
            or not 1 <= limit <= 200
            or offset < 0
        ):
            raise ValueError("Invalid list sort or pagination.")
        with self.connect() as conn:
            conn.execute("BEGIN")
            row = conn.execute("SELECT revision FROM lists WHERE id=?", (list_id,)).fetchone()
            if not row:
                raise KeyError("Unknown list.")
            total = conn.execute(
                "SELECT COUNT(*) FROM list_items WHERE list_id=?", (list_id,)
            ).fetchone()[0]
            column = columns[sort]
            rows = conn.execute(
                f"SELECT w.* FROM list_items i JOIN works w ON w.id=i.work_id WHERE i.list_id=? ORDER BY {column} IS NULL,{column} {order},w.id LIMIT ? OFFSET ?",
                (list_id, limit, offset),
            ).fetchall()
            return {
                "items": self._summaries_with_sources(conn, rows),
                "total": total,
                "limit": limit,
                "offset": offset,
                "has_more": offset + len(rows) < total,
                "revision": row[0],
            }

    def artwork(self, work_id: str, aspect: str) -> list[str]:
        if aspect not in ("poster", "fanart"):
            return []
        with self.connect() as conn:
            row = conn.execute(f"SELECT {aspect} FROM works WHERE id=?", (work_id,)).fetchone()
            return json.loads(row[0]) if row else []

    def facets(self) -> dict[str, Any]:
        with self.connect() as conn:
            value = {
                name: [
                    r[0]
                    for r in conn.execute(
                        f"SELECT DISTINCT j.value FROM works w,json_each(w.{name}) j ORDER BY j.value COLLATE NOCASE"
                    )
                ]
                for name in ("genres", "formats", "collections")
            }
            value["formats"] = sorted(
                {canonical_format(label) for label in value["formats"]}, key=fold
            )
            value["years"] = [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT year FROM works WHERE year IS NOT NULL ORDER BY year DESC"
                )
            ]
        value["sources"] = self.sources()
        return value

    def status(self) -> dict[str, Any]:
        with self.connect() as conn:
            counts = {
                r[0]: r[1] for r in conn.execute("SELECT kind,COUNT(*) FROM works GROUP BY kind")
            }
            editions = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        return {
            "works": sum(counts.values()),
            "editions": editions,
            "counts": counts,
            "sources": self.sources(),
        }

    def new_job(self, source_id: str, operation: str = "kodi_sync") -> dict[str, Any]:
        if operation not in {
            "kodi_sync",
            "kodi_scan",
            "kodi_clean",
            "collection_import",
            "music_sync",
            "music_import",
        }:
            raise ValueError("Unsupported job operation.")
        job_id = str(uuid4())
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO jobs(id,source_id,status,created_at,operation) VALUES(?,?,'pending',?,?)",
                (job_id, source_id, now(), operation),
            )
        return self.job(job_id)

    @staticmethod
    def _job_view(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["result"] = json.loads(row["result"]) if row["result"] else None
        value["overall_percent"] = Store._progress_percent(value)
        return value

    @staticmethod
    def _progress_percent(job: dict[str, Any]) -> int:
        """Monotonic estimated overall progress; never restarts per media kind.

        The time required for MariaDB queries and the final SQLite publication
        cannot be known in advance, so this is explicitly an estimate.
        """
        if job["status"] == "completed":
            return 100
        phase = job.get("progress_phase")
        start, end = {
            "kodi_cleaning": (1, 1),
            "kodi_clean_finished": (1, 1),
            "connecting": (1, 4),
            "loading": (4, 8),
            "movies": (8, 70),
            "series": (70, 80),
            "episodes": (80, 95),
            "verifying_library": (95, 97),
            "parsing_clz": (2, 92),
            "parsing_mymovies": (2, 92),
            "parsing_filmotech": (2, 92),
            "music_artists": (5, 15),
            "music_albums": (15, 35),
            "music_songs": (35, 92),
            "parsing_music_csv": (2, 92),
            "parsing_discogs": (2, 92),
            "parsing_clz_music": (2, 92),
            "publishing": (97, 99),
        }.get(phase, (0, 0))
        current = max(0, int(job.get("progress_current") or 0))
        total = max(0, int(job.get("progress_total") or 0))
        if total and phase in (
            "movies", "series", "episodes", "music_artists", "music_albums", "music_songs"
        ):
            return start + (end - start) * min(current, total) // total
        if phase == "publishing":
            return start
        if phase in ("connecting", "loading"):
            return start
        return start

    def last_kodi_sync_attempt(self, source_id: str) -> str | None:
        """Last durable SQL/Kodi import attempt, including failed jobs.

        Auto-refresh throttling must survive app and server restarts. A source
        that is offline must not trigger another full catalogue synchronization
        on every app launch, even though its last_sync remains stale.
        """
        with self.connect() as conn:
            row = conn.execute(
                "SELECT created_at FROM jobs WHERE source_id=? AND operation='kodi_sync' "
                "ORDER BY created_at DESC LIMIT 1",
                (source_id,),
            ).fetchone()
            return row[0] if row else None

    def jobs(self, *, limit: int = 30) -> list[dict[str, Any]]:
        """Durable feed of manual and automatically scheduled jobs."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC,rowid DESC LIMIT ?",
                (min(100, max(1, limit)),),
            ).fetchall()
            return [self._job_view(row) for row in rows]

    def job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return None
            return self._job_view(row)

    def update_job_progress(
        self, job_id: str, phase: str, current: int = 0, total: int = 0
    ) -> None:
        """Commit phase counters independently of the Kodi read transaction."""
        with self.connect() as conn:
            conn.execute(
                "UPDATE jobs SET progress_phase=?,progress_current=?,progress_total=? "
                "WHERE id=? AND status='running'",
                (phase, max(0, current), max(0, total), job_id),
            )

    def finish_job(
        self, job_id: str, *, result: dict[str, Any] | None = None, error: str | None = None
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE jobs SET status=?,finished_at=?,result=?,error=?,progress_phase=? WHERE id=?",
                (
                    "failed" if error else "completed",
                    now(),
                    encode(result) if result else None,
                    error,
                    "failed" if error else "completed",
                    job_id,
                ),
            )
            if error:
                conn.execute(
                    "UPDATE sources SET last_error=? WHERE id=(SELECT source_id FROM jobs WHERE id=?)",
                    (error, job_id),
                )

    def start_job(self, job_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE jobs SET status='running',"
                "progress_phase=CASE WHEN operation='kodi_scan' THEN 'kodi_scanning' "
                "WHEN operation IN ('collection_import','music_import') THEN 'reading_collection' "
                "ELSE 'connecting' END,"
                "progress_current=0,progress_total=0 WHERE id=?",
                (job_id,),
            )

    def players(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM players ORDER BY name").fetchall()
            return [self._player_view(r) for r in rows]

    @staticmethod
    def _player_view(row: sqlite3.Row) -> dict[str, Any]:
        config = json.loads(row["config"])
        return {
            "id": row["id"],
            "name": row["name"],
            "source_id": row["source_id"],
            "has_password": bool(config.pop("password", "")),
            **config,
        }

    def player(self, player_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM players WHERE id=?", (player_id,)).fetchone()
            return {**dict(row), "config": json.loads(row["config"])} if row else None

    def save_player(self, data: dict[str, Any], player_id: str | None = None) -> dict[str, Any]:
        player_id = player_id or str(uuid4())
        config = {k: v for k, v in data.items() if k not in ("name", "source_id")}
        with self.connect() as conn:
            if config.get("password") is None or config.get("mac") is None:
                old = conn.execute("SELECT config FROM players WHERE id=?", (player_id,)).fetchone()
                previous = json.loads(old[0]) if old else {}
                if config.get("password") is None:
                    config["password"] = previous.get("password", "")
                if config.get("mac") is None:
                    config["mac"] = previous.get("mac", "")
            conn.execute(
                "INSERT INTO players(id,name,source_id,config) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,source_id=excluded.source_id,config=excluded.config",
                (player_id, data["name"], data["source_id"], encode(config)),
            )
        return next(p for p in self.players() if p["id"] == player_id)

    def delete_player(self, player_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM players WHERE id=?", (player_id,))

    @staticmethod
    def _source_symbol(source_type: str) -> str:
        # SF Symbols IDs are reusable directly by the iOS application.
        return {"kodi": "play.rectangle.on.rectangle", "mymovies": "film.stack"}.get(
            source_type, "arrow.triangle.2.circlepath"
        )

    def stage_writebacks(
        self, work_id: str, detail: dict, changes: dict, sources: list[dict]
    ) -> None:
        """Persist the latest explicit edit BEFORE calling remote services.

        A new value supersedes an older queued value for the same source/field.
        Clearing a local override discards the matching queued field but does
        not write null to the remote service.
        """
        from .writeback import WRITABLE_FIELDS

        by_source = {}
        for edition in detail["editions"]:
            if edition["source_type"] in WRITABLE_FIELDS:
                by_source[edition["source_id"]] = edition["source_type"]
        source_info = {source["id"]: source for source in sources if source}
        with self.connect() as conn:
            for source_id, source_type in by_source.items():
                source = source_info.get(source_id) or {}
                if source_type == "mymovies" and not source.get("config", {}).get("remote_enabled"):
                    continue
                applicable = {k: v for k, v in changes.items() if k in WRITABLE_FIELDS[source_type]}
                if not applicable:
                    continue
                row = conn.execute(
                    "SELECT id,changes FROM writeback_pending WHERE work_id=? AND source_id=?",
                    (work_id, source_id),
                ).fetchone()
                pending = json.loads(row["changes"]) if row else {}
                # Clear local-only resets while preserving unrelated failed fields.
                for key, value in applicable.items():
                    if value is None:
                        pending.pop(key, None)
                    else:
                        pending[key] = value
                if row:
                    if pending:
                        conn.execute(
                            "UPDATE writeback_pending SET changes=?,updated_at=?,last_error=NULL "
                            "WHERE id=?",
                            (encode(pending), now(), row["id"]),
                        )
                    else:
                        conn.execute("DELETE FROM writeback_pending WHERE id=?", (row["id"],))
                elif pending:
                    timestamp = now()
                    conn.execute(
                        "INSERT INTO writeback_pending "
                        "(id,work_id,source_id,changes,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (str(uuid4()), work_id, source_id, encode(pending), timestamp, timestamp),
                    )

    def pending_writebacks(self, *, pending_id: str | None = None, limit: int = 200) -> list[dict]:
        with self.connect() as conn:
            where = "WHERE p.id=?" if pending_id else ""
            args = (pending_id,) if pending_id else (min(max(limit, 1), 500),)
            rows = conn.execute(
                "SELECT p.*,w.title AS media_title,s.name AS source_name,"
                "s.type AS source_type FROM writeback_pending p "
                "JOIN works w ON w.id=p.work_id "
                "JOIN sources s ON s.id=p.source_id "
                + where
                + " ORDER BY p.updated_at DESC,p.id DESC"
                + ("" if pending_id else " LIMIT ?"),
                args,
            ).fetchall()
            return [
                {
                    "id": row["id"],
                    "media_id": row["work_id"],
                    "media_title": row["media_title"],
                    "source_id": row["source_id"],
                    "source_name": row["source_name"],
                    "source_type": row["source_type"],
                    "icon": self._source_symbol(row["source_type"]),
                    "changes": json.loads(row["changes"]),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "retry_count": row["retry_count"],
                    "last_error": row["last_error"],
                }
                for row in rows
            ]

    def writeback_pending_count(self) -> int:
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM writeback_pending").fetchone()[0]

    def writeback_state(self, work_id: str | None = None, limit: int = 30) -> list[dict]:
        """Last upstream result per source, overlaid with unresolved pending changes."""
        with self.connect() as conn:
            return self._writeback_state_in_transaction(conn, work_id, limit)

    def _writeback_state_in_transaction(
        self, conn: sqlite3.Connection, work_id: str | None = None, limit: int = 30
    ) -> list[dict]:
        query = (
            "SELECT a.*,w.title AS media_title FROM writeback_audit a "
            "LEFT JOIN works w ON w.id=a.work_id "
            + ("WHERE a.work_id=? " if work_id else "")
            + "ORDER BY a.created_at DESC,a.rowid DESC LIMIT ?"
        )
        args = (work_id, 3000) if work_id else (3000,)
        rows = conn.execute(query, args).fetchall()
        latest = {}
        for row in rows:
            key = (row["work_id"], row["source_id"])
            if key not in latest:
                latest[key] = {
                    "media_id": row["work_id"],
                    "media_title": row["media_title"],
                    "source_id": row["source_id"],
                    "source_name": row["source_name"],
                    "source_type": row["source_type"],
                    "icon": self._source_symbol(row["source_type"]),
                    "status": row["status"],
                    "method": row["method"],
                    "updated_at": row["created_at"],
                    "pending_fields": [],
                    "message": row["message"],
                }
        pending_rows = conn.execute(
            "SELECT p.*,s.type AS source_type,s.name AS source_name,"
            "w.title AS media_title FROM writeback_pending p "
            "JOIN sources s ON s.id=p.source_id "
            "LEFT JOIN works w ON w.id=p.work_id " + ("WHERE p.work_id=?" if work_id else ""),
            (work_id,) if work_id else (),
        ).fetchall()
        for row in pending_rows:
            key = (row["work_id"], row["source_id"])
            last = latest.get(key, {})
            latest[key] = {
                "media_id": row["work_id"],
                "media_title": row["media_title"],
                "source_id": row["source_id"],
                "source_name": row["source_name"],
                "source_type": row["source_type"],
                "icon": self._source_symbol(row["source_type"]),
                "status": "pending",
                "method": last.get("method"),
                "updated_at": row["updated_at"],
                "pending_fields": sorted(json.loads(row["changes"])),
                "message": row["last_error"] or last.get("message"),
            }
        return sorted(latest.values(), key=lambda event: event["updated_at"], reverse=True)[
            :limit
        ]

    def record_writebacks(
        self, work_id: str, changes: dict, results: list[dict], *, replay: bool = False
    ) -> None:
        """Atomically record remote outcomes and clear ONLY delivered queue fields."""
        if not results:
            return
        fields = encode(sorted(changes))
        with self.connect() as conn:
            for result in results:
                source_id = result["source_id"]
                row = conn.execute(
                    "SELECT id,changes FROM writeback_pending WHERE work_id=? AND source_id=?",
                    (work_id, source_id),
                ).fetchone()
                if row:
                    queued = json.loads(row["changes"])
                    if result["status"] == "updated":
                        for field, value in changes.items():
                            if field in queued and queued[field] == value:
                                queued.pop(field)
                        if queued:
                            conn.execute(
                                "UPDATE writeback_pending SET changes=?,updated_at=?,last_error=NULL "
                                "WHERE id=?",
                                (encode(queued), now(), row["id"]),
                            )
                        else:
                            conn.execute("DELETE FROM writeback_pending WHERE id=?", (row["id"],))
                    else:
                        conn.execute(
                            "UPDATE writeback_pending SET last_error=?,updated_at=?,"
                            "retry_count=retry_count+? WHERE id=?",
                            (result.get("message"), now(), int(replay), row["id"]),
                        )
                conn.execute(
                    "INSERT INTO writeback_audit "
                    "(id,work_id,source_id,source_name,source_type,created_at,status,"
                    "method,attempts,fields,message) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(uuid4()),
                        work_id,
                        source_id,
                        result["source_name"],
                        result["source_type"],
                        now(),
                        result["status"],
                        result.get("method"),
                        encode(result.get("attempts", [])),
                        fields,
                        result.get("message"),
                    ),
                )

    def writeback_history(
        self, *, work_id: str | None = None, source_id: str | None = None, limit: int = 50
    ) -> list[dict]:
        if not work_id and not source_id:
            raise ValueError("A media or a source ID is required.")
        column = "work_id" if work_id else "source_id"
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT a.*,w.title AS media_title FROM writeback_audit a "
                f"LEFT JOIN works w ON w.id=a.work_id WHERE a.{column}=? "
                "ORDER BY a.created_at DESC,a.rowid DESC LIMIT ?",
                (work_id or source_id, min(max(1, limit), 200)),
            ).fetchall()
            return [
                {
                    **dict(row),
                    "attempts": json.loads(row["attempts"]),
                    "fields": json.loads(row["fields"]),
                }
                for row in rows
            ]
