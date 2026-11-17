"""Built-in internal source: validation, idempotency and safe edition reconciliation."""
import asyncio
from uuid import uuid4

import httpx
import pytest
from conftest import READ

from home_media_catalog import internal_catalog
from home_media_catalog.ai import AIConfiguration, AISettingsInput
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
    second = store.save_internal_media(entry(id=str(uuid4()), format="Blu-ray 3D", barcode=None))
    assert first["id"] == second["id"]
    assert store.media(source_type="internal")["total"] == 1
    assert len(store.detail(first["id"])["editions"]) == 2


def test_manual_editions_without_provider_ids_join_only_matching_title_and_year(store):
    first = store.save_internal_media(entry(id=str(uuid4()), tmdb_id=None, format="DVD"))
    second = store.save_internal_media(entry(id=str(uuid4()), tmdb_id=None,
                                              format="Blu-ray", barcode=None))
    assert first["id"] == second["id"]
    remake = store.save_internal_media(entry(id=str(uuid4()), tmdb_id=None,
                                              format="DVD", year=2025, barcode=None))
    assert remake["id"] != first["id"]
    assert store.media(source_type="internal")["total"] == 2



def test_enrichment_accepts_gtin_alias_but_rejects_different_disc(store):
    """Replay may use a padded EAN after importing the equivalent UPC-A."""
    item = entry(barcode="036000291452")
    created = store.save_internal_media(item)
    result = store.update_internal_edition(
        item["id"], "Blu-ray", "0036000291452", fill_missing_only=True,
        edition_details={"region": "B"},
    )
    assert result["id"] == created["id"]
    detail = store.detail(created["id"])
    edition = next(e for e in detail["editions"] if e["source_type"] == "internal")
    assert edition["metadata"]["barcode"] == "036000291452"
    assert edition["metadata"]["region"] == "B"
    with pytest.raises(ValueError, match="Edition barcode changed"):
        store.update_internal_edition(
            item["id"], "Blu-ray", "4006381333931", fill_missing_only=True,
            edition_details={"region": "A"},
        )
    edition = next(e for e in store.detail(created["id"])["editions"]
                   if e["source_type"] == "internal")
    assert edition["metadata"]["barcode"] == "036000291452"
    assert edition["metadata"]["region"] == "B"

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


def test_internal_create_and_delete_with_ai_indexing_enabled(client, admin, tmp_path, monkeypatch):
    """AI's create_task must run on the ASGI loop, not the sync route worker."""
    AIConfiguration(tmp_path).update(AISettingsInput(api_key="fake-unit-test-key"))

    async def fake_embed(texts, model, key):
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr("home_media_catalog.ai_vectors.embed", fake_embed)
    value = entry()
    created = client.post("/api/v1/internal/items", headers=admin, json=value)
    assert created.status_code == 201, created.text
    removed = client.delete(
        "/api/v1/internal/items/" + value["id"], headers=admin
    )
    assert removed.status_code == 204, removed.text
    assert client.get(
        "/api/v1/media?source_type=internal", headers=admin
    ).json()["total"] == 0


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


def test_barcode_aliases_include_zero_padded_gtin14():
    aliases = internal_catalog.barcode_variants("00036000291452")
    assert "036000291452" in aliases
    assert "0036000291452" in aliases
    assert "00036000291452" in internal_catalog.barcode_variants("036000291452")


def test_parallel_barcode_providers_prefer_disc_and_share_alias_cache(monkeypatch):
    internal_catalog._CACHE.clear()
    calls = []

    async def movie(value, client):
        calls.append(("movie", value))
        return {"found": True, "title": "Movie - Blu-ray", "source": "upcmdb", "items": []}

    async def generic(value, client):
        calls.append(("generic", value))
        return {"found": True, "title": "Generic product", "source": "upcitemdb", "items": []}

    async def bnf(value, client):
        calls.append(("bnf", value))
        return None

    monkeypatch.setattr(internal_catalog, "_lookup_upcmdb", movie)
    monkeypatch.setattr(internal_catalog, "_lookup_upcitemdb", generic)
    monkeypatch.setattr(internal_catalog, "_lookup_bnf", bnf)
    first = asyncio.run(internal_catalog.lookup_disc_barcode("036000291452"))
    second = asyncio.run(internal_catalog.lookup_disc_barcode("0036000291452"))
    assert first["source"] == "upcmdb"
    assert second["barcode"] == "0036000291452"
    assert second["source"] == "upcmdb"
    assert len(calls) == 3
    internal_catalog._CACHE.clear()


