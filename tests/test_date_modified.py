from datetime import UTC, datetime, timedelta

from home_media_catalog.connectors.base import Record


def seed(store):
    source = store.save_source({"name": "Kodi", "type": "kodi"})
    records = [
        Record(str(i), "movie", name, identifiers={"imdb": f"tt765432{i}"})
        for i, name in enumerate(["Alpha", "Bravo", "Charlie"])
    ]
    store.apply_snapshot(source["id"], records)
    ids = {item["title"]: item["id"] for item in store.media()["items"]}
    return source["id"], records, ids


def test_import_is_not_a_modification(store):
    seed(store)
    assert all(item["date_modified"] is None for item in store.media()["items"])


def test_personal_edit_uses_device_time_and_sorts_first(client, admin):
    store = client.app.state.store
    _, _, ids = seed(store)
    edited = (datetime.now(UTC) - timedelta(hours=2)).replace(microsecond=0)
    response = client.patch(
        f"/api/v1/media/{ids['Alpha']}/personal",
        headers={**admin, "X-Modified-At": edited.isoformat().replace("+00:00", "Z")},
        json={"user_rating": 8},
    )
    assert response.status_code == 200
    page = client.get(
        "/api/v1/media?sort=date_modified&order=desc", headers=admin
    ).json()
    assert page["items"][0]["id"] == ids["Alpha"]
    assert page["items"][0]["date_modified"] == edited.isoformat()


def test_modification_date_never_moves_backwards_or_to_the_future(client, admin):
    store = client.app.state.store
    _, _, ids = seed(store)
    path = f"/api/v1/media/{ids['Bravo']}/personal"
    client.patch(path, headers=admin, json={"watched": True})
    latest = store.detail(ids["Bravo"])["date_modified"]
    older = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    client.patch(path, headers={**admin, "X-Modified-At": older}, json={"watched": False})
    assert store.detail(ids["Bravo"])["date_modified"] == latest
    future = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    client.patch(path, headers={**admin, "X-Modified-At": future}, json={"watched": True})
    assert store.detail(ids["Bravo"])["date_modified"] <= datetime.now(UTC).isoformat()


def test_changed_source_edition_modifies_existing_work_only(store):
    source_id, records, ids = seed(store)
    records[2] = Record("2", "movie", "Charlie", watched=True,
                        identifiers={"imdb": "tt7654322"})
    store.apply_snapshot(source_id, records + [Record("3", "movie", "Delta")])
    rows = {item["title"]: item for item in store.media()["items"]}
    assert rows["Charlie"]["date_modified"] is not None
    assert rows["Delta"]["date_modified"] is None
    assert rows["Alpha"]["date_modified"] is None


def test_rescan_preserves_modification_date(store):
    source_id, records, ids = seed(store)
    store.update_personal(ids["Alpha"], {"user_rating": 7})
    stamp = store.detail(ids["Alpha"])["date_modified"]
    store.apply_snapshot(source_id, records)
    assert store.detail(ids["Alpha"])["date_modified"] == stamp
