from unittest.mock import patch

from conftest import READ


def test_clean_kodi_requires_admin_and_player(client, admin):
    source = client.post(
        "/api/v1/sources",
        headers=admin,
        json={"name": "Kodi", "type": "kodi", "config": {"host": "nas", "username": "reader"}},
    ).json()
    sid = source["id"]
    endpoint = f"/api/v1/sources/{sid}/clean"
    assert client.post(endpoint).status_code == 401
    assert client.post(endpoint, headers={"Authorization": f"Bearer {READ}"}).status_code == 403
    assert client.post(endpoint, headers=admin).status_code == 422
    player = client.post(
        "/api/v1/players",
        headers=admin,
        json={"name": "Kodi TV", "source_id": sid, "host": "kodi"},
    ).json()
    assert client.post(endpoint, headers=admin, params={"player_id": "bad"}).status_code == 422

    async def fake_submit(source_id, selected):
        assert source_id == sid
        assert selected["id"] == player["id"]
        return {
            "id": "clean-job",
            "source_id": source_id,
            "created_at": "2026-10-25T00:00:00+00:00",
            "status": "pending",
            "operation": "kodi_clean",
        }

    with patch.object(client.app.state.sync, "submit_clean", side_effect=fake_submit) as submit:
        result = client.post(endpoint, headers=admin)
    assert result.status_code == 202, result.text
    assert result.json()["operation"] == "kodi_clean"
    submit.assert_awaited_once()


def test_clean_requires_confirmed_completion_before_catalogue_import():
    import asyncio
    from unittest.mock import AsyncMock

    from home_media_catalog.kodi_scan import KodiScanUnavailable
    from home_media_catalog.sync import SyncManager

    class Store:
        def __init__(self):
            self.finished = []

        def start_job(self, _job):
            pass

        def update_job_progress(self, *_args):
            pass

        def finish_job(self, _job, **kwargs):
            self.finished.append(kwargs)

    instance = Store()
    manager = SyncManager(instance, None)
    job = {"id": "j", "source_id": "s"}
    source = {"id": "s"}
    player = {"name": "Kodi", "config": {}}

    async def check():
        with (
            patch(
                "home_media_catalog.sync.clean_until_finished",
                new=AsyncMock(side_effect=KodiScanUnavailable("WebSocket unavailable")),
            ),
            patch.object(manager, "_run", new_callable=AsyncMock) as downstream,
        ):
            await manager._run_clean(job, source, player)
            downstream.assert_not_awaited()

    asyncio.run(check())
    assert instance.finished == [{"error": "WebSocket unavailable"}]



def test_clean_falls_back_to_http_without_claiming_completion():
    import asyncio
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    from home_media_catalog import kodi_scan
    from home_media_catalog.kodi_scan import KodiScanUnavailable

    calls = []

    @asynccontextmanager
    async def fake_connection(config, *, timeout):
        assert config == {"host": "kodi", "port": 8080}
        assert timeout == 6

        async def rpc(method, params):
            calls.append((method, params))
            return (
                {"RemoveData": True}
                if method == "JSONRPC.Permission"
                else "OK"
            )

        yield rpc

    async def run():
        with (
            patch.object(
                kodi_scan,
                "_websocket_clean_until_finished",
                new=AsyncMock(side_effect=KodiScanUnavailable("WebSocket disabled")),
            ),
            patch.object(kodi_scan, "connection", new=fake_connection),
        ):
            return await kodi_scan.clean_until_finished({"host": "kodi", "port": 8080})

    assert asyncio.run(run()) == "http_requested"
    assert calls == [
        ("JSONRPC.Permission", {}),
        ("VideoLibrary.Clean", {"showdialogs": False}),
    ]


def test_unconfirmed_http_clean_never_imports_intermediate_snapshot():
    import asyncio
    from unittest.mock import AsyncMock

    from home_media_catalog.sync import SyncManager

    class Store:
        def __init__(self):
            self.finished = []
            self.phases = []

        def start_job(self, _job):
            pass

        def update_job_progress(self, _, phase):
            self.phases.append(phase)

        def finish_job(self, _job, **kwargs):
            self.finished.append(kwargs)

    instance = Store()
    manager = SyncManager(instance, None)
    job = {"id": "j", "source_id": "s"}
    source = {"id": "s"}
    player = {"name": "Kodi", "config": {"host": "kodi"}}

    async def run():
        with (
            patch(
                "home_media_catalog.sync.clean_until_finished",
                new=AsyncMock(return_value="http_requested"),
            ),
            patch.object(manager, "_run", new_callable=AsyncMock) as downstream,
        ):
            await manager._run_clean(job, source, player)
            downstream.assert_not_awaited()

    asyncio.run(run())
    assert instance.finished[0]["result"]["kodi_clean"] == "requested_unconfirmed"
    assert instance.finished[0]["result"]["kodi_clean_method"] == "http"
    assert "kodi_clean_finished" not in instance.phases