def test_barcode_fallback_uses_open_products_facts_after_primary_miss(monkeypatch):
    internal_catalog._CACHE.clear()

    async def miss(value, client):
        return None

    async def products(value, client):
        return {"found": True, "title": "Film DVD", "source": "openproductsfacts", "items": []}

    monkeypatch.setattr(internal_catalog, "_lookup_upcitemdb", miss)
    monkeypatch.setattr(internal_catalog, "_lookup_bnf", miss)
    monkeypatch.setattr(internal_catalog, "_lookup_openproductsfacts", products)
    monkeypatch.setattr(internal_catalog, "_lookup_wikidata", miss)
    monkeypatch.delenv("BARCODELOOKUP_API_KEY", raising=False)
    result = asyncio.run(internal_catalog.lookup_disc_barcode("4006381333931"))
    assert result["source"] == "openproductsfacts"
    assert result["providers_checked"] == ["upcitemdb", "bnf", "openproductsfacts", "wikidata"]
    internal_catalog._CACHE.clear()


def test_upcmdb_parses_title_format_and_imdb_reference():
    def handler(request):
        assert request.url.path == "/api/v1/lookup/883929264483"
        return httpx.Response(200, json={
            "status": "success",
            "data": {"title": "The Matrix", "format": "Blu-ray", "imdbID": "tt0133093"},
        })

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await internal_catalog._lookup_upcmdb("883929264483", client)

    result = asyncio.run(run())
    assert result["title"] == "The Matrix - Blu-ray"
    assert result["items"][0]["imdb_id"] == "tt0133093"


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



def test_legacy_clz_one_season_repaired_without_reimport(store):
    source = store.save_source({"name": "Physical discs", "type": "clz"})["id"]
    store.apply_snapshot(source, [
        Record("clz:hawkeye", "tvshow", "Hawkeye", year=2021,
               identifiers={"imdb": "tt10160804"},
               metadata={"series_title": "Hawkeye",
                         "physical_episode_count": 6}),
    ])
    work_id = store.media(q="Hawkeye")["items"][0]["id"]
    assert store.seasons(work_id) == []
    assert store.has_legacy_clz_single_season(work_id)
    # The caller performs the verified provider lookup first; the store must
    # not guess season 1 without that external confirmation.
    assert store.repair_legacy_clz_single_season(work_id, episode_count=6)
    seasons = store.seasons(work_id)
    assert len(seasons) == 1
    assert seasons[0]["season"] == 1
    assert seasons[0]["owned_physical"] is True
    assert seasons[0]["physical_episode_count"] == 6
    assert not store.has_legacy_clz_single_season(work_id)


def test_single_season_disc_inference_rejects_multi_season_boxset():
    assert not internal_catalog.is_multi_season_boxset("Hawkeye Blu-ray 4K")
    assert internal_catalog.is_multi_season_boxset("Complete Series Blu-ray")
    assert internal_catalog.is_multi_season_boxset("Mandalorian Seasons 1-3")
    assert internal_catalog.is_multi_season_boxset("Intégrale de la série")


def test_tmdb_series_profile_requires_unambiguous_single_regular_season(monkeypatch):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def tmdb(_token):
        async def get(path, **kwargs):
            assert path == "tv/88329"
            assert kwargs == {"language": "fr-FR"}
            return {"number_of_seasons": 1, "seasons": [
                {"season_number": 0, "episode_count": 3},
                {"season_number": 1, "episode_count": 6,
                 "poster_path": "/hawk.jpg"},
            ]}
        yield get

    monkeypatch.setattr(internal_catalog, "tmdb_connection", tmdb)
    result = asyncio.run(
        internal_catalog.tmdb_series_season_profile("88329", "token")
    )
    assert result["single_season"] is True
    assert result["episode_count"] == 6
    assert result["poster_path"] == "/hawk.jpg"


