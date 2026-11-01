"""Native JSON transport for physical music collections."""
import base64
import time


def test_native_json_music_import(client, admin):
    source = client.post(
        "/api/v1/sources", headers=admin,
        json={"name": "Mes CD", "type": "music_csv", "config": {}},
    )
    assert source.status_code == 201
    source_id = source.json()["id"]
    csv = b"Artist,Title,Format,Year\nAir,Moon Safari,CD,1998\n"
    endpoint = "/api/v1/imports/music/music_csv/json"
    assert client.post(endpoint, headers=admin, json={
        "source_id": source_id, "content_base64": "***not-base64***"
    }).status_code == 422
    accepted = client.post(endpoint, headers=admin, json={
        "source_id": source_id, "content_base64": base64.b64encode(csv).decode()
    })
    assert accepted.status_code == 202
    job_id = accepted.json()["id"]
    for _ in range(100):
        job = client.get(f"/api/v1/jobs/{job_id}", headers=admin).json()
        if job["status"] in ("completed", "failed"):
            break
        time.sleep(0.02)
    assert job["status"] == "completed", job
    status = client.get("/api/v1/music/status", headers=admin).json()
    assert status["albums"] == 1
    assert status["physical_albums"] == 1
    assert client.get("/api/v1/music/albums", headers=admin).json()["items"][0]["title"] == "Moon Safari"
    assert client.post("/api/v1/imports/music/discogs/json", headers=admin, json={
        "source_id": source_id, "content_base64": base64.b64encode(csv).decode()
    }).status_code == 422
