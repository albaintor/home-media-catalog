"""Manual collections override imported memberships and remain stable after synchronization."""

from home_media_catalog.backup import export_backup, import_backup
from home_media_catalog.connectors.base import Record


def _seed(store):
    source = store.save_source({"name": "Physical library", "type": "clz"})["id"]
    records = [
        Record("film-a", "movie", "Film A", year=2017, collections=["Kodi Saga"]),
        Record("series-b", "tvshow", "Series B", year=2021, collections=["Kodi Saga"]),
        Record("film-c", "movie", "Film C", year=2023, collections=["Other Saga"]),
    ]
    store.apply_snapshot(source, records)
    ids = {item["title"]: item["id"] for item in store.media(limit=50)["items"]}
    return source, records, ids


def test_manual_collection_is_prioritized_and_survives_imports(store):
    source, records, ids = _seed(store)
    manual = store.save_manual_collection("Mixed favourites")
    assert manual["is_manual"] is True
    assert manual["item_count"] == 0

    added = store.add_manual_collection_items(
        manual["id"], [ids["Film A"], ids["Series B"], ids["Film A"]]
    )
    assert added == {"added": 2, "already_present": 0, "total_requested": 2}
    duplicate = store.add_manual_collection_items(manual["id"], [ids["Film A"]])
    assert duplicate["added"] == 0
    assert duplicate["already_present"] == 1

    manual_page = store.media(collection_id=manual["id"], limit=20)
    assert manual_page["total"] == 2
    assert {item["kind"] for item in manual_page["items"]} == {"movie", "tvshow"}
    assert store.media(collection_id=manual["id"], kind="movie")["total"] == 1
    assert store.media(collection_id=manual["id"], kind="tvshow")["total"] == 1
    for item in manual_page["items"]:
        assert item["collections"] == ["Mixed favourites"]
        assert item["manual_collection_ids"] == [manual["id"]]
    assert store.media(collection="Kodi Saga")["total"] == 0

    # A later provider rescan may change its own labels, not manually chosen membership.
    store.apply_snapshot(source, records)
    assert store.media(collection_id=manual["id"])["total"] == 2
    assert store.collection_index()[0]["id"] == manual["id"]

    assert store.remove_manual_collection_item(manual["id"], ids["Film A"])
    assert not store.remove_manual_collection_item(manual["id"], ids["Film A"])
    assert store.media(collection_id=manual["id"])["total"] == 1
    assert store.media(collection="Kodi Saga")["total"] == 1
    assert store.remove_manual_collection_item(manual["id"], ids["Series B"])
    assert next(row for row in store.collection_index() if row["id"] == manual["id"])["item_count"] == 0
    store.delete_manual_collection(manual["id"])
    assert store.media()["total"] == 3


def test_manual_collection_endpoints_and_permissions(client, admin):
    store = client.app.state.store
    _, _, ids = _seed(store)
    assert client.post("/api/v1/collections", json={"name": "Personal"}).status_code == 401

    created = client.post("/api/v1/collections", json={"name": "Personal"}, headers=admin)
    assert created.status_code == 201, created.text
    collection = created.json()
    collection_id = collection["id"]
    assert collection["is_manual"] is True
    assert client.post("/api/v1/collections", json={"name": "Personal"}, headers=admin).status_code == 409

    add = client.post(
        f"/api/v1/collections/{collection_id}/items",
        json={"media_ids": [ids["Film A"], ids["Series B"], ids["Film A"]]},
        headers=admin,
    )
    assert add.status_code == 200, add.text
    assert add.json()["added"] == 2
    summary = client.get("/api/v1/collections", headers=admin).json()
    assert next(entry for entry in summary if entry["id"] == collection_id)["item_count"] == 2
    assert client.get(
        "/api/v1/media", params={"collection_id": collection_id, "kind": "tvshow"},
        headers=admin,
    ).json()["total"] == 1

    renamed = client.patch(
        f"/api/v1/collections/{collection_id}",
        json={"name": "Personal picks"}, headers=admin,
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Personal picks"
    assert client.delete(
        f"/api/v1/collections/{collection_id}/items/{ids['Film A']}",
        headers=admin,
    ).status_code == 204
    assert client.delete(
        f"/api/v1/collections/{collection_id}", headers=admin,
    ).status_code == 204
    assert client.get("/api/v1/media", headers=admin).json()["total"] == 3


def test_manual_collections_are_in_portable_backups(store):
    _, _, ids = _seed(store)
    collection = store.save_manual_collection("My Mixed Collection")
    store.add_manual_collection_items(
        collection["id"], [ids["Film A"], ids["Series B"]]
    )
    backup = export_backup(store)
    assert "collections" in backup["tables"]
    assert "collection_items" in backup["tables"]
    store.delete_manual_collection(collection["id"])
    import_backup(store, backup)
    restored = next(
        row for row in store.collection_index() if row["id"] == collection["id"]
    )
    assert restored["item_count"] == 2
    assert restored["is_manual"] is True