def test_internal_tv_seasons_share_one_work_and_own_distinct_posters(store):
    first_id = None
    for number in (1, 2, 3):
        data = {
            "id": str(uuid4()), "kind": "tvshow", "title": "The Mandalorian",
            "year": 2019, "tmdb_id": "82856",
            "physical_title": f"The Mandalorian - Saison {number}",
            "physical_episode_count": 8,
            "season_poster_path": f"/season{number}.jpg",
            "format": "Blu-ray",
        }
        added = store.save_internal_media(data)
        if first_id is None:
            first_id = added["id"]
        assert added["id"] == first_id
        assert store.season_poster(first_id, number) == [
            f"https://image.tmdb.org/t/p/w342/season{number}.jpg"
        ]

    assert store.media(kind="tvshow")["total"] == 1
    seasons = store.seasons(first_id)
    assert [row["season"] for row in seasons] == [1, 2, 3]
    assert all(row["owned_physical"] for row in seasons)
    assert [row["physical_episode_count"] for row in seasons] == [8, 8, 8]
    assert len(store.detail(first_id)["editions"]) == 3

    # Removing one disc must not remove its siblings or its series.
    editions = store.detail(first_id)["editions"]
    second = next(
        e["external_key"] for e in editions
        if e["metadata"].get("physical_season") == 2
    )
    store.delete_internal_media(second)
    assert [row["season"] for row in store.seasons(first_id)] == [1, 3]


def test_internal_tv_season_manual_edition_title_does_not_create_duplicate(store):
    first = store.save_internal_media({
        "id": str(uuid4()), "kind": "tvshow",
        "title": "A TV Series - Season 1", "year": 2020,
        "format": "DVD",
    })
    second = store.save_internal_media({
        "id": str(uuid4()), "kind": "tvshow",
        "title": "A TV Series: Season 2", "year": 2020,
        "format": "Blu-ray",
    })
    assert first["id"] == second["id"]
    assert store.detail(first["id"])["title"] == "A TV Series"
    assert [row["season"] for row in store.seasons(first["id"])] == [1, 2]


def test_season_parser_rejects_boxsets_and_unrelated_titles():
    parse = internal_catalog.physical_season_in_title
    assert parse("The Mandalorian - Saison 03 Blu-ray")[0] == 3
    assert parse("The Mandalorian: Season 2")[0] == 2
    assert parse("The Mandalorian - S01")[0] == 1
    assert parse("The Mandalorian: Seasons 1-3")[0] is None
    assert parse("The Mandalorian (complete series)")[0] is None


def test_internal_tv_season_rest_infers_season_from_disc_label(client, admin):
    data = {
        "id": str(uuid4()), "kind": "tvshow", "title": "The Mandalorian",
        "year": 2019, "tmdb_id": "82856", "format": "Blu-ray",
        "physical_title": "The Mandalorian Season 3",
        "season_poster_path": "/s3.jpg", "physical_episode_count": 8,
    }
    response = client.post("/api/v1/internal/items", json=data, headers=admin)
    assert response.status_code == 201, response.text
    work_id = response.json()["id"]
    detail = client.get("/api/v1/media/" + work_id, headers=admin).json()
    edition = next(e for e in detail["editions"] if e["source_type"] == "internal")
    assert edition["metadata"]["physical_season"] == 3
    assert edition["metadata"]["season_poster_path"] == "/s3.jpg"
    assert client.get(f"/api/v1/media/{work_id}/seasons", headers=admin).json()[0][
        "physical_episode_count"
    ] == 8


def test_tmdb_season_artwork_lookup_uses_season_not_show(monkeypatch):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def connection(_token):
        async def get(path, **kwargs):
            assert path == "tv/82856/season/3"
            assert kwargs == {"language": "fr-FR"}
            return {"poster_path": "/s3.jpg", "episodes": [{"episode_number": i} for i in range(8)]}
        yield get

    monkeypatch.setattr(internal_catalog, "tmdb_connection", connection)
    metadata = asyncio.run(internal_catalog.tmdb_physical_season("82856", 3, "token"))
    assert metadata == {"season": 3, "poster_path": "/s3.jpg", "episode_count": 8}


def test_edit_internal_edition_keeps_other_sources_and_identity(client, admin):
    store = client.app.state.store
    external = store.save_source({"name": "CLZ", "type": "clz"})["id"]
    store.apply_snapshot(external, [Record(
        "disc:edit", "movie", "Internal Example", year=2024,
        identifiers={"tmdb": "554433"}, formats=["DVD"]
    )])
    item = entry()
    created = client.post("/api/v1/internal/items", headers=admin, json=item)
    assert created.status_code == 201
    work_id = created.json()["id"]
    endpoint = "/api/v1/internal/items/" + item["id"]
    payload = {"format": "4K Ultra HD Blu-ray", "barcode": "036000291452"}
    assert client.patch(endpoint, headers={"Authorization": f"Bearer {READ}"},
                        json=payload).status_code == 403
    assert client.patch(endpoint, headers=admin, json={**payload,
                        "barcode": "4006381333932"}).status_code == 422
    updated = client.patch(endpoint, headers=admin, json=payload)
    assert updated.status_code == 200, updated.text
    assert updated.json() == created.json()
    detail = store.detail(work_id)
    assert len(detail["editions"]) == 2
    internal = next(e for e in detail["editions"] if e["source_type"] == "internal")
    assert internal["external_key"] == item["id"]
    assert internal["metadata"]["barcode"] == payload["barcode"]
    assert "DVD" in detail["formats"]
    assert "Blu-ray" not in detail["formats"]
    assert "4K Ultra HD Blu-ray" in detail["formats"]


