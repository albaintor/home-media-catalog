import time

from conftest import ADMIN, INGRESS, READ

from home_media_catalog.connectors.base import Record


def test_search_matches_localized_and_original_titles_in_both_scopes(client, admin):
    """Title-only and advanced search must both include original and localized titles."""
    source = client.post(
        "/api/v1/sources", headers=admin, json={"name": "Original title test", "type": "clz"}
    ).json()["id"]
    client.app.state.store.apply_snapshot(
        source,
        [
            Record("original-title-work", "movie", "Le Jardin des nuages",
                   original_title="The Floating Garden"),
            Record("other-title-work", "movie", "Un autre film",
                   original_title="Another Movie"),
        ],
    )
    for advanced in (False, True):
        scope = "&deep_search=true" if advanced else ""
        for query, expected in (("jardin", "Le Jardin des nuages"),
                                ("floating", "Le Jardin des nuages"),
                                ("another", "Un autre film")):
            response = client.get(
                f"/api/v1/media?q={query}{scope}", headers=admin
            )
            assert response.status_code == 200
            titles = [entry["title"] for entry in response.json()["items"]]
            assert titles == [expected], (query, advanced, titles)


def test_auth_required_and_read_only_token(client):
    assert client.get("/api/v1/media").status_code == 401
    read = {"Authorization": f"Bearer {READ}"}
    assert client.get("/api/v1/media", headers=read).status_code == 200
    assert (
        client.post("/api/v1/sources", headers=read, json={"name": "X", "type": "clz"}).status_code
        == 403
    )
    assert client.get("/api/v1/openapi.json").status_code == 401
    assert (
        client.get("/api/v1/media", headers={"X-Kodi-Library-Ingress-Token": INGRESS}).status_code
        == 200
    )
    assert (
        client.get(
            "/api/v1/media",
            headers={"X-Ingress-Path": "/spoof", "X-Kodi-Library-Ingress-Token": "wrong"},
        ).status_code
        == 401
    )


