"""Tests for source-aware Kodi write-back, without a running Kodi instance."""

import json
from unittest.mock import patch

import httpx
import pytest
from conftest import READ

from home_media_catalog.connectors.base import Record


def prepare_media(client, admin, kind="movie", with_player=True):
    sid = client.post(
        "/api/v1/sources",
        headers=admin,
        json={
            "name": "Kodi NAS",
            "type": "kodi",
            "config": {"host": "nas", "username": "reader"},
        },
    ).json()["id"]
    kodi_id = {"movie": 10, "tvshow": 20, "episode": 30}[kind]
    key = f"{kind}:{kodi_id}"
    client.app.state.store.apply_snapshot(
        sid,
        [
            Record(
                key,
                kind,
                "Example",
                identifiers={"tmdb": "123"},
                user_rating=4,
                watched=False,
                play_count=0 if kind != "tvshow" else None,
                season=1 if kind == "episode" else None,
                episode=2 if kind == "episode" else None,
                metadata={"kodi_id": kodi_id},
            )
        ],
    )
    if with_player:
        result = client.post(
            "/api/v1/players",
            headers=admin,
            json={"name": "Salon", "source_id": sid, "host": "kodi"},
        )
        assert result.status_code == 201
    mid = next(
        item["id"]
        for item in client.get(f"/api/v1/media?kind={kind}", headers=admin).json()["items"]
        if item["kind"] == kind
    )
    return sid, mid


def fake_kodi(handler):
    """Provide a real JSON-RPC request/response transport to the HTTP client."""
    real_client = httpx.AsyncClient

    def respond(request):
        body = json.loads(request.content)
        result = handler(body["method"], body["params"])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    return patch(
        "home_media_catalog.players.httpx.AsyncClient",
        side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
    )


def test_movie_rating_and_watched_are_written_to_kodi(client, admin):
    _sid, mid = prepare_media(client, admin)
    calls = []

    def respond(method, params):
        calls.append((method, params))
        if method == "JSONRPC.Permission":
            return {"ReadData": True, "UpdateData": True}
        if method == "VideoLibrary.GetMovieDetails":
            return {
                "moviedetails": {
                    "uniqueid": {"tmdb": "123"},
                    "title": "Example",
                    "playcount": 3,
                }
            }
        if method == "VideoLibrary.SetMovieDetails":
            return "OK"
        raise AssertionError(method)

    with fake_kodi(respond):
        response = client.patch(
            f"/api/v1/media/{mid}/personal",
            headers=admin,
            json={"user_rating": 7.5, "watched": True},
        )
    assert response.status_code == 200, response.text
    assert response.json()["user_rating"] == 7.5
    assert response.json()["watch_status"] == "watched"
    assert response.json()["writeback"][0]["status"] == "updated"
    assert "8/10" in response.json()["writeback"][0]["message"]
    assert calls[-1] == (
        "VideoLibrary.SetMovieDetails",
        {"movieid": 10, "userrating": 8, "playcount": 3},
    )
    assert len([c for c in calls if c[0] == "VideoLibrary.SetMovieDetails"]) == 1


def test_removing_imported_kodi_rating_writes_zero_and_does_not_resurface(client, admin):
    # The rating (4) comes only from the Kodi import: there is no personal value.
    _sid, mid = prepare_media(client, admin)
    calls = []

    def respond(method, params):
        calls.append((method, params))
        if method == "JSONRPC.Permission":
            return {"ReadData": True, "UpdateData": True}
        if method == "VideoLibrary.GetMovieDetails":
            return {"moviedetails": {"uniqueid": {"tmdb": "123"}, "title": "Example"}}
        if method == "VideoLibrary.SetMovieDetails":
            return "OK"
        raise AssertionError(method)

    with fake_kodi(respond):
        response = client.patch(
            f"/api/v1/media/{mid}/personal", headers=admin, json={"user_rating": None}
        )
    assert response.status_code == 200, response.text
    assert response.json()["user_rating"] is None
    assert response.json()["writeback"][0]["status"] == "updated"
    assert calls[-1] == ("VideoLibrary.SetMovieDetails", {"movieid": 10, "userrating": 0})
    assert client.get(f"/api/v1/media/{mid}", headers=admin).json()["user_rating"] is None


@pytest.mark.parametrize(
    ("kind", "set_method", "get_method", "id_param"),
    [
        ("tvshow", "SetTVShowDetails", "GetTVShowDetails", "tvshowid"),
        ("episode", "SetEpisodeDetails", "GetEpisodeDetails", "episodeid"),
    ],
)
def test_tvshow_and_episode_write_to_correct_jsonrpc_method(
    client, admin, kind, set_method, get_method, id_param
):
    _sid, mid = prepare_media(client, admin, kind)
    kodi_id = {"tvshow": 20, "episode": 30}[kind]
    seen = []

    def respond(method, params):
        seen.append((method, params))
        if method == "JSONRPC.Permission":
            return {"UpdateData": True}
        if method == "VideoLibrary." + get_method:
            key = "tvshowdetails" if kind == "tvshow" else "episodedetails"
            return {
                key: {
                    "uniqueid": {"tmdb": "123"},
                    "title": "Example",
                    "playcount": 0,
                    "season": 1,
                    "episode": 2,
                }
            }
        if method == "VideoLibrary.GetEpisodes" and kind == "tvshow":
            return {
                "episodes": [{"episodeid": 31, "tvshowid": 20, "playcount": 0}],
                "limits": {"total": 1},
            }
        if method == "VideoLibrary.SetEpisodeDetails" and kind == "tvshow":
            return "OK"
        if method == "VideoLibrary." + set_method:
            return "OK"
        raise AssertionError(method)

    with fake_kodi(respond):
        response = client.patch(
            f"/api/v1/media/{mid}/personal", headers=admin, json={"watched": True}
        )
    assert response.status_code == 200
    assert response.json()["writeback"][0]["status"] == "updated"
    if kind == "tvshow":
        assert seen[-1] == (
            "VideoLibrary.SetEpisodeDetails",
            {"episodeid": 31, "playcount": 1},
        )
        assert not any(method == "VideoLibrary.SetTVShowDetails" for method, _ in seen)
    else:
        assert seen[-1] == (
            "VideoLibrary." + set_method,
            {id_param: kodi_id, "playcount": 1},
        )


