"""Materialized collection index, migrations and revisioned cover behavior."""

import io

from PIL import Image

from home_media_catalog.connectors.base import Record
from home_media_catalog.list_thumbnails import compose


def _source(store):
    return store.save_source({"name": "Physical library", "type": "clz"})["id"]


def test_collection_index_reuses_media_and_exposes_singletons(store):
    source = _source(store)
    store.apply_snapshot(
        source,
        [
            Record("avengers-1", "movie", "Avengers One", year=2012,
                   collections=["Avengers"]),
            Record("avengers-2", "movie", "Avengers Two", year=2015,
                   collections=["Avengers", "Superheroes"]),
            Record("solo", "movie", "Solo Film", year=2020,
                   collections=["Solo Series"]),
        ],
    )
    groups = {item["name"]: item for item in store.collection_index()}
    assert groups["Avengers"]["item_count"] == 2
    assert groups["Superheroes"]["item_count"] == 1
    assert groups["Superheroes"]["single_media"]["title"] == "Avengers Two"
    assert groups["Solo Series"]["single_media"]["title"] == "Solo Film"
    identifier = groups["Avengers"]["id"]
    assert identifier.startswith("collection-")
    assert store.media(collection_id=identifier)["total"] == 2
    assert store.media(collection_id=identifier, limit=1)["has_more"]
    assert store.collection_thumbnail_input(identifier)["revision"] == groups["Avengers"]["revision"]

    # Metadata and membership edits are reflected without a full index rebuild.
    store.apply_snapshot(
        source,
        [
            Record("avengers-1", "movie", "Avengers One", year=2012,
                   collections=["Avengers"]),
            Record("solo", "movie", "Solo Film", year=2020,
                   collections=["Solo Series"]),
        ],
    )
    updated = {item["name"]: item for item in store.collection_index()}
    assert updated["Avengers"]["item_count"] == 1
    assert updated["Avengers"]["revision"] > groups["Avengers"]["revision"]
    assert "Superheroes" not in updated
    assert store.media(collection_id=identifier)["total"] == 1


def test_collection_index_migrates_existing_catalogue(store):
    source = _source(store)
    store.apply_snapshot(source, [
        Record("one", "movie", "Film One", collections=["Saga - Saga"])
    ])
    with store.connect() as conn:
        conn.execute("DELETE FROM collection_items")
        conn.execute("DELETE FROM collections")
        conn.execute("DELETE FROM catalogue_meta WHERE key='collections_index_v1'")
    store.initialize()
    groups = store.collection_index()
    assert len([g for g in groups if g["name"] == "Saga - Saga"]) == 1
    assert store.media(collection_id=next(
        g["id"] for g in groups if g["name"] == "Saga - Saga"
    ))["total"] == 1


def test_collection_portrait_cover_preserves_existing_list_cover():
    portrait = compose([None, None], width=300, height=450)
    landscape = compose([None, None])
    with Image.open(io.BytesIO(portrait)) as result:
        assert result.size == (300, 450)
    with Image.open(io.BytesIO(landscape)) as result:
        assert result.size == (360, 216)


def test_collection_endpoints(client, admin):
    store = client.app.state.store
    source = _source(store)
    store.apply_snapshot(source, [
        Record("one", "movie", "Single", collections=["Only One"])
    ])
    assert client.get("/api/v1/collections").status_code == 401
    response = client.get("/api/v1/collections", headers=admin)
    assert response.status_code == 200
    (group,) = [item for item in response.json() if item["name"] == "Only One"]
    assert group["single_media"]["title"] == "Single"
    page = client.get("/api/v1/collections/" + group["id"] + "/media", headers=admin)
    assert page.status_code == 200
    assert page.json()["total"] == 1
    assert client.get("/api/v1/media", params={"collection_id":group["id"]},
                      headers=admin).json()["total"] == 1


def test_media_collection_filter_accepts_multiple_values(client, admin):
    """Selected collections are a union; other filters still intersect."""
    store = client.app.state.store
    source = _source(store)
    store.apply_snapshot(source, [
        Record("first", "movie", "First Film", year=2000, collections=["Alpha"]),
        Record("second", "movie", "Second Film", year=2005, collections=["Beta"]),
        Record("third", "movie", "Third Film", year=2010, collections=["Gamma"]),
        Record("overlap", "movie", "Both Films", year=2015, collections=["Alpha", "Beta"]),
    ])
    response = client.get(
        "/api/v1/media",
        params=[("collection", "Alpha"), ("collection", "Beta")],
        headers=admin,
    )
    assert response.status_code == 200
    assert response.json()["total"] == 3
    assert {item["title"] for item in response.json()["items"]} == {
        "First Film", "Second Film", "Both Films"
    }
    response = client.get(
        "/api/v1/media",
        params=[("collection", "Alpha"), ("collection", "Beta"), ("year", "2005")],
        headers=admin,
    )
    assert response.status_code == 200
    assert [item["title"] for item in response.json()["items"]] == ["Second Film"]
    single = client.get(
        "/api/v1/media", params={"collection": "Alpha"}, headers=admin
    )
    assert single.status_code == 200
    assert single.json()["total"] == 2
