"""Portable catalogue backup: authentication, exact restore, redaction and atomicity."""

from copy import deepcopy

from conftest import READ

from home_media_catalog.connectors.base import Record


def _library(client):
    store = client.app.state.store
    source = store.save_source(
        {
            "name": "Disc archive",
            "type": "clz",
            "config": {"password": "server-only-secret", "host": "nas"},
        }
    )
    store.apply_snapshot(
        source["id"],
        [
            Record("disc:1", "movie", "Example film", year=2020),
            Record("disc:2", "movie", "Another film", year=2021),
        ],
    )
    work = next(item for item in store.media()["items"] if item["title"] == "Example film")["id"]
    store.update_personal(work, {"user_rating": 9.0, "watched": 1})
    playlist = store.save_list({"name": "Favorites", "description": "My selection"})
    store.change_list_item(playlist["id"], work)
    return store, source["id"], work, playlist["id"]


def test_export_import_round_trip_and_thumbnail_revision(client, admin):
    store, source_id, work_id, list_id = _library(client)
    store.register_device_token("test-device", "iPhone", "admin")
    assert client.get("/api/v1/backup").status_code == 401
    read = {"Authorization": f"Bearer {READ}"}
    assert client.get("/api/v1/backup", headers=read).status_code == 200
    assert client.post("/api/v1/backup", headers=read, json={}).status_code == 403

    exported = client.get("/api/v1/backup", headers=admin)
    assert exported.status_code == 200
    document = exported.json()
    assert document["format"] == "home-kodi-library"
    assert "device_tokens" not in document["tables"]
    assert "writeback_pending" not in document["tables"]
    assert "server-only-secret" not in exported.text
    assert document["credentials_omitted"] is True
    original_revision = store.lists()[0]["revision"]

    store.update_personal(work_id, {"user_rating": 1.0, "watched": 0})
    store.delete_list(list_id)
    response = client.post("/api/v1/backup", headers=admin, json=document)
    assert response.status_code == 200, response.text
    assert response.json()["lists"] == 1
    assert store.detail(work_id)["personal"]["user_rating"] == 9.0
    assert store.detail(work_id)["personal"]["watched"] == 1
    restored = store.lists()[0]
    assert restored["id"] == list_id
    assert restored["revision"] == original_revision + 1
    assert store.list_page(list_id)["items"][0]["id"] == work_id
    assert list(client.app.state.list_thumbnails.directory.glob(f"{list_id}-*.jpg"))
    assert store.source(source_id, private=True)["config"]["password"] == "server-only-secret"
    assert store.device_tokens()[0]["token_hash"] == "test-device"
    assert any(item["id"] == work_id for item in store.media(q="Example")["items"])


