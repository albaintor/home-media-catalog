"""Conservative, explicitly configured Kodi MariaDB write fallback.

This is used only when the Kodi JSON-RPC transport is unavailable. We never
write through views; the source credentials need UPDATE privileges.
"""

import math
import re
import socket
from contextlib import closing

import pymysql

from .connectors.base import ConnectorError
from .utils import fold

TABLES = {
    "movie": ("movie", "idMovie"),
    "tvshow": ("tvshow", "idShow"),
    "episode": ("episode", "idEpisode"),
}


def write_kodi_mariadb(source: dict, detail: dict, editions: list[dict], changes: dict) -> str:
    config = source["config"]
    if not config.get("mariadb_write_enabled"):
        raise ConnectorError("Repli MariaDB non activé pour cette source Kodi.")
    database = config.get("database") or ""
    automatic_database = not bool(database)
    if database and not re.fullmatch(r"MyVideos[0-9]+", database, flags=re.IGNORECASE):
        raise ConnectorError("Le nom de base doit être au format MyVideos suivi d'un numéro.")
    username = config.get("username") or ""
    password = config.get("password") or ""
    if not username:
        raise ConnectorError("Configurez les identifiants MariaDB de la source Kodi.")
    host = config.get("host", "")
    if not host or any(c in host for c in ("/", "@", "?", "#")):
        raise ConnectorError("Hôte MariaDB invalide.")
    kind = detail["kind"]
    if kind not in TABLES:
        raise ConnectorError("Type Kodi non pris en charge pour l'écriture SQL.")
    table, id_col = TABLES[kind]
    ssl = {"check_hostname": True, "verify_mode": True} if config.get("tls") else None
    if ssl and config.get("ca_file"):
        ssl["ca"] = config["ca_file"]
    try:
        with closing(
            pymysql.connect(
                host=host,
                port=config.get("port", 3306),
                user=username,
                password=password,
                database=database or None,
                connect_timeout=5,
                read_timeout=15,
                write_timeout=15,
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=False,
                ssl=ssl,
            )
        ) as conn:
            try:
                with conn.cursor() as cursor:
                    if automatic_database:
                        cursor.execute("SHOW DATABASES")
                        visible = [
                            (int(match.group(1)), name)
                            for row in cursor.fetchall()
                            for name in [str(next(iter(row.values())))]
                            if (match := re.fullmatch(r"MyVideos([0-9]+)", name, flags=re.IGNORECASE))
                        ]
                        if not visible:
                            raise ConnectorError(
                                "Aucune base MyVideos visible : impossible de sélectionner automatiquement la base."
                            )
                        database = max(visible, key=lambda item: (item[0], item[1]))[1]
                        conn.select_db(database)
                    ids = {}
                    for edition in editions:
                        kodi_id = edition.get("metadata", {}).get("kodi_id")
                        if type(kodi_id) is not int or kodi_id <= 0:
                            raise ConnectorError(
                                "Identifiant Kodi absent : resynchronisez d'abord la source."
                            )
                        ids.setdefault(kodi_id, edition)
                    # Preflight every identity and file ID before updating anything.
                    targets = []
                    for kodi_id, edition in ids.items():
                        metadata = edition.get("metadata") or {}
                        imported_database = metadata.get("database")
                        if imported_database and str(imported_database).casefold() != database.casefold():
                            raise ConnectorError(
                                "La base MyVideos sélectionnée ne correspond pas à celle de l'import. "
                                "Resynchronisez la source avant d'écrire."
                            )
                        cursor.execute(
                            f"SELECT {id_col}, c00, userrating"
                            + (", idFile" if kind in ("movie", "episode") else "")
                            + f" FROM {table} WHERE {id_col}=%s FOR UPDATE",
                            (kodi_id,),
                        )
                        row = cursor.fetchone()
                        if row is None:
                            raise ConnectorError(
                                "Un identifiant Kodi n'existe plus : resynchronisez la source."
                            )
                        if fold(str(row["c00"] or "")) != fold(str(detail["title"] or "")):
                            raise ConnectorError(
                                "Titre du média différent dans MariaDB : écriture refusée."
                            )
                        local_ids = edition.get("identifiers") or {}
                        cursor.execute(
                            "SELECT type, value FROM uniqueid WHERE media_type=%s AND media_id=%s",
                            (kind, kodi_id),
                        )
                        remote_ids = {str(r["type"]): str(r["value"]) for r in cursor.fetchall()}
                        common = local_ids.keys() & remote_ids.keys()
                        if any(str(local_ids[k]) != remote_ids[k] for k in common):
                            raise ConnectorError(
                                "Identifiants externes contradictoires : écriture refusée."
                            )
                        # Old Kodi imports can lack external IDs entirely.
                        # When that happens, require the imported *exact* video
                        # path to match the same file row in this MyVideos DB,
                        # in addition to the already checked ID and title.
                        # Never rely on the numeric ID and title alone.
                        if not common:
                            expected_file = str(metadata.get("file") or "")
                            stored_file_id = metadata.get("file_id") or row.get("idFile")
                            # TV shows are aggregates with no file of their
                            # own. An exact saved database provenance plus an
                            # exact title and Kodi ID identifies this library
                            # even when no external ID exists.
                            verified_show = (
                                kind == "tvshow"
                                and imported_database
                                and str(imported_database).casefold() == database.casefold()
                            )
                            if not verified_show:
                                if not expected_file or not stored_file_id or kind == "tvshow":
                                    raise ConnectorError(
                                        "Aucun identifiant externe ni fichier commun : "
                                        "repli SQL refusé par sécurité."
                                    )
                                cursor.execute(
                                    "SELECT p.strPath, f.strFilename FROM files f "
                                    "JOIN path p ON p.idPath=f.idPath "
                                    "WHERE f.idFile=%s",
                                    (stored_file_id,),
                                )
                                file_row = cursor.fetchone()
                                if (
                                    file_row is None or
                                    str(file_row.get("strPath") or "") +
                                    str(file_row.get("strFilename") or "") != expected_file
                                ):
                                    raise ConnectorError(
                                        "Le chemin vidéo ne correspond pas à l'import Kodi : "
                                        "écriture SQL refusée."
                                    )
                        file_ids = []
                        if kind in ("movie", "episode"):
                            if row["idFile"] is not None:
                                file_ids.append(int(row["idFile"]))
                            if kind == "movie":
                                # Alternative versions have distinct files; target only
                                # IDs already attached to the verified movie.
                                cursor.execute("SHOW COLUMNS FROM movie_view")
                                cols = {r["Field"].casefold() for r in cursor.fetchall()}
                                if "videoversionidfile" in cols:
                                    cursor.execute(
                                        "SELECT DISTINCT videoVersionIdFile FROM movie_view "
                                        "WHERE idMovie=%s AND videoVersionIdFile IS NOT NULL",
                                        (kodi_id,),
                                    )
                                    file_ids.extend(
                                        int(r["videoVersionIdFile"]) for r in cursor.fetchall()
                                    )
                        elif type(changes.get("watched")) is bool:
                            cursor.execute(
                                "SELECT DISTINCT e.idFile FROM tvshowlinkepisode t "
                                "JOIN episode e ON e.idEpisode=t.idEpisode "
                                "WHERE t.idShow=%s AND e.idFile IS NOT NULL",
                                (kodi_id,),
                            )
                            file_ids = [int(r["idFile"]) for r in cursor.fetchall()]
                            if not file_ids:
                                raise ConnectorError(
                                    "La série ne possède aucun épisode Kodi modifiable."
                                )
                        targets.append((kodi_id, sorted(set(file_ids))))

                    rating = changes.get("user_rating")
                    watched = changes.get("watched")
                    if "user_rating" in changes:
                        # None removes the rating: Kodi stores 0 for "no rating".
                        rounded = 0 if rating is None else math.floor(float(rating) + 0.5)
                        for kodi_id, _ in targets:
                            cursor.execute(
                                f"UPDATE {table} SET userrating=%s WHERE {id_col}=%s",
                                (rounded, kodi_id),
                            )
                    if type(watched) is bool:
                        for _kodi_id, file_ids in targets:
                            if not file_ids:
                                raise ConnectorError("Aucun fichier Kodi associé à ce média.")
                            placeholders = ",".join("%s" for _ in file_ids)
                            sql = (
                                "UPDATE files SET playCount=GREATEST(COALESCE(playCount,0),1), "
                                "lastPlayed=COALESCE(lastPlayed,UTC_TIMESTAMP()) "
                                if watched
                                else "UPDATE files SET playCount=0,lastPlayed=NULL "
                            ) + f"WHERE idFile IN ({placeholders})"
                            cursor.execute(sql, file_ids)
                    conn.commit()
                    return (
                        f"Repli MariaDB appliqué sur {database}"
                        + (" (sélection automatique)" if automatic_database else "")
                        + " ; Kodi ne reçoit aucune notification JSON-RPC."
                        + " Un rafraîchissement des vues Kodi peut être nécessaire."
                    )
            except Exception:
                conn.rollback()
                raise
    except ConnectorError:
        raise
    except pymysql.MySQLError as exc:
        code = exc.args[0] if exc.args else "inconnu"
        raise ConnectorError(
            f"Écriture MariaDB refusée ou indisponible (code {code}), transaction annulée."
        ) from exc
    except (socket.gaierror, OSError) as exc:
        raise ConnectorError("Connexion au serveur MariaDB indisponible.") from exc