def test_manual_internal_work_metadata_is_editable_without_tmdb(client, admin):
    import base64
    from io import BytesIO

    from PIL import Image

    image = BytesIO()
    Image.new("RGB", (20, 30), "red").save(image, format="JPEG")
    encoded = base64.b64encode(image.getvalue()).decode()
    item = entry(tmdb_id=None, title="Unlisted work", poster_jpeg=encoded)
    created = client.post("/api/v1/internal/items", headers=admin, json=item)
    assert created.status_code == 201, created.text
    work_id = created.json()["id"]
    response = client.patch(
        "/api/v1/internal/items/" + item["id"], headers=admin,
        json={"format": "DVD", "barcode": None, "manual_metadata": {
            "title": "Corrected title", "year": 1999, "original_title": "Original",
            "plot": "Manual synopsis", "poster_jpeg": encoded,
        }},
    )
    assert response.status_code == 200, response.text
    assert response.json()["id"] == work_id
    detail = client.get("/api/v1/media/" + work_id, headers=admin).json()
    assert detail["title"] == "Corrected title"
    assert detail["year"] == 1999
    assert detail["plot"] == "Manual synopsis"
    assert client.get("/api/v1/media/" + work_id + "/artwork/poster",
                      headers=admin).content == image.getvalue()


def test_custom_title_preserves_provider_identity_and_search(client, admin):
    store = client.app.state.store
    value = entry(title="Quebec title")
    created = client.post("/api/v1/internal/items", json=value, headers=admin)
    work_id = created.json()["id"]
    endpoint = "/api/v1/media/" + work_id + "/custom-title"
    response = client.patch(endpoint, headers=admin, json={"custom_title": "French title"})
    assert response.status_code == 200, response.text
    assert response.json()["title"] == "French title"
    assert response.json()["provider_title"] == "Quebec title"
    assert response.json()["identifiers"]["tmdb"] == [value["tmdb_id"]]
    assert store.media(q="French title")["total"] == 1
    assert store.media(q="Quebec title")["total"] == 1
    store.save_enrichment(work_id, {"people": [], "trailers": []})
    assert store.detail(work_id)["title"] == "French title"
    reset = client.patch(endpoint, headers=admin, json={"custom_title": None})
    assert reset.json()["title"] == "Quebec title"


def test_advanced_metadata_hdr_and_source_conversion(store):
    source = store.save_source({"name": "Discs", "type": "clz", "config": {}})
    store.apply_snapshot(source["id"], [
        Record("disc-1", "movie", "Source title", year=2009, formats=["Blu-ray", "HDR10"],
               identifiers={"tmdb": "12345"}, metadata={"country": "Japan", "studios": ["Old studio"]})
    ])
    work_id = store.media()["items"][0]["id"]
    edition_id = store.detail(work_id)["editions"][0]["id"]
    store.update_media_metadata(work_id, {"custom_title": "New title", "year": 2010,
        "genres": ["Animation"], "country": "France", "studios": ["New studio"]})
    store.set_edition_hdr(work_id, edition_id, ["Dolby Vision"])
    assert "HDR10" not in store.detail(work_id)["formats"]
    assert store.convert_source_to_internal(source["id"])["copied"] == 1
    assert store.convert_source_to_internal(source["id"], True)["copied"] == 0
    detail = store.detail(work_id)
    assert detail["title"] == "New title"
    assert detail["year"] == 2010
    assert detail["genres"] == ["Animation"]
    assert detail["country"] == "France"
    assert detail["studios"] == ["New studio"]
    assert detail["identifiers"]["tmdb"] == ["12345"]
    assert len(detail["editions"]) == 1
    assert detail["editions"][0]["source_type"] == "internal"
    assert "Dolby Vision" in detail["formats"]


