"""Portable JSON library backups. Never export authentication or connection secrets."""

import json
import sqlite3

from .utils import fold, now

FORMAT = "home-kodi-library"
FORMAT_VERSION = 1
SCHEMA_VERSION = 2
MAX_BACKUP_BYTES = 100 * 1024 * 1024

# Parent rows precede dependants; generated FTS and transient state are rebuilt.
TABLES = (
    "sources",
    "works",
    "records",
    "identifiers",
    "personal",
    "enrichment",
    "lists",
    "list_items",
    "work_person_ids",
    "work_person_names",
    "catalogue_meta",
    "players",
)
# Music tables (added later). Older backups without them remain importable.
MUSIC_TABLES = (
    "music_artists",
    "music_artist_sources",
    "music_albums",
    "music_album_artists",
    "music_album_editions",
    "music_tracks",
)
SECRETS = {"password", "remote_api_key", "api_key", "access_token", "token", "secret"}


def _without_secrets(config: str) -> str:
    value = json.loads(config)
    if not isinstance(value, dict):
        raise ValueError("Invalid connection configuration.")
    return json.dumps(
        {key: ("" if key.lower() in SECRETS else item) for key, item in value.items()},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def export_backup(store) -> dict:
    """A consistent transactional snapshot; device tokens and writeback queues stay private."""
    with store.connect() as conn:
        conn.execute("BEGIN")
        tables = {}
        for table in TABLES + MUSIC_TABLES:
            rows = [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
            if table in ("sources", "players"):
                for row in rows:
                    row["config"] = _without_secrets(row["config"])
            tables[table] = rows
        return {
            "format": FORMAT,
            "version": FORMAT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "exported_at": now(),
            "credentials_omitted": True,
            "tables": tables,
        }


def import_backup(store, document: object) -> tuple[list[str], list[str]]:
    """Atomically replace catalogue data. Preserve existing paired-device access."""
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise ValueError("Not a Home Media Catalog backup.")
    if (
        document.get("version") != FORMAT_VERSION
        or document.get("schema_version") != SCHEMA_VERSION
    ):
        raise ValueError("Unsupported backup format or database schema.")
    tables = document.get("tables")
    if not isinstance(tables, dict) or set(tables) - set(MUSIC_TABLES) != set(TABLES):
        raise ValueError("Backup is missing required catalogue tables.")
    restored = TABLES + tuple(t for t in MUSIC_TABLES if t in tables)
    if any(
        not isinstance(tables[name], list) or any(not isinstance(row, dict) for row in tables[name])
        for name in restored
    ):
        raise ValueError("Invalid backup table data.")

    with store.connect() as conn:
        columns = {
            name: {column["name"] for column in conn.execute(f"PRAGMA table_info({name})")}
            for name in restored
        }
        for table in restored:
            for row in tables[table]:
                if not row or set(row) - columns[table]:
                    raise ValueError(f"Invalid columns in backup table {table}.")
                # No binary blobs or nested objects should reach SQLite bind parameters.
                if any(
                    not isinstance(value, (str, int, float, type(None), bool))
                    for value in row.values()
                ):
                    raise ValueError(f"Invalid value in backup table {table}.")

        old_lists = [row["id"] for row in conn.execute("SELECT id FROM lists")]
        existing_secrets = {}
        for table in ("sources", "players"):
            existing_secrets[table] = {
                row["id"]: json.loads(row["config"])
                for row in conn.execute(f"SELECT id,config FROM {table}")
            }
        if conn.execute(
            "SELECT 1 FROM jobs WHERE status IN ('pending','running') LIMIT 1"
        ).fetchone():
            raise RuntimeError("Wait for active imports or synchronizations to finish.")

        # A single write transaction prevents partially restored catalogues.
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("PRAGMA defer_foreign_keys=ON")
        try:
            for table in (
                "music_tracks_fts",
                "music_albums_fts",
                "music_artists_fts",
                "music_tracks",
                "music_album_editions",
                "music_album_artists",
                "music_albums",
                "music_artist_sources",
                "music_artists",
                "writeback_pending",
                "writeback_audit",
                "jobs",
                "list_items",
                "lists",
                "personal",
                "enrichment",
                "identifiers",
                "work_person_ids",
                "work_person_names",
                "records",
                "players",
                "works_fts",
                "works_title_fts",
                "works",
                "sources",
                "catalogue_meta",
            ):
                conn.execute(f"DELETE FROM {table}")

            for table in restored:
                for original in tables[table]:
                    row = dict(original)
                    if table in ("sources", "players"):
                        config = json.loads(row["config"])
                        if not isinstance(config, dict):
                            raise ValueError("Invalid connection configuration.")
                        # Restore passwords only when the identical connection already
                        # exists locally; backups never contain those credentials.
                        previous = existing_secrets[table].get(row["id"], {})
                        if all(
                            config.get(key) == previous.get(key)
                            for key in ("host", "port", "username", "tls")
                        ):
                            for key in SECRETS:
                                if key in previous and not config.get(key):
                                    config[key] = previous[key]
                        row["config"] = (
                            _without_secrets(json.dumps(config))
                            if not previous
                            else json.dumps(config, ensure_ascii=False, separators=(",", ":"))
                        )
                    names = list(row)
                    marks = ",".join("?" for _ in names)
                    conn.execute(
                        f"INSERT INTO {table} ({','.join(names)}) VALUES ({marks})",
                        [row[name] for name in names],
                    )

            # Thumbnail revision must differ even when restoring the same list ID.
            conn.execute("UPDATE lists SET revision=revision+1")
            conn.executemany(
                "INSERT INTO works_fts(work_id,search_text) VALUES(?,?)",
                (
                    (row["id"], row["search_text"])
                    for row in conn.execute("SELECT id,search_text FROM works")
                ),
            )
            conn.executemany(
                "INSERT INTO works_title_fts(work_id,titles) VALUES(?,?)",
                (
                    (row["id"], fold(" ".join(filter(None, (row["title"], row["original_title"])))))
                    for row in conn.execute("SELECT id,title,original_title FROM works")
                ),
            )
            # Music search indexes are derived data.
            conn.execute(
                "INSERT INTO music_artists_fts(rowid,text) SELECT rid,name FROM music_artists"
            )
            conn.executemany(
                "INSERT INTO music_albums_fts(rowid,text) VALUES(?,?)",
                (
                    (row["rid"], fold(" ".join(filter(None, (row["title"], row["artist_name"])))))
                    for row in conn.execute("SELECT rid,title,artist_name FROM music_albums")
                ),
            )
            conn.executemany(
                "INSERT INTO music_tracks_fts(rowid,text) VALUES(?,?)",
                (
                    (
                        row["rid"],
                        fold(
                            " ".join(
                                filter(None, (row["title"], row["artist_name"], row["album_title"]))
                            )
                        ),
                    )
                    for row in conn.execute(
                        "SELECT rid,title,artist_name,album_title FROM music_tracks"
                    )
                ),
            )
            if conn.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("Backup contains broken media or list references.")
            new_lists = [row["id"] for row in conn.execute("SELECT id FROM lists")]
        except (sqlite3.Error, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid or inconsistent backup data.") from exc
        return old_lists, new_lists
