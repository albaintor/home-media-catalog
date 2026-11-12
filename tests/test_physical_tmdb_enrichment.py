"""Physical edition TMDB enrichment is keyed to the work and survives source refresh."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from home_media_catalog.connectors.base import Record
from home_media_catalog.store import Store


def _payload():
    return {
        "people": [{"name": "Example Actor", "category": "cast", "tmdb_id": "42",
                    "role": "Lead", "photos": ["https://image.tmdb.org/t/p/w185/test.jpg"]}],
        "trailers": ["https://www.youtube.com/watch?v=abcdefghijk"],
        "tmdb_id": "4400",
    }


def test_scanned_physical_editions_share_enrichment_cache(store):
    base = {"kind": "movie", "title": "Physical Example", "tmdb_id": "4400",
            "year": 2024, "barcode": None}
    first = store.save_internal_media({**base, "id": str(uuid4()), "format": "DVD"})
    work_id = first["id"]
    assert store.tmdb_enrichment_candidates() == [work_id]
    assert store.save_enrichment(work_id, _payload())
    assert store.tmdb_enrichment_candidates() == []
    second = store.save_internal_media(
        {**base, "id": str(uuid4()), "format": "Blu-ray"}
    )
    assert second["id"] == work_id
    assert len(store.detail(work_id)["editions"]) == 2
    assert store.detail(work_id)["trailers"] == _payload()["trailers"]
    assert store.detail(work_id)["people"][0]["name"] == "Example Actor"
    assert store.tmdb_enrichment_candidates() == []


def test_reimport_reuses_enrichment_even_if_source_metadata_changes(store):
    source = store.save_source({"name": "CLZ discs", "type": "clz"})["id"]
    film = Record(external_key="physical-edition-1", kind="movie",
                  title="The Imported Film", year=2023,
                  identifiers={"imdb": "tt1234567"}, formats=["DVD"])
    store.apply_snapshot(source, [film])
    work_id = store.media(source_type="clz")["items"][0]["id"]
    store.save_enrichment(work_id, _payload())
    # New XML snapshot changes the physical format; TMDB credits are
    # independent from the source-owned physical-edition metadata.
    film.formats = ["Blu-ray"]
    store.apply_snapshot(source, [film])
    detail = store.detail(work_id)
    assert detail["trailers"] == _payload()["trailers"]
    assert detail["people"][0]["name"] == "Example Actor"
    assert "Blu-ray" in detail["formats"]
    assert store.tmdb_enrichment_candidates() == []


def test_poster_only_enrichment_remains_eligible_for_credits_and_trailers(store):
    source = store.save_source({"name": "My Movies", "type": "mymovies"})["id"]
    store.apply_snapshot(source, [
        Record(external_key="disc", kind="movie", title="Needs details",
               identifiers={"imdb": "tt1234567"})
    ])
    work_id = store.media(source_type="mymovies")["items"][0]["id"]
    store.save_enrichment(work_id, {
        "poster": "https://image.tmdb.org/t/p/w500/known.jpg"
    })
    assert store.detail(work_id)["enriched_at"] is None
    assert store.tmdb_enrichment_candidates() == [work_id]


def test_persisted_rate_limit_checkpoints_survive_reinitialization(store):
    until = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
    state = {"retry_at": until, "last_error": "HTTP 429", "skipped": {}}
    store.save_tmdb_retry_state(state)
    recovered = Store(store.path)
    assert recovered.tmdb_retry_state()["retry_at"] == until
    assert recovered.tmdb_retry_state()["last_error"] == "HTTP 429"