def test_photographed_cover_overrides_tmdb_and_survives_enrichment(store):
    first = store.save_internal_media(entry(poster_path="/provider.jpg"))
    work_id = first["id"]
    original = "https://image.tmdb.org/t/p/w500/provider.jpg"
    store.save_enrichment(work_id, {"people": [], "trailers": [], "poster": original})
    photo = "hmc-poster://" + "a" * 64
    second = store.save_internal_media(entry(_poster_reference=photo, format="DVD", barcode=None))
    assert second["id"] == work_id
    assert store.detail(work_id)["identifiers"]["tmdb"] == ["554433"]
    assert store.artwork(work_id, "poster")[0] == photo
    store.save_enrichment(work_id, {"people": [], "trailers": [], "poster": original})
    assert store.artwork(work_id, "poster")[0] == photo


def test_remove_custom_poster_restores_provider_or_leaves_no_poster(store):
    photo = "hmc-poster://" + "b" * 64
    provider = "https://image.tmdb.org/t/p/w500/original.jpg"
    first = store.save_internal_media(entry(_poster_reference=photo))
    work_id = first["id"]
    assert store.detail(work_id)["has_custom_poster"]
    store.save_enrichment(work_id, {"people": [], "trailers": [], "poster": provider})
    store.update_media_metadata(work_id, {"remove_poster": True})
    assert store.artwork(work_id, "poster") == [provider]
    assert not store.detail(work_id)["has_custom_poster"]
    assert store.detail(work_id)["identifiers"]["tmdb"] == ["554433"]
    other = store.save_internal_media(entry(title="No match", tmdb_id=None, _poster_reference=photo, barcode=None))
    store.update_media_metadata(other["id"], {"remove_poster": True})
    assert store.artwork(other["id"], "poster") == []
    assert not store.detail(other["id"])["has_custom_poster"]


@pytest.mark.parametrize("operation", ["convert", "convert_and_remove", "delete"])
def test_source_mutations_rollback_on_rebuild_failure(store, monkeypatch, operation):
    source_id = store.save_source({"name": "Discs", "type": "clz", "config": {}})["id"]
    store.apply_snapshot(source_id, [
        Record("disc-1", "movie", "Example disc", identifiers={"tmdb": "12345"})
    ])
    work_id = store.media()["items"][0]["id"]
    original = store.detail(work_id)["editions"]
    with monkeypatch.context() as patch:
        def fail_rebuild(*args, **kwargs):
            raise RuntimeError("simulated interrupted transaction")
        patch.setattr(store, "_rebuild", fail_rebuild)
        with pytest.raises(RuntimeError, match="simulated interrupted transaction"):
            if operation == "delete":
                store.delete_source(source_id)
            else:
                store.convert_source_to_internal(source_id, operation == "convert_and_remove")

    assert store.source(source_id) is not None
    assert store.detail(work_id)["editions"] == original
    if operation != "delete":
        assert store.convert_source_to_internal(source_id)["copied"] == 1
        assert store.convert_source_to_internal(source_id)["copied"] == 0
        store.convert_source_to_internal(source_id, True)
        assert store.source(source_id) is None
        assert [e["source_type"] for e in store.detail(work_id)["editions"]] == ["internal"]
    else:
        store.delete_source(source_id)
        assert store.source(source_id) is None


def test_ultra_hd_disc_filter_matches_internal_and_legacy_labels(store):
    disc = store.save_internal_media(entry(
        format="4K Ultra HD Blu-ray", tmdb_id="900001", title="UHD disc"
    ))
    legacy = store.save_source({"name": "Legacy CLZ", "type": "clz"})["id"]
    store.apply_snapshot(legacy, [
        Record("disc", "movie", "Legacy UHD", identifiers={"tmdb": "900002"},
               formats=["Blu-ray 4K"])
    ])
    kodi = store.save_source({"name": "Kodi", "type": "kodi"})["id"]
    store.apply_snapshot(kodi, [
        Record("movie:1", "movie", "Digital 4K", identifiers={"tmdb": "900003"},
               formats=["4K"])
    ])
    for label in ("Blu-ray 4K", "4K Ultra HD Blu-ray", "Ultra HD Blu-ray"):
        rows = store.media(format=label)["items"]
        assert {row["title"] for row in rows} == {"UHD disc", "Legacy UHD"}
        assert any(row["id"] == disc["id"] for row in rows)
    assert {row["title"] for row in store.media(format="4K")["items"]} == {"Digital 4K"}
    assert store.media(format=["Blu-ray 4K", "4K"])["total"] == 0


