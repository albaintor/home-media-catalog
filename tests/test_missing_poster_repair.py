"""Regression coverage for server-side and iOS-uploaded missing posters."""
from io import BytesIO

from PIL import Image

from conftest import READ
from home_media_catalog.connectors.base import Record


def seed_without_artwork(store):
    source = store.save_source({"name": "CLZ test", "type": "clz"})["id"]
    records = [
        Record(
            "disc-one", "movie", "Repairable film", year=2018,
            identifiers={"imdb": "tt5463162"}, collections=["Film Saga"]
        ),
        Record(
            "disc-two", "movie", "Second film", year=2019,
            identifiers={"tmdb": "383499"}, collections=["Film Saga"]
        ),
    ]
    store.apply_snapshot(source, records)
    media_id = next(row["id"] for row in store.media()["items"]
                    if row["title"] == "Repairable film")
    return source, records, media_id


def poster_bytes():
    output = BytesIO()
    Image.new("RGB", (32, 48), (50, 70, 90)).save(output, format="JPEG")
    return output.getvalue()


def test_repaired_poster_persists_across_clz_resynchronization(store):
    source, records, media_id = seed_without_artwork(store)
    assert media_id in store.missing_poster_ids()
    before = {
        item["id"]: item["revision"] for item in store.collection_index()
    }
    reference = "https://image.tmdb.org/t/p/w500/safe-poster.jpg"
    assert store.save_poster_artwork(media_id, reference)
    assert not store.save_poster_artwork(media_id, reference)
    assert store.detail(media_id)["artwork"]["poster"] is not None
    assert store.artwork(media_id, "poster") == [reference]
    assert media_id not in store.missing_poster_ids()
    assert any(item["revision"] > before[item["id"]]
               for item in store.collection_index() if item["id"] in before)
    store.apply_snapshot(source, records)
    assert store.artwork(media_id, "poster") == [reference]
    store.save_enrichment(media_id, {"people": [], "trailers": []})
    assert store.artwork(media_id, "poster") == [reference]


def test_admin_binary_poster_upload_and_authenticated_artwork(client, admin):
    store = client.app.state.store
    _, _, media_id = seed_without_artwork(store)
    route = f"/api/v1/media/{media_id}/artwork/poster"
    jpeg = poster_bytes()
    assert client.post(route, content=jpeg,
                       headers={"Content-Type": "image/jpeg"}).status_code == 401
    assert client.post(route, content=jpeg, headers={
        "Authorization": f"Bearer {READ}",
        "Content-Type": "image/jpeg",
    }).status_code == 403
    assert client.post(route, content=jpeg, headers={
        **admin, "Content-Type": "application/octet-stream",
    }).status_code == 415
    assert client.post(route, content=b"<svg>not an image</svg>", headers={
        **admin, "Content-Type": "image/jpeg",
    }).status_code == 415
    assert client.post(route, content=jpeg + b" " * (5 * 1024 * 1024),
                       headers={**admin, "Content-Type": "image/jpeg"}).status_code == 413
    response = client.post(route, content=jpeg, headers={
        **admin, "Content-Type": "image/jpeg",
    })
    assert response.status_code == 200, response.text
    assert response.json()["artwork"]["poster"] == route
    response = client.get(route, headers=admin)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("image/jpeg")
    assert response.content == jpeg


def test_poster_repair_keeps_existing_provider_artwork(store):
    source = store.save_source({"name": "Kodi", "type": "kodi"})["id"]
    store.apply_snapshot(source, [
        Record("kodi-1", "movie", "Already decorated",
               identifiers={"imdb": "tt5463162"},
               poster=["https://image.tmdb.org/t/p/w500/original.jpg"])
    ])
    media_id = store.media()["items"][0]["id"]
    assert media_id not in store.missing_poster_ids()
    assert not store.save_poster_artwork(
        media_id, "https://image.tmdb.org/t/p/w500/override.jpg"
    )
    assert store.artwork(media_id, "poster") == [
        "https://image.tmdb.org/t/p/w500/original.jpg"
    ]
