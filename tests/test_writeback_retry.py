"""Persistent outgoing writeback cache: newest edit wins, replay is manual."""

from unittest.mock import AsyncMock, patch

from conftest import READ

from home_media_catalog.connectors.base import ConnectorError, Record


def prepare_movie(client, admin, *, two_sources=False):
    result = client.post(
        "/api/v1/sources",
        headers=admin,
        json={"name": "Kodi NAS", "type": "kodi", "config": {"host": "nas", "username": "reader"}},
    )
    assert result.status_code == 201, result.text
    source = result.json()["id"]
    client.app.state.store.apply_snapshot(
        source,
        [
            Record(
                "movie:10",
                "movie",
                "Example",
                identifiers={"tmdb": "123"},
                metadata={"kodi_id": 10},
                watched=False,
            )
        ],
    )
    player = client.post(
        "/api/v1/players",
        headers=admin,
        json={"name": "Salon", "source_id": source, "host": "kodi"},
    )
    assert player.status_code == 201
    movie_id = client.get("/api/v1/media", headers=admin).json()["items"][0]["id"]
    return source, movie_id


def test_writeback_audit_keeps_ten_events_without_losing_pending(client, admin):
    sid, mid = prepare_movie(client, admin)
    store = client.app.state.store
    detail = store.detail(mid)
    store.stage_writebacks(
        mid, detail, {"watched": True}, [store.source(sid, private=True)]
    )
    assert store.writeback_pending_count() == 1

    result = {
        "source_id": sid,
        "source_name": "Kodi NAS",
        "source_type": "kodi",
        "status": "updated",
        "method": "jsonrpc",
        "attempts": [],
        "message": None,
    }
    for index in range(15):
        store.record_writebacks(mid, {"user_rating": index}, [result])

    with store.connect() as conn:
        audit_rows = conn.execute(
            "SELECT fields FROM writeback_audit ORDER BY created_at DESC,rowid DESC"
        ).fetchall()
    assert len(audit_rows) == 10
    assert len(store.writeback_history(work_id=mid)) == 10
    status = client.get("/api/v1/writebacks/status", headers=admin).json()
    assert status["pending_count"] == 1
    assert status["pending"][0]["changes"] == {"watched": True}
    assert len(status["recent"]) <= 10


def test_writeback_audit_pruned_on_startup(client, admin):
    sid, mid = prepare_movie(client, admin)
    store = client.app.state.store
    with store.connect() as conn:
        for index in range(13):
            conn.execute(
                "INSERT INTO writeback_audit "
                "(id,work_id,source_id,source_name,source_type,created_at,status,"
                "method,attempts,fields,message) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"legacy-{index}", mid, sid, "Kodi NAS", "kodi",
                    "2026-11-11T12:00:00+00:00", "updated", "jsonrpc",
                    "[]", "[]", None,
                ),
            )
    store.initialize()
    with store.connect() as conn:
        ids = [row["id"] for row in conn.execute(
            "SELECT id FROM writeback_audit ORDER BY created_at DESC,rowid DESC"
        )]
    assert ids == [f"legacy-{index}" for index in range(12, 2, -1)]