def test_browser_session_csrf_logout_and_no_bearer_fallback(client):
    login = client.post("/api/v1/auth/session", json={"token": ADMIN})
    assert login.status_code == 200
    assert "HttpOnly" in login.headers["set-cookie"]
    assert "samesite=lax" in login.headers["set-cookie"].lower()
    assert client.get("/api/v1/media").status_code == 200
    assert client.get("/api/v1/media", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/api/v1/sources", json={"name": "X", "type": "clz"}).status_code == 403
    csrf = {"X-CSRF-Token": login.json()["csrf_token"]}
    assert (
        client.post("/api/v1/sources", headers=csrf, json={"name": "X", "type": "clz"}).status_code
        == 201
    )
    assert client.delete("/api/v1/auth/session", headers=csrf).status_code == 204
    assert client.get("/api/v1/media").status_code == 401


def test_read_session_can_logout_and_rate_limit(client):
    login = client.post("/api/v1/auth/session", json={"token": READ})
    assert (
        client.delete(
            "/api/v1/auth/session", headers={"X-CSRF-Token": login.json()["csrf_token"]}
        ).status_code
        == 204
    )
    for _ in range(10):
        assert client.post("/api/v1/auth/session", json={"token": "invalid"}).status_code == 401
    assert client.post("/api/v1/auth/session", json={"token": "invalid"}).status_code == 429


def test_source_secrets_redacted_and_null_password_preserves_existing(client, admin):
    body = {
        "name": "Kodi",
        "type": "kodi",
        "config": {
            "host": "nas",
            "username": "reader",
            "password": "private-secret",
            "database": "MyVideos131",
        },
    }
    response = client.post("/api/v1/sources", headers=admin, json=body)
    assert response.status_code == 201
    sid = response.json()["id"]
    assert "private-secret" not in response.text
    assert response.json()["config"]["has_password"]
    body["config"]["password"] = None
    assert client.put(f"/api/v1/sources/{sid}", headers=admin, json=body).status_code == 200
    assert (
        client.app.state.store.source(sid, private=True)["config"]["password"] == "private-secret"
    )
    assert "private-secret" not in client.get("/api/v1/status", headers=admin).text


def test_validation_parameters_and_redacted_errors(client, admin):
    assert client.get("/api/v1/media?limit=201", headers=admin).status_code == 422
    assert client.get("/api/v1/media?sort=drop", headers=admin).status_code == 422
    response = client.post(
        "/api/v1/sources",
        headers=admin,
        json={
            "name": "X",
            "type": "kodi",
            "config": {
                "host": "nas",
                "username": "u",
                "password": "private-secret",
                "database": "MyVideos;DROP",
            },
        },
    )
    assert response.status_code == 422
    assert "private-secret" not in response.text
    assert response.json()["error"]["code"] == "invalid_request"


def test_api_pagination_detail_facets_and_artwork_auth(client, admin):
    sid = client.post("/api/v1/sources", headers=admin, json={"name": "X", "type": "clz"}).json()[
        "id"
    ]
    client.app.state.store.apply_snapshot(
        sid,
        [
            Record(
                "one",
                "movie",
                "Example",
                genres=["Adventure"],
                formats=["4K UHD"],
                poster=["https://example.com/cover.jpg"],
            )
        ],
    )
    page = client.get("/api/v1/media?genre=Adventure&format=4K%20UHD", headers=admin).json()
    assert page["total"] == 1 and not page["has_more"]
    mid = page["items"][0]["id"]
    assert (
        client.get(f"/api/v1/media/{mid}", headers=admin).json()["editions"][0]["external_key"]
        == "one"
    )
    assert client.get("/api/v1/facets", headers=admin).json()["formats"] == ["Blu-ray 4K"]
    assert client.get("/api/v1/media?format=4K", headers=admin).json()["total"] == 0
    assert client.get("/api/v1/media?format=Blu-ray%204K", headers=admin).json()["total"] == 1
    assert client.get("/api/v1/media?format=4K&format=HDR10%2B", headers=admin).json()["total"] == 0
    assert client.get(page["items"][0]["artwork"]["poster"]).status_code == 401
    assert client.get("/api/v1/media/missing", headers=admin).status_code == 404


def test_async_import_and_invalid_xml_retains_previous_data(client, admin, clz_xml):
    sid = client.post("/api/v1/sources", headers=admin, json={"name": "CLZ", "type": "clz"}).json()[
        "id"
    ]

    def upload(data):
        response = client.post(
            "/api/v1/imports/clz",
            headers=admin,
            data={"source_id": sid},
            files={"file": ("export.xml", data, "application/xml")},
        )
        assert response.status_code == 202
        for _ in range(200):
            job = client.get("/api/v1/jobs/" + response.json()["id"], headers=admin).json()
            if job["status"] in ("completed", "failed"):
                return job
            time.sleep(0.01)
        raise AssertionError("job did not complete")

    assert upload(clz_xml)["status"] == "completed"
    assert client.get("/api/v1/status", headers=admin).json()["editions"] == 4
    assert upload(b"not xml")["status"] == "failed"
    assert client.get("/api/v1/status", headers=admin).json()["editions"] == 4


def test_openapi_contract_is_independent_of_ui(client, admin):
    schema = client.get("/api/v1/openapi.json", headers=admin).json()
    assert schema["components"]["securitySchemes"]["BearerToken"]["scheme"] == "bearer"
    assert "/api/v1/media" in schema["paths"]
    assert "/api/v1/imports/clz" in schema["paths"]
    assert "/api/v1/players/{player_id}/play" in schema["paths"]
    assert schema["components"]["schemas"]["MediaPage"]["properties"]["items"]["items"][
        "$ref"
    ].endswith("MediaSummary")


def test_api_does_not_require_frontend_cookie(client, admin):
    assert not client.cookies
    response = client.get("/api/v1/media?q=anything&sort=title&order=asc", headers=admin)
    assert response.status_code == 200
    assert response.json()["items"] == []
    assert not client.cookies


def test_jobs_feed_is_authorized_and_lists_active_or_completed_work(client, admin):
    assert client.get("/api/v1/jobs").status_code == 401
    sid = client.post(
        "/api/v1/sources",
        headers=admin,
        json={
            "name": "Kodi",
            "type": "kodi",
            "config": {"host": "localhost", "username": "test"},
        },
    ).json()["id"]
    store = client.app.state.store
    job = store.new_job(sid)
    store.start_job(job["id"])
    read = {"Authorization": f"Bearer {READ}"}
    listed = client.get("/api/v1/jobs", headers=read)
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == job["id"]
    assert listed.json()[0]["status"] == "running"
    store.finish_job(job["id"], result={"removed_editions": 1, "database": "MyVideos131"})
    latest = client.get("/api/v1/jobs", headers=admin).json()[0]
    assert latest["status"] == "completed"
    assert latest["result"]["removed_editions"] == 1


def test_media_diagnostics_checks_movie_and_reports_all_sources(client, admin, monkeypatch):
    sid = client.post(
        "/api/v1/sources",
        headers=admin,
        json={
            "name": "NAS Kodi",
            "type": "kodi",
            "config": {"host": "nas", "username": "reader", "database": "MyVideos131"},
        },
    ).json()["id"]
    store = client.app.state.store
    store.apply_snapshot(
        sid,
        [
            Record(
                "movie:99:77",
                "movie",
                "Removed title",
                metadata={"kodi_id": 99, "file_id": 77, "database": "MyVideos131"},
            )
        ],
    )
    item = store.media()["items"][0]
    from home_media_catalog.connectors.kodi import KodiConnector

    monkeypatch.setattr(
        KodiConnector,
        "inspect_movie_links",
        lambda self, ids: {
            "database": "MyVideos131",
            "movies": {},
            "library_visible_movie_ids": [],
            "library_rule": "movie_view.isDefaultVersion = 1",
            "library_verified": True,
        },
    )
    read = {"Authorization": f"Bearer {READ}"}
    assert client.get(f"/api/v1/media/{item['id']}/diagnostics", headers=read).status_code == 403
    response = client.get(f"/api/v1/media/{item['id']}/diagnostics", headers=admin)
    assert response.status_code == 200, response.text
    diag = response.json()
    edition = diag["sources"][0]["editions"][0]
    assert diag["sources"][0]["queried_database"] == "MyVideos131"
    assert edition["exists_in_movie"] is False


def test_media_diagnostics_identifies_sql_movie_absent_from_navigation(client, admin, monkeypatch):
    from home_media_catalog.connectors.kodi import KodiConnector

    sid = client.post(
        "/api/v1/sources",
        headers=admin,
        json={
            "name": "Kodi",
            "type": "kodi",
            "config": {"host": "nas", "username": "reader", "database": "MyVideos131"},
        },
    ).json()["id"]
    store = client.app.state.store
    store.apply_snapshot(
        sid,
        [Record("movie:7:8", "movie", "Phantom", metadata={"kodi_id": 7, "file_id": 8})],
    )
    movie = store.media()["items"][0]
    store.save_player({"name": "Salon", "source_id": sid, "host": "192.168.1.3", "port": 8080})
    monkeypatch.setattr(
        KodiConnector,
        "inspect_movie_links",
        lambda self, ids: {
            "database": "MyVideos131",
            "library_visible_movie_ids": [],
            "library_rule": "movie_view.isDefaultVersion = 1",
            "library_verified": True,
            "movies": {
                7: {
                    "title": "Phantom",
                    "default_file_id": 8,
                    "file_row_exists": True,
                    "path_row_exists": True,
                    "file": "smb://nas/films/phantom.mkv",
                    "excluded_from_scan": 0,
                    "scan_disabled": 0,
                    "versions": [],
                }
            },
        },
    )

    response = client.get(f"/api/v1/media/{movie['id']}/diagnostics", headers=admin)
    assert response.status_code == 200, response.text
    info = response.json()["sources"][0]
    assert info["editions"][0]["sql_links"]["file_row_exists"] is True
    assert info["editions"][0]["exists_in_movie"] is True
    assert info["editions"][0]["in_library"] is False
    assert info["library_verified"] is True


def test_mobile_contract_announced_without_changing_versioned_endpoint(client, admin):
    # Existing mobile builds keep using /api/v1/status; new builds can
    # negotiate a future breaking data-model change before catalogue requests.
    status = client.get("/api/v1/status", headers=admin)
    assert status.status_code == 200
    assert status.json()["api_version"] == "v1"
    assert status.json()["mobile_compatibility"] == {"min_contract": 1, "max_contract": 5}
    assert status.json()["data_model_version"] == 10
    assert status.json()["version"].endswith("+model.6")

    discovery = client.get("/api/v1/discovery")
    assert discovery.status_code == 200
    assert discovery.json()["mobile_compatibility"] == {"min_contract": 1, "max_contract": 5}
    assert discovery.json()["data_model_version"] == status.json()["data_model_version"]
    assert discovery.json()["server_version"] == status.json()["version"]

    assert client.get("/api/v1/status").status_code == 401
    assert client.get("/api/v1/media", headers=admin).status_code == 200


def test_on_open_refresh_only_after_24_hours_and_never_duplicates(client, admin):
    """The web/mobile opening endpoint must not create jobs for recent sources."""
    from datetime import UTC, datetime, timedelta
    from unittest.mock import patch

    from conftest import READ

    def source(label):
        response = client.post(
            "/api/v1/sources",
            headers=admin,
            json={
                "name": label,
                "type": "kodi",
                "config": {"host": "nas", "username": "reader"},
            },
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    stale_id = source("Older than 24 hours")
    recent_id = source("Updated an hour ago")
    store = client.app.state.store
    now = datetime.now(UTC)
    with store.connect() as conn:
        conn.execute(
            "UPDATE sources SET last_sync=? WHERE id=?",
            ((now - timedelta(hours=25)).isoformat(timespec="seconds"), stale_id),
        )
        conn.execute(
            "UPDATE sources SET last_sync=? WHERE id=?",
            ((now - timedelta(hours=1)).isoformat(timespec="seconds"), recent_id),
        )

    sync = client.app.state.sync
    recorded = []

    async def submit(source_id):
        recorded.append(source_id)
        sync.active.add(source_id)
        return {
            "id": "test-job",
            "source_id": source_id,
            "status": "pending",
            "created_at": now.isoformat(timespec="seconds"),
        }

    readonly = {"Authorization": f"Bearer {READ}"}
    with patch.object(sync, "submit", side_effect=submit):
        first = client.post("/api/v1/sources/sync-stale", headers=readonly)
        assert first.status_code == 202, first.text
        assert [item["source_id"] for item in first.json()] == [stale_id]
        # A second client can open while the first import is still running.
        second = client.post("/api/v1/sources/sync-stale", headers=readonly)
        assert second.status_code == 202
        assert second.json() == []
        assert recorded == [stale_id]

        sync.active.discard(stale_id)
        with store.connect() as conn:
            conn.execute(
                "UPDATE sources SET last_sync=? WHERE id=?",
                (datetime.now(UTC).isoformat(timespec="seconds"), stale_id),
            )
        third = client.post("/api/v1/sources/sync-stale", headers=readonly)
        assert third.status_code == 202
        assert third.json() == []
        assert recorded == [stale_id]


def test_no_periodic_scheduler_and_model_part_of_release():
    from home_media_catalog import DATA_MODEL_VERSION, __version__
    from home_media_catalog.sync import SyncManager

    assert not hasattr(SyncManager, "_schedule")
    assert __version__.endswith(f"+model.{DATA_MODEL_VERSION}")


def test_empty_catalogue_bootstrap_is_one_time_not_periodic(client, admin):
    import asyncio
    from datetime import UTC, datetime
    from unittest.mock import patch

    source = client.post(
        "/api/v1/sources",
        headers=admin,
        json={
            "name": "Kodi NAS",
            "type": "kodi",
            "config": {"host": "nas", "username": "reader"},
        },
    ).json()["id"]
    manager = client.app.state.sync
    store = client.app.state.store
    started = []

    async def fake_submit(source_id):
        started.append(source_id)
        manager.active.add(source_id)
        return {
            "id": "boot-job",
            "source_id": source_id,
            "status": "pending",
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }

    with patch.object(manager, "submit", side_effect=fake_submit):
        initial = asyncio.run(manager.bootstrap_empty_catalogue())
        assert [job["source_id"] for job in initial] == [source]
        # A second server/bootstrap check while the import runs does nothing.
        assert asyncio.run(manager.bootstrap_empty_catalogue()) == []
        manager.active.clear()
        store.apply_snapshot(source, [Record("movie:1", "movie", "Film")])
        assert asyncio.run(manager.bootstrap_empty_catalogue()) == []
        assert started == [source]
        # Even with a stale timestamp, populated data does not cause a
        # background import at startup. Only client-open checks may do so.
        with store.connect() as conn:
            conn.execute(
                "UPDATE sources SET last_sync='2020-01-01T00:00:00+00:00' WHERE id=?",
                (source,),
            )
        assert asyncio.run(manager.bootstrap_empty_catalogue()) == []
        # Opening the UI after the 24h threshold triggers exactly one import.
        assert len(asyncio.run(manager.sync_stale_on_open())) == 1
        assert started == [source, source]


def test_bulk_add_ai_results_to_new_and_existing_lists(client, admin):
    source = client.app.state.store.save_source({"name": "Batch", "type": "clz"})
    client.app.state.store.apply_snapshot(
        source["id"],
        [Record(str(i), "movie", f"AI match {i}") for i in range(4)],
    )
    all_media = client.get("/api/v1/media", headers=admin).json()["items"]
    ids = [item["id"] for item in all_media]
    new_list = client.post("/api/v1/lists", json={"name": "AI picks"}, headers=admin).json()
    url = f"/api/v1/lists/{new_list['id']}/items/bulk"
    first = client.post(url, headers=admin, json={"media_ids": ids + [ids[0]]})
    assert first.status_code == 200, first.text
    assert first.json() == {"added": 4, "already_present": 0, "total_requested": 4}
    page = client.get(f"/api/v1/lists/{new_list['id']}/items", headers=admin).json()
    assert [item["id"] for item in page["items"]] == ids
    second = client.post(url, headers=admin, json={"media_ids": ids[:3]})
    assert second.status_code == 200
    assert second.json() == {"added": 0, "already_present": 3, "total_requested": 3}
    after = client.get("/api/v1/lists", headers=admin).json()
    assert next(item for item in after if item["id"] == new_list["id"])["item_count"] == 4

    # Invalid media IDs must not partially add preceding valid IDs.
    other = client.post("/api/v1/lists", json={"name": "Other"}, headers=admin).json()
    invalid = client.post(
        f"/api/v1/lists/{other['id']}/items/bulk",
        headers=admin,
        json={"media_ids": [ids[0], "nonexistent"]},
    )
    assert invalid.status_code == 404
    assert client.get(f"/api/v1/lists/{other['id']}/items", headers=admin).json()["total"] == 0

    read = {"Authorization": "Bearer " + READ}
    assert client.post(url, headers=read, json={"media_ids": ids}).status_code == 403
    assert client.post(url, headers=admin, json={"media_ids": []}).status_code == 422


def test_mobile_discovery_advertises_optional_features_and_contract(client, admin):
    discovery = client.get("/api/v1/discovery")
    assert discovery.status_code == 200
    body = discovery.json()
    assert body["api_version"] == "v1"
    assert body["mobile_compatibility"] == {"min_contract": 1, "max_contract": 5}
    assert {"ai_search", "bulk_list_add"} <= set(body["capabilities"])
    assert body["data_model_version"] >= 6
    status = client.get("/api/v1/status", headers=admin).json()
    assert status["version"] == body["server_version"]
    assert status["data_model_version"] == body["data_model_version"]

def test_batch_media_details_matches_individual_endpoint(client, admin):
    source = client.post(
        "/api/v1/sources", headers=admin,
        json={"name": "Batch test", "type": "clz"},
    )
    assert source.status_code == 201, source.text
    source_id = source.json()["id"]
    store = client.app.state.store
    store.apply_snapshot(
        source_id,
        [Record("movie:1", "movie", "First Film"),
         Record("movie:2", "movie", "Second Film")],
    )
    ids = [row["id"] for row in store.media(kind="movie")["items"]]
    assert len(ids) == 2
    from conftest import READ
    read = {"Authorization": f"Bearer {READ}"}
    route = "/api/v1/media/details/batch"
    assert client.post(route, json={"ids": ids}).status_code == 401
    result = client.post(route, headers=read, json={"ids": ids})
    assert result.status_code == 200, result.text
    expected = {
        mid: client.get(f"/api/v1/media/{mid}", headers=read).json() for mid in ids
    }
    assert {item["id"]: item for item in result.json()["items"]} == expected
    # Repeated IDs are deduplicated; deleted IDs are omitted, never fabricated.
    dup = client.post(route, headers=read, json={"ids": [ids[0], ids[0], "missing"]})
    assert dup.status_code == 200
    assert [item["id"] for item in dup.json()["items"]] == [ids[0]]
    for invalid in [[], [""], ["x"] * 101, "not an array"]:
        response = client.post(route, headers=read, json={"ids": invalid})
        assert response.status_code == 422, response.text
    assert "media_details_batch" in client.get("/api/v1/discovery").json()["capabilities"]


