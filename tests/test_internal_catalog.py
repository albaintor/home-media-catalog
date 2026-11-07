"""Built-in internal source: validation, idempotency and safe edition reconciliation."""
from uuid import uuid4

import pytest
from conftest import READ

from home_media_catalog.connectors.base import Record
from home_media_catalog.internal_catalog import valid_barcode


def entry(**fields):
    return {"id": str(uuid4()), "title": "Internal Example", "kind": "movie",
            "year": 2024, "tmdb_id": "554433", "format": "Blu-ray",
            "barcode": "4006381333931", **fields}


def test_gtin_checksum():
    assert valid_barcode("4006381333931")
    assert valid_barcode("036000291452")
    assert not valid_barcode("4006381333932")
    assert not valid_barcode("123456789")
    assert not valid_barcode("400638133393x")


def test_internal_edition_merges_with_external_without_mutating_it(store):
    external = store.save_source({"name": "Kodi", "type": "kodi"})["id"]
    store.apply_snapshot(external, [
        Record("movie:9", "movie", "Internal Example", year=2024,
               identifiers={"tmdb": "554433"})
    ])
    item = entry()
    created = store.save_internal_media(item)
    assert any(s["id"] == "internal" and s["type"] == "internal"
               for s in store.sources())
    again = store.save_internal_media(item)
    assert created == again
    detail = store.detail(created["id"])
    assert {e["source_type"] for e in detail["editions"]} == {"kodi", "internal"}
    assert len(detail["editions"]) == 2
    internal = next(e for e in detail["editions"] if e["source_type"] == "internal")
    assert internal["metadata"]["barcode"] == item["barcode"]
    assert store.media(source_type="internal")["total"] == 1
    store.delete_internal_media(item["id"])
    remaining = store.detail(created["id"])
    assert remaining is not None
    assert len(remaining["editions"]) == 1
    assert remaining["editions"][0]["source_type"] == "kodi"
    assert store.media(source_type="internal")["total"] == 0


def test_internal_multiple_physical_editions_share_one_work(store):
    first = store.save_internal_media(entry(id=str(uuid4()), format="DVD"))
    second = store.save_internal_media(entry(id=str(uuid4()), format="Blu-ray 3D"))
    assert first["id"] == second["id"]
    assert store.media(source_type="internal")["total"] == 1
    assert len(store.detail(first["id"])["editions"]) == 2


def test_manual_editions_without_provider_ids_join_only_matching_title_and_year(store):
    first = store.save_internal_media(entry(id=str(uuid4()), tmdb_id=None, format="DVD"))
    second = store.save_internal_media(entry(id=str(uuid4()), tmdb_id=None,
                                              format="Blu-ray", barcode=None))
    assert first["id"] == second["id"]
    remake = store.save_internal_media(entry(id=str(uuid4()), tmdb_id=None,
                                              format="DVD", year=2025))
    assert remake["id"] != first["id"]
    assert store.media(source_type="internal")["total"] == 2


def test_internal_invalid_barcode_does_not_persist(store):
    with pytest.raises(ValueError, match="barcode"):
        store.save_internal_media(entry(barcode="4006381333932"))
    assert store.media(source_type="internal")["total"] == 0


def test_internal_rest_api_authorization_and_idempotency(client, admin):
    payload = entry()
    endpoint = "/api/v1/internal/items"
    assert client.post(endpoint, json=payload).status_code == 401
    assert client.post(endpoint, headers={"Authorization": f"Bearer {READ}"},
                       json=payload).status_code == 403
    created = client.post(endpoint, headers=admin, json=payload)
    assert created.status_code == 201, created.text
    repeated = client.post(endpoint, headers=admin, json=payload)
    assert repeated.status_code == 201
    assert repeated.json() == created.json()
    item = client.get("/api/v1/media?source_type=internal", headers=admin)
    assert item.status_code == 200
    assert item.json()["total"] == 1
    detail = client.get("/api/v1/media/" + created.json()["id"], headers=admin)
    assert detail.status_code == 200
    assert detail.json()["editions"][0]["metadata"]["barcode"] == payload["barcode"]
    assert client.post(endpoint, headers=admin, json={**payload, "id": str(uuid4()),
                        "barcode": "4006381333932"}).status_code == 422
    assert client.delete("/api/v1/sources/internal", headers=admin).status_code == 409
    removed = client.delete(endpoint + "/" + payload["id"], headers=admin)
    assert removed.status_code == 204
    assert client.get("/api/v1/media?source_type=internal", headers=admin).json()["total"] == 0
