"""Music REST API: sources, imports, browsing, export and streaming tickets."""

import time

from home_media_catalog.connectors.music_base import (
    MusicAlbumRecord,
    MusicSnapshot,
    MusicTrackRecord,
)
from home_media_catalog.music import MusicCatalogue

DISCOGS = (
    b"Catalog#,Artist,Title,Label,Format,Rating,Released,release_id,CollectionFolder,"
    b"Date Added,Collection Media Condition,Collection Sleeve Condition,Collection Notes\n"
    b'NODATA 02,Radiohead,OK Computer,Parlophone,"2xLP, Album, RE",5,1997,1234,'
    b"Uncategorized,2024-01-01,Mint (M),Near Mint (NM or M-),\n"
)


def _wait(client, admin, job_id):
    for _ in range(100):
        job = client.get(f"/api/v1/jobs/{job_id}", headers=admin).json()
        if job["status"] in ("completed", "failed"):
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_music_collection_import_and_browsing(client, admin):
    created = client.post(
        "/api/v1/sources",
        headers=admin,
        json={"name": "Mes vinyles", "type": "discogs", "config": {}},
    )
    assert created.status_code == 201
    source_id = created.json()["id"]
    # Music collections cannot be "synchronized" like Kodi sources.
    assert client.post(f"/api/v1/sources/{source_id}/sync", headers=admin).status_code == 422
    response = client.post(
        "/api/v1/imports/music/discogs",
        headers=admin,
        data={"source_id": source_id},
        files={"file": ("collection.csv", DISCOGS, "text/csv")},
    )
    assert response.status_code == 202
    assert response.json()["operation"] == "music_import"
    job = _wait(client, admin, response.json()["id"])
    assert job["status"] == "completed", job
    assert job["result"]["music_albums"] == 1

    status = client.get("/api/v1/music/status", headers=admin).json()
    assert status["albums"] == 1 and status["physical_albums"] == 1
    assert status["formats"] == {"Vinyl": 1} and status["revision"] == 1
    albums = client.get("/api/v1/music/albums", headers=admin, params={"format": "Vinyl"}).json()
    assert albums["total"] == 1
    album = albums["items"][0]
    assert album["title"] == "OK Computer" and album["owned_physical"]
    detail = client.get(f"/api/v1/music/albums/{album['id']}", headers=admin).json()
    assert detail["editions"][0]["catalog_number"] == "NODATA 02"
    artist = client.get(f"/api/v1/music/artists/{album['artist_id']}", headers=admin).json()
    assert artist["name"] == "Radiohead" and artist["albums"][0]["id"] == album["id"]
    search = client.get("/api/v1/music/search", headers=admin, params={"q": "computer"}).json()
    assert [a["id"] for a in search["albums"]] == [album["id"]]
    facets = client.get("/api/v1/music/facets", headers=admin).json()
    assert facets["formats"] == ["Vinyl"] and facets["years"] == [1997]
    sources = client.get("/api/v1/sources", headers=admin).json()
    assert next(s for s in sources if s["id"] == source_id)["music_count"] == 1

    # Wrong collection type for this source.
    wrong = client.post(
        "/api/v1/imports/music/clz_music",
        headers=admin,
        data={"source_id": source_id},
        files={"file": ("x.xml", b"<musicinfo/>", "text/xml")},
    )
    assert wrong.status_code == 422
    assert client.delete(f"/api/v1/sources/{source_id}", headers=admin).status_code == 204
    assert client.get("/api/v1/music/status", headers=admin).json()["albums"] == 0


def test_music_export_and_stream_ticket(client, admin):
    source = client.post(
        "/api/v1/sources",
        headers=admin,
        json={"name": "Kodi", "type": "kodi", "config": {"music_enabled": True}},
    ).json()
    assert source["config"]["music_enabled"] is True
    store = client.app.state.store
    MusicCatalogue(store).apply_snapshot(
        source["id"],
        MusicSnapshot(
            albums=[MusicAlbumRecord(external_key="album:1", title="Moon Safari", artists=["Air"])],
            tracks=[
                MusicTrackRecord(
                    external_key=f"song:{i}",
                    title=f"Track {i}",
                    album_key="album:1",
                    artists=["Air"],
                    track=i,
                    file=f"smb://nas/music/air/{i:02}.flac",
                    playable=True,
                )
                for i in range(1, 6)
            ],
        ),
    )
    page = client.get("/api/v1/music/export/tracks", headers=admin, params={"limit": 3}).json()
    assert len(page["items"]) == 3 and page["next"] is not None and page["total"] == 5
    rest = client.get(
        "/api/v1/music/export/tracks", headers=admin, params={"after": page["next"]}
    ).json()
    assert len(rest["items"]) == 2 and rest["next"] is None
    player = client.post(
        "/api/v1/players",
        headers=admin,
        json={"name": "Salon", "source_id": source["id"], "host": "kodi.local"},
    )
    assert player.status_code == 201, player.text
    track_id = page["items"][0]["id"]
    ticket = client.post(
        f"/api/v1/players/{player.json()['id']}/music-stream",
        headers=admin,
        json={"track_id": track_id},
    )
    assert ticket.status_code == 200, ticket.text
    assert ticket.json()["path"].endswith("/audio.flac")
    assert client.get("/api/v1/music/albums", headers=admin).json()["items"][0]["playable"]
    unauthenticated = client.get("/api/v1/music/albums")
    assert unauthenticated.status_code == 401
