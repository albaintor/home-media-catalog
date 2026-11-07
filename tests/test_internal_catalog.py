"""Built-in internal source: validation, idempotency and safe edition reconciliation."""
import asyncio
from uuid import uuid4

import httpx
import pytest
from conftest import READ

from home_media_catalog import internal_catalog
from home_media_catalog.connectors.base import Record
from home_media_catalog.internal_catalog import valid_barcode


def entry(**fields):
    return {"id": str(uuid4()), "title": "Internal Example", "kind": "movie",
            "year": 2024, "tmdb_id": "554433", "format": "Blu-ray",
            "barcode": "4006381333931", **fields}


def test_gtin_checksum():
    assert valid_barcode("4006381333931")
    assert valid_barcode("036000291452")
    assert valid_barcode("01234558")  # UPC-E equivalent of GTIN-12 012345000058
    assert internal_catalog.expand_upce("01234558") == "012345000058"
    assert "012345000058" in internal_catalog.barcode_variants("01234558")
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


def test_existing_clz_barcode_is_used_before_external_provider(client, admin):
    store = client.app.state.store
    sid = store.save_source({"name": "My movie collection", "type": "clz"})["id"]
    store.apply_snapshot(sid, [Record(
        "disc:99", "movie", "A known physical edition",
        metadata={"barcode": "036000291452"}, formats=["DVD"],
    )])
    response = client.get("/api/v1/internal/barcode/0036000291452", headers=admin)
    assert response.status_code == 200
    assert response.json()["title"] == "A known physical edition"
    assert response.json()["source"] == "catalog"


def test_public_barcode_provider_fallback_does_not_depend_on_region(monkeypatch):
    internal_catalog._CACHE.clear()

    async def unavailable_upc(value, client):
        raise httpx.ReadTimeout("rate limited")

    async def bnf_fallback(value, client):
        assert value == "4006381333931"
        return {
            "barcode": value, "found": True,
            "title": "Un film - Blu-ray", "source": "bnf",
            "items": [{"title": "Un film - Blu-ray"}],
        }

    monkeypatch.setattr(internal_catalog, "_lookup_upcitemdb", unavailable_upc)
    monkeypatch.setattr(internal_catalog, "_lookup_bnf", bnf_fallback)
    monkeypatch.delenv("BARCODELOOKUP_API_KEY", raising=False)
    result = asyncio.run(internal_catalog.lookup_disc_barcode("4006381333931"))
    assert result["status"] == "found"
    assert result["source"] == "bnf"
    assert result["providers_unavailable"] == ["upcitemdb"]


def test_bnf_sru_response_extracts_physical_title_without_scraping():
    xml = b'''<srw:searchRetrieveResponse xmlns:srw="http://www.loc.gov/zing/srw/"
        xmlns:dc="http://purl.org/dc/elements/1.1/">
        <srw:records><srw:record><srw:recordData>
        <dc:title>International film - DVD</dc:title>
        <dc:description>Code a barres commercial : EAN 4006381333931</dc:description>
        </srw:recordData></srw:record></srw:records>
        </srw:searchRetrieveResponse>'''

    def handler(request):
        assert request.url.params["recordSchema"] == "dublincore"
        assert '4006381333931' in request.url.params["query"]
        return httpx.Response(200, content=xml)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await internal_catalog._lookup_bnf("4006381333931", client)

    result = asyncio.run(run())
    assert result["title"] == "International film - DVD"
    assert result["source"] == "bnf"


@pytest.mark.parametrize("source_type", ["kodi", "clz", "mymovies", "filmotech"])
def test_internal_edition_attaches_to_every_import_source_type(store, source_type):
    source_id = store.save_source({"name": source_type, "type": source_type})["id"]
    store.apply_snapshot(source_id, [
        Record("edition-a", "movie", "Cross-source title", year=2024,
               identifiers={"imdb": "tt9837659"})
    ])
    initial = store.media(q="Cross-source title")
    assert initial["total"] == 1
    original_id = initial["items"][0]["id"]
    saved = store.save_internal_media(entry(
        title="Cross-source title", year=2024, tmdb_id=None, imdb_id=None,
        barcode=None, id=str(uuid4())
    ))
    assert saved["id"] == original_id
    assert store.media(q="Cross-source title")["total"] == 1
    sources = {e["source_type"] for e in store.detail(original_id)["editions"]}
    assert sources == {source_type, "internal"}
    store.delete_internal_media(saved["item_id"])
    assert store.media(q="Cross-source title")["total"] == 1
    assert [e["source_type"] for e in store.detail(original_id)["editions"]] == [source_type]


def test_internal_edition_attaches_to_existing_tv_series_without_duplicate(store):
    source_id = store.save_source({"name": "Kodi", "type": "kodi"})["id"]
    store.apply_snapshot(source_id, [
        Record("series-42", "tvshow", "Same series", year=2021)
    ])
    previous = store.media(q="Same series")["items"][0]["id"]
    added = store.save_internal_media(entry(
        kind="tvshow", title="Same series", year=2021, tmdb_id=None,
        barcode=None, id=str(uuid4())
    ))
    assert added["id"] == previous
    assert len(store.detail(previous)["editions"]) == 2


def test_manual_edition_does_not_merge_distinct_remake_years(store):
    source_id = store.save_source({"name": "CLZ", "type": "clz"})["id"]
    store.apply_snapshot(source_id, [
        Record("old-edition", "movie", "A Remake", year=1984)
    ])
    added = store.save_internal_media(entry(
        title="A Remake", year=2024, tmdb_id=None, barcode=None,
        id=str(uuid4())
    ))
    assert store.media(q="A Remake")["total"] == 2
    assert len(store.detail(added["id"])["editions"]) == 1