def test_error_queues_explicit_edit_and_manual_replay_with_icon(client, admin):
    sid, mid = prepare_movie(client, admin)
    with patch(
        "home_media_catalog.writeback._write_kodi",
        new_callable=AsyncMock,
        side_effect=ConnectorError("JSON-RPC refuse la modification"),
    ):
        response = client.patch(
            f"/api/v1/media/{mid}/personal",
            json={"watched": True, "user_rating": 8},
            headers=admin,
        )
    assert response.status_code == 200, response.text
    assert response.json()["writeback"][0]["status"] == "failed"
    assert response.json()["writeback_state"][0]["status"] == "pending"

    status = client.get("/api/v1/writebacks/status", headers=admin)
    assert status.status_code == 200
    assert status.json()["pending_count"] == 1
    pending = status.json()["pending"][0]
    assert pending["media_id"] == mid
    assert pending["source_id"] == sid
    assert pending["source_type"] == "kodi"
    assert pending["icon"] == "play.rectangle.on.rectangle"
    assert pending["changes"] == {"watched": True, "user_rating": 8}
    recent = status.json()["recent"][0]
    assert recent["media_id"] == mid
    assert recent["media_title"] == "Example"
    assert recent["updated_at"]
    assert (
        client.get(f"/api/v1/media/{mid}", headers=admin).json()["writeback_state"][0]["status"]
        == "pending"
    )

    calls = []

    async def ok(player, detail, editions, changes):
        calls.append(changes)
        return None

    with patch("home_media_catalog.writeback._write_kodi", side_effect=ok):
        replay = client.post(f"/api/v1/writebacks/pending/{pending['id']}/retry", headers=admin)
    assert replay.status_code == 200, replay.text
    assert replay.json()["results"][0]["result"]["method"] == "jsonrpc"
    assert replay.json()["pending_count"] == 0
    assert calls == [{"watched": True, "user_rating": 8}]
    state = client.get(f"/api/v1/media/{mid}", headers=admin).json()["writeback_state"]
    assert state[0]["status"] == "updated"
    assert state[0]["icon"] == "play.rectangle.on.rectangle"
    history = client.get(f"/api/v1/media/{mid}/writeback", headers=admin).json()
    assert len(history) == 2
    assert history[0]["status"] == "updated"
    assert history[0]["media_title"] == "Example"
    assert history[0]["created_at"]
    source_history = client.get(f"/api/v1/sources/{sid}/writeback", headers=admin).json()
    assert source_history[0]["media_title"] == "Example"


def test_repeated_edits_supersede_unsent_values_and_success_clears_only_edited_fields(
    client, admin
):
    _sid, mid = prepare_movie(client, admin)
    with patch(
        "home_media_catalog.writeback._write_kodi",
        new_callable=AsyncMock,
        side_effect=ConnectorError("offline"),
    ):
        for values in ({"user_rating": 4}, {"user_rating": 9}, {"watched": True}):
            r = client.patch(f"/api/v1/media/{mid}/personal", headers=admin, json=values)
            assert r.status_code == 200
    status = client.get("/api/v1/writebacks/status", headers=admin).json()
    assert status["pending_count"] == 1
    assert status["pending"][0]["changes"] == {"user_rating": 9, "watched": True}
    # A later successful change clears only its field, not unrelated failed ones.
    with patch(
        "home_media_catalog.writeback._write_kodi", new_callable=AsyncMock, return_value=None
    ):
        r = client.patch(f"/api/v1/media/{mid}/personal", headers=admin, json={"watched": False})
    assert r.status_code == 200
    status = client.get("/api/v1/writebacks/status", headers=admin).json()
    assert status["pending_count"] == 1
    assert status["pending"][0]["changes"] == {"user_rating": 9}

    # Resetting an override to null clears its queue entry, with no remote request.
    with patch("home_media_catalog.writeback._write_kodi") as write:
        r = client.patch(f"/api/v1/media/{mid}/personal", headers=admin, json={"user_rating": None})
    assert r.status_code == 200
    write.assert_not_called()
    assert client.get("/api/v1/writebacks/status", headers=admin).json()["pending_count"] == 0