def test_no_player_reports_local_only_and_removing_rating_is_propagated(client, admin):
    _sid, mid = prepare_media(client, admin, with_player=False)
    response = client.patch(f"/api/v1/media/{mid}/personal", headers=admin, json={"user_rating": 9})
    assert response.status_code == 200
    assert response.json()["user_rating"] == 9
    assert response.json()["writeback"][0]["status"] == "unavailable"
    assert "JSON-RPC" in response.json()["writeback"][0]["message"]
    response = client.patch(
        f"/api/v1/media/{mid}/personal", headers=admin, json={"user_rating": None}
    )
    assert response.status_code == 200
    # Removing the rating must not restore the imported Kodi value (4); it is
    # reported (and queued) for Kodi like any other rating change.
    assert response.json()["user_rating"] is None
    assert response.json()["writeback"][0]["status"] == "unavailable"


def test_mismatched_kodi_identity_never_gets_updated(client, admin):
    _sid, mid = prepare_media(client, admin)
    calls = []

    def respond(method, params):
        calls.append(method)
        if method == "JSONRPC.Permission":
            return {"UpdateData": True}
        if method == "VideoLibrary.GetMovieDetails":
            return {"moviedetails": {"uniqueid": {"tmdb": "different"}, "title": "Other"}}
        raise AssertionError("Unexpected write: " + method)

    with fake_kodi(respond):
        response = client.patch(
            f"/api/v1/media/{mid}/personal", headers=admin, json={"watched": True}
        )
    assert response.status_code == 200
    assert response.json()["watch_status"] == "watched"
    assert response.json()["writeback"][0]["status"] == "failed"
    assert "SetMovieDetails" not in calls


def test_read_only_collection_and_read_token_never_write_to_kodi(client, admin):
    _sid, mid = prepare_media(client, admin)
    readonly = client.patch(
        f"/api/v1/media/{mid}/personal",
        headers={"Authorization": f"Bearer {READ}"},
        json={"watched": True},
    )
    assert readonly.status_code == 403

    sid = client.post(
        "/api/v1/sources", headers=admin, json={"name": "Collection", "type": "clz"}
    ).json()["id"]
    client.app.state.store.apply_snapshot(sid, [Record("physical", "movie", "Another")])
    item = next(
        x
        for x in client.get("/api/v1/media", headers=admin).json()["items"]
        if x["title"] == "Another"
    )
    response = client.patch(
        f"/api/v1/media/{item['id']}/personal",
        headers=admin,
        json={"watched": True, "user_rating": 6},
    )
    assert response.status_code == 200
    assert response.json()["writeback"] == []


def test_kodi_offline_retains_local_edit_and_reports_failure(client, admin):
    _sid, mid = prepare_media(client, admin)

    def respond(method, params):
        if method == "JSONRPC.Permission":
            return {"UpdateData": False}
        raise AssertionError("Unexpected mutation when UpdateData is denied")

    with fake_kodi(respond):
        response = client.patch(
            f"/api/v1/media/{mid}/personal", headers=admin, json={"user_rating": 6}
        )
    assert response.status_code == 200
    assert response.json()["user_rating"] == 6
    assert response.json()["writeback"][0]["status"] == "failed"
    assert "UpdateData" in response.json()["writeback"][0]["message"]


def test_personal_rating_uses_one_second_kodi_rpc_timeout(client, admin):
    """Personal changes must not wait 15 s for every unavailable Kodi RPC."""
    _sid, media_id = prepare_media(client, admin)

    def respond(method, params):
        if method == "JSONRPC.Permission":
            return {"ReadData": True, "UpdateData": True}
        if method == "VideoLibrary.GetMovieDetails":
            return {
                "moviedetails": {
                    "uniqueid": {"tmdb": "123"},
                    "title": "Example",
                    "playcount": 0,
                }
            }
        if method == "VideoLibrary.SetMovieDetails":
            return "OK"
        raise AssertionError(method)

    with fake_kodi(respond) as constructor:
        response = client.patch(
            f"/api/v1/media/{media_id}/personal",
            headers=admin,
            json={"user_rating": 8},
        )
    assert response.status_code == 200, response.text
    assert response.json()["writeback"][0]["method"] == "jsonrpc"
    assert constructor.call_args.kwargs["timeout"] == 1.0
