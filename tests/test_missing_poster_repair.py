"""Regression coverage for server-side and iOS-uploaded missing posters."""
from io import BytesIO

from conftest import READ
from PIL import Image

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
    manual_id = store.save_manual_collection("Manually grouped saga")["id"]
    store.add_manual_collection_items(manual_id, [media_id])
    before = {
        item["id"]: item["revision"] for item in store.collection_index()
    }
    reference = "https://image.tmdb.org/t/p/w500/safe-poster.jpg"
    assert store.save_poster_artwork(media_id, reference)
    assert not store.save_poster_artwork(media_id, reference)
    assert store.detail(media_id)["artwork"]["poster"] is not None
    assert store.artwork(media_id, "poster") == [reference]
    assert media_id not in store.missing_poster_ids()
    after = {item["id"]: item["revision"] for item in store.collection_index()}
    assert any(revision > before[collection_id]
               for collection_id, revision in after.items() if collection_id in before)
    # Manual collection URLs must also change; otherwise previously cached
    # two-tile mosaics still show the missing poster placeholder.
    assert after[manual_id] > before[manual_id]
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


def test_visible_poster_request_repairs_missing_clz_artwork(tmp_path, monkeypatch):
    """Web/iOS lazy tiles must recover a poster even if summary artwork is null."""
    from fastapi.testclient import TestClient

    from home_media_catalog.app import create_app
    from home_media_catalog.settings import Settings

    app = create_app(Settings(
        data_dir=tmp_path, api_token="unit-test-admin-token-with-at-least-24-characters",
        tmdb_api_token="test-tmdb-token",
    ))
    app.state.store.initialize()
    _, _, media_id = seed_without_artwork(app.state.store)
    assert app.state.store.detail(media_id)["artwork"]["poster"] is None

    async def lookup(detail, token):
        assert detail["identifiers"]["imdb"] == ["tt5463162"]
        assert token == "test-tmdb-token"
        return "https://image.tmdb.org/t/p/w500/deadpool2.jpg"

    async def artwork(candidates, *, persist=True):
        from home_media_catalog.artwork import ArtworkError

        if not candidates:
            raise ArtworkError("No source artwork")
        assert candidates[0].endswith("deadpool2.jpg")
        return poster_bytes(), "image/jpeg"

    monkeypatch.setattr("home_media_catalog.app.resolve_tmdb_poster", lookup)
    monkeypatch.setattr(app.state.artwork, "get", artwork)
    with TestClient(app) as test:
        response = test.get(
            f"/api/v1/media/{media_id}/artwork/poster",
            headers={"Authorization": "Bearer unit-test-admin-token-with-at-least-24-characters"},
        )
        assert response.status_code == 200, response.text
        assert response.content == poster_bytes()
        assert app.state.store.detail(media_id)["artwork"]["poster"] is not None


def test_bad_clz_url_replaced_without_reimport(tmp_path, monkeypatch):
    """A nonempty but unreachable CLZ URL is NOT a valid downloaded poster."""
    from fastapi.testclient import TestClient

    from home_media_catalog.app import create_app
    from home_media_catalog.artwork import ArtworkError
    from home_media_catalog.settings import Settings

    app = create_app(Settings(
        data_dir=tmp_path, api_token="unit-test-admin-token-with-at-least-24-characters",
        tmdb_api_token="test-tmdb-token",
    ))
    store = app.state.store
    store.initialize()
    source = store.save_source({"name": "CLZ bad cover", "type": "clz"})["id"]
    record = Record(
        "deadpool-2", "movie", "Deadpool 2", year=2018,
        identifiers={"imdb": "tt5463162"}, collections=["Deadpool"],
        poster=["https://broken.example/cover.jpg"],
    )
    store.apply_snapshot(source, [record])
    media_id = store.media()["items"][0]["id"]
    assert media_id not in store.missing_poster_ids()

    async def lookup(detail, token):
        assert detail["identifiers"]["imdb"] == ["tt5463162"]
        return "https://image.tmdb.org/t/p/w500/deadpool2.jpg"

    async def artwork(candidates, *, persist=True):
        if candidates[0].startswith("https://broken.example/"):
            raise ArtworkError("CLZ artwork unreachable")
        return poster_bytes(), "image/jpeg"

    monkeypatch.setattr("home_media_catalog.app.resolve_tmdb_poster", lookup)
    monkeypatch.setattr(app.state.artwork, "get", artwork)
    with TestClient(app) as test:
        response = test.get(
            f"/api/v1/media/{media_id}/artwork/poster",
            headers={"Authorization": "Bearer unit-test-admin-token-with-at-least-24-characters"},
        )
        assert response.status_code == 200, response.text
        assert store.artwork(media_id, "poster")[0].endswith("deadpool2.jpg")
        store.apply_snapshot(source, [record])
        assert store.artwork(media_id, "poster")[0].endswith("deadpool2.jpg")