def test_replay_never_replays_stale_local_values_or_readonly_tokens(client, admin):
    _sid, mid = prepare_movie(client, admin)
    with patch(
        "home_media_catalog.writeback._write_kodi",
        new_callable=AsyncMock,
        side_effect=ConnectorError("offline"),
    ):
        client.patch(f"/api/v1/media/{mid}/personal", headers=admin, json={"user_rating": 8})
    pending = client.get("/api/v1/writebacks/pending", headers=admin).json()[0]
    readonly = {"Authorization": f"Bearer {READ}"}
    assert client.get("/api/v1/writebacks/status", headers=readonly).status_code == 200
    assert (
        client.post(
            f"/api/v1/writebacks/pending/{pending['id']}/retry", headers=readonly
        ).status_code
        == 403
    )
    # Simulate a local change outside the public PATCH endpoint.
    client.app.state.store.update_personal(mid, {"user_rating": 7})
    with patch("home_media_catalog.writeback._write_kodi") as write:
        r = client.post(f"/api/v1/writebacks/pending/{pending['id']}/retry", headers=admin)
    assert r.status_code == 409
    write.assert_not_called()
    assert client.get("/api/v1/writebacks/status", headers=admin).json()["pending_count"] == 1


def test_bulk_retry_keeps_failures_and_returns_per_source_result(client, admin):
    _sid, mid = prepare_movie(client, admin)
    with patch(
        "home_media_catalog.writeback._write_kodi",
        new_callable=AsyncMock,
        side_effect=ConnectorError("offline"),
    ):
        client.patch(f"/api/v1/media/{mid}/personal", headers=admin, json={"watched": True})
        response = client.post("/api/v1/writebacks/retry", headers=admin)
    assert response.status_code == 200, response.text
    assert response.json()["pending_count"] == 1
    assert response.json()["results"][0]["result"]["status"] == "failed"
    pending = client.get("/api/v1/writebacks/pending", headers=admin).json()[0]
    assert pending["retry_count"] == 1
    with patch(
        "home_media_catalog.writeback._write_kodi", new_callable=AsyncMock, return_value=None
    ):
        response = client.post("/api/v1/writebacks/retry", headers=admin)
    assert response.status_code == 200
    assert response.json()["pending_count"] == 0


def test_importing_a_kodi_snapshot_does_not_push_upstream_or_retry_queue(client, admin):
    sid, mid = prepare_movie(client, admin)
    with patch(
        "home_media_catalog.writeback._write_kodi",
        new_callable=AsyncMock,
        side_effect=ConnectorError("offline"),
    ):
        client.patch(f"/api/v1/media/{mid}/personal", headers=admin, json={"watched": True})
    with patch("home_media_catalog.writeback._write_kodi") as remote:
        client.app.state.store.apply_snapshot(
            sid,
            [
                Record(
                    "movie:10",
                    "movie",
                    "Example",
                    identifiers={"tmdb": "123"},
                    metadata={"kodi_id": 10},
                    watched=False,
                )
            ],
        )
    remote.assert_not_called()
    assert client.get("/api/v1/writebacks/status", headers=admin).json()["pending_count"] == 1


def test_idempotent_patch_does_not_replay_failed_write(client, admin):
    _sid, mid = prepare_movie(client, admin)
    with patch(
        "home_media_catalog.writeback._write_kodi",
        new_callable=AsyncMock,
        side_effect=ConnectorError("offline"),
    ):
        first = client.patch(f"/api/v1/media/{mid}/personal", headers=admin, json={"watched": True})
    assert first.json()["writeback"][0]["status"] == "failed"
    with patch("home_media_catalog.writeback._write_kodi") as remote:
        repeated = client.patch(
            f"/api/v1/media/{mid}/personal", headers=admin, json={"watched": True}
        )
    assert repeated.status_code == 200
    assert repeated.json()["writeback"] == []
    remote.assert_not_called()
    assert client.get("/api/v1/writebacks/status", headers=admin).json()["pending_count"] == 1


def test_queue_survives_catalogue_store_reinitialization(client, admin):
    _sid, mid = prepare_movie(client, admin)
    with patch(
        "home_media_catalog.writeback._write_kodi",
        new_callable=AsyncMock,
        side_effect=ConnectorError("offline"),
    ):
        client.patch(f"/api/v1/media/{mid}/personal", headers=admin, json={"user_rating": 7})
    store = client.app.state.store
    store.initialize()
    entries = store.pending_writebacks()
    assert len(entries) == 1
    assert entries[0]["changes"] == {"user_rating": 7}