def test_backup_restores_edition_resume_and_person_roles_and_accepts_old_v2(client, admin):
    """Keep newly added catalogue state without breaking historical JSON archives."""
    store, _, work_id, _ = _library(client)
    with store.connect() as conn:
        edition = conn.execute(
            "SELECT id FROM records WHERE work_id=?", (work_id,)
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO playback_resume "
            "(work_id,edition_id,position_seconds,total_seconds,updated_at) "
            "VALUES(?,?,?,?,?)",
            (work_id, edition, 123.5, 600.0, "2026-11-11T12:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO work_person_roles(work_id,name_fold,category) VALUES(?,?,?)",
            (work_id, "example actor", "actor"),
        )

    exported = client.get("/api/v1/backup", headers=admin)
    assert exported.status_code == 200
    document = exported.json()
    assert document["tables"]["playback_resume"]
    assert document["tables"]["work_person_roles"]
    assert document["tables"]["playback_resume"][0]["position_seconds"] == 123.5

    with store.connect() as conn:
        conn.execute("DELETE FROM playback_resume")
        conn.execute("DELETE FROM work_person_roles WHERE work_id=?", (work_id,))

    restored = client.post("/api/v1/backup", headers=admin, json=document)
    assert restored.status_code == 200, restored.text
    with store.connect() as conn:
        resume = conn.execute(
            "SELECT position_seconds,total_seconds FROM playback_resume WHERE work_id=?",
            (work_id,),
        ).fetchone()
        roles = conn.execute(
            "SELECT name_fold,category FROM work_person_roles WHERE work_id=?",
            (work_id,),
        ).fetchall()
    assert resume["position_seconds"] == 123.5
    assert resume["total_seconds"] == 600.0
    assert ("example actor", "actor") in [(row["name_fold"], row["category"]) for row in roles]

    older = deepcopy(document)
    older["tables"].pop("playback_resume")
    older["tables"].pop("work_person_roles")
    legacy_import = client.post("/api/v1/backup", headers=admin, json=older)
    assert legacy_import.status_code == 200, legacy_import.text


def test_invalid_backup_is_rejected_without_partial_import(client, admin):
    store, _, work_id, _ = _library(client)
    original = client.get("/api/v1/backup", headers=admin).json()
    incomplete = deepcopy(original)
    del incomplete["tables"]["works"]
    assert client.post("/api/v1/backup", headers=admin, json=incomplete).status_code == 422

    broken = deepcopy(original)
    broken["tables"]["list_items"][0]["work_id"] = "unknown-work"
    assert client.post("/api/v1/backup", headers=admin, json=broken).status_code == 422
    assert store.detail(work_id)["title"] == "Example film"
    assert len(store.lists()) == 1



def test_catalogue_cleanup_merges_duplicate_sources_and_preserves_other_settings(client, admin):
    store, source_id, work_id, list_id = _library(client)
    first = store.save_source({
        "name": "Primary Kodi", "type": "kodi",
        "config": {"host": "nas.local", "port": 3306, "mariadb_write_enabled": True}
    })
    second = store.save_source({
        "name": "Another name, same SQL server", "type": "kodi",
        "config": {"host": "NAS.LOCAL", "port": 3306}
    })
    from home_media_catalog.connectors.base import Record
    store.apply_snapshot(first["id"], [Record("movie:7", "movie", "Movie", year=2021)])
    store.apply_snapshot(second["id"], [Record("movie:7", "movie", "Movie", year=2021)])
    pre = client.get("/api/v1/catalogue/maintenance/preview", headers=admin)
    assert pre.status_code == 200
    assert pre.json()["duplicate_sources"] == 1
    denied = client.post("/api/v1/catalogue/maintenance/clean", headers=admin)
    assert denied.status_code == 422
    done = client.post(
        "/api/v1/catalogue/maintenance/clean",
        headers={**admin, "X-Confirm-Catalogue-Maintenance": "clean"},
    )
    assert done.status_code == 200, done.text
    assert done.json()["merged_sources"] == 1
    assert "backup" not in done.json()
    assert not list(store.path.parent.glob("catalogue.before-cleanup.*.db"))
    remaining = store.sources()
    assert sum(src["type"] == "kodi" for src in remaining) == 1
    assert store.media()["total"] >= 2
    assert store.detail(work_id)["personal"]["user_rating"] == 9.0
    second_pass = client.post(
        "/api/v1/catalogue/maintenance/clean",
        headers={**admin, "X-Confirm-Catalogue-Maintenance": "clean"},
    )
    assert second_pass.status_code == 200
    assert second_pass.json()["merged_sources"] == 0


def test_catalogue_reset_preserves_access_but_clears_catalogue(client, admin):
    store, source_id, work_id, list_id = _library(client)
    store.register_device_token("phone-token", "iPhone", "admin")
    denied = client.post("/api/v1/catalogue/maintenance/reset", headers=admin)
    assert denied.status_code == 422
    response = client.post(
        "/api/v1/catalogue/maintenance/reset",
        headers={**admin, "X-Confirm-Catalogue-Maintenance": "reset"},
    )
    assert response.status_code == 200, response.text
    assert store.media()["total"] == 0
    assert store.sources() == []
    assert store.player("missing") is None
    assert client.get("/api/v1/catalogue/maintenance/preview", headers=admin).status_code == 200
    assert "backup" not in response.json()
    assert not list(store.path.parent.glob("catalogue.before-reset.*.db"))
    assert client.get("/api/v1/auth/session", headers=admin).status_code == 200