def test_disc_technical_metadata_local_first_replay(client, admin):
    endpoint = "/api/v1/internal/items"
    item = entry(format="4K Ultra HD Blu-ray", technical_formats=["4K", "Dolby Vision"])
    created = client.post(endpoint, headers=admin, json=item)
    assert created.status_code == 201, created.text
    detail_path = "/api/v1/media/" + created.json()["id"]
    patch = {"format": "DVD", "barcode": item["barcode"], "fill_missing_only": True,
             "technical_formats": ["1080p", "HDR10", "DTS-HD MA", "Dolby Atmos"]}
    for _ in range(2):  # replay must be idempotent
        response = client.patch(endpoint + "/" + item["id"], headers=admin, json=patch)
        assert response.status_code == 200, response.text
    detail = client.get(detail_path, headers=admin).json()
    formats = set(detail["editions"][0]["metadata"]["formats"])
    assert {"4K", "Dolby Vision", "DTS-HD MA", "Dolby Atmos"} <= formats
    assert not formats.intersection({"DVD", "1080p", "HDR10"})
    assert {"4K", "Dolby Vision", "DTS-HD MA", "Dolby Atmos"} <= set(detail["formats"])
    bad = client.patch(endpoint + "/" + item["id"], headers=admin,
                       json={**patch, "barcode": "036000291452"})
    assert bad.status_code == 422
    assert client.get(detail_path, headers=admin).json()["editions"][0]["metadata"]["barcode"] == item["barcode"]


def test_disc_refresh_does_not_override_manual_hdr(store):
    item = entry(format="4K Ultra HD Blu-ray")
    work = store.save_internal_media(item)["id"]
    edition = store.detail(work)["editions"][0]["id"]
    store.set_edition_hdr(work, edition, [])
    store.update_internal_edition(item["id"], item["format"], item["barcode"],
                                  technical=["HDR10", "Dolby Atmos"], fill_missing_only=True)
    metadata = store.detail(work)["editions"][0]["metadata"]
    assert metadata["hdr_override"] == []
    assert "HDR10" not in metadata["formats"]
    assert "Dolby Atmos" in metadata["formats"]
    store.set_edition_hdr(work, edition, None)
    assert "HDR10" not in store.detail(work)["editions"][0]["metadata"]["formats"]

def test_internal_gtin_aliases_are_idempotent_with_distinct_client_ids(store):
    first = store.save_internal_media(entry(barcode="036000291452"))
    for alias in ("0036000291452", "00036000291452"):
        assert store.save_internal_media(entry(barcode=alias)) == first
    assert len(store.detail(first["id"])["editions"]) == 1
    copy = store.save_internal_media(entry(barcode="0036000291452", allow_duplicate=True))
    assert copy["item_id"] != first["item_id"]
    assert len(store.detail(first["id"])["editions"]) == 2
    assert internal_catalog.canonical_gtin("036000291452") == internal_catalog.canonical_gtin("0036000291452")

def test_convert_source_reuses_equivalent_existing_internal_gtin(store):
    existing = store.save_internal_media(entry(barcode="036000291452", format=""))
    source = store.save_source({"name": "Collection", "type": "clz"})["id"]
    store.apply_snapshot(source, [
        Record("disc", "movie", "Internal Example", year=2024,
               identifiers={"tmdb": "554433"}, formats=["Blu-ray"],
               metadata={"barcode": "0036000291452"})
    ])
    store.convert_source_to_internal(source, remove_source=True)
    editions = store.detail(existing["id"])["editions"]
    assert len(editions) == 1
    assert editions[0]["external_key"] == existing["item_id"]
    assert "Blu-ray" in editions[0]["formats"]


def test_rich_disc_details_are_saved_and_only_missing_fields_enriched(store):
    item = entry(edition_details={"audio_streams": [{"language": "French", "codec": "DTS", "channels": "5.1"}]})
    created = store.save_internal_media(item)
    store.update_internal_edition(item["id"], "Blu-ray", item["barcode"], fill_missing_only=True,
                                  edition_details={"audio_streams": [{"language": "English"}],
                                                   "subtitles": ["French", "English"], "region": "B"})
    metadata = store.detail(created["id"])["editions"][0]["metadata"]
    assert metadata["audio_streams"] == [{"language": "French", "codec": "DTS", "channels": "5.1"}]
    assert metadata["subtitles"] == ["French", "English"]
    assert metadata["region"] == "B"
