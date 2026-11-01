"""Music playlists are separate from video lists and the in-memory audio queue."""
from home_media_catalog.connectors.music_base import (
    MusicAlbumRecord, MusicSnapshot, MusicTrackRecord,
)
from home_media_catalog.music import MusicCatalogue


def test_music_playlist_crud_and_track_order(client, admin):
    source = client.post(
        "/api/v1/sources", headers=admin,
        json={"name": "Kodi", "type": "kodi", "config": {}},
    ).json()
    MusicCatalogue(client.app.state.store).apply_snapshot(
        source["id"],
        MusicSnapshot(
            albums=[MusicAlbumRecord(
                external_key="album:1", title="Test Album", artists=["Air"]
            )],
            tracks=[
                MusicTrackRecord(
                    external_key=f"song:{n}", title=f"Track {n}",
                    album_key="album:1", artists=["Air"], track=n,
                    file=f"smb://nas/audio/{n}.m4a", playable=True,
                ) for n in range(1, 4)
            ],
        ),
    )
    ids = [t["id"] for t in
           client.get("/api/v1/music/tracks", headers=admin).json()["items"]]
    new = client.post(
        "/api/v1/music/playlists", headers=admin, json={"name": "À écouter"},
    )
    assert new.status_code == 201, new.text
    playlist_id = new.json()["id"]
    assert client.get("/api/v1/music/playlists", headers=admin).json()[0]["track_count"] == 0
    added = client.post(
        f"/api/v1/music/playlists/{playlist_id}/tracks",
        headers=admin, json={"track_ids": [ids[2], ids[0], ids[2]]},
    )
    assert added.status_code == 200, added.text
    assert [t["id"] for t in added.json()["tracks"]] == [ids[2], ids[0], ids[2]]
    assert client.post(
        f"/api/v1/music/playlists/{playlist_id}/tracks", headers=admin,
        json={"track_ids": ["not-a-real-track"]},
    ).status_code == 422
    detail = client.get(f"/api/v1/music/playlists/{playlist_id}", headers=admin)
    assert len(detail.json()["tracks"]) == 3
    replaced = client.put(
        f"/api/v1/music/playlists/{playlist_id}/tracks",
        headers=admin, json={"track_ids": [ids[1], ids[0]]},
    )
    assert [t["id"] for t in replaced.json()["tracks"]] == [ids[1], ids[0]]
    renamed = client.patch(
        f"/api/v1/music/playlists/{playlist_id}", headers=admin,
        json={"name": "Road trip"},
    )
    assert renamed.json()["name"] == "Road trip"
    assert client.delete(
        f"/api/v1/music/playlists/{playlist_id}/tracks/0", headers=admin
    ).status_code == 204
    assert len(client.get(
        f"/api/v1/music/playlists/{playlist_id}", headers=admin
    ).json()["tracks"]) == 1
    assert client.delete(
        f"/api/v1/music/playlists/{playlist_id}", headers=admin
    ).status_code == 204
    assert client.get(
        f"/api/v1/music/playlists/{playlist_id}", headers=admin
    ).status_code == 404


def test_music_playlist_write_authentication_and_video_independence(client, admin):
    assert client.get("/api/v1/music/playlists").status_code == 401
    assert client.post(
        "/api/v1/music/playlists", json={"name": "No access"}
    ).status_code == 401
    row = client.post(
        "/api/v1/music/playlists", headers=admin, json={"name": "Music"},
    )
    assert row.status_code == 201
    assert client.get("/api/v1/lists", headers=admin).json() != [
        {"name": "Music"}]
