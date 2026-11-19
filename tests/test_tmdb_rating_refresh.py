"""TMDB public rating: gap filling, explicit refresh and local-first merge."""

import json
from datetime import UTC, datetime, timedelta

from home_media_catalog.connectors.base import Record
from home_media_catalog.enrichment import tmdb_rating


def _movie(store, rating=None):
    source = store.save_source({"name": "CLZ", "type": "clz"})["id"]
    store.apply_snapshot(source, [Record(
        "disc", "movie", "Rated film", rating=rating,
        identifiers={"imdb": "tt1234567"},
    )])
    return store.media(source_type="clz")["items"][0]["id"]


def _enrichment(**changes):
    return {"tmdb_id": "4400", "people": [], "trailers": [], **changes}


def _device_item(media_id, **changes):
    return {
        "media_id": media_id, "imdb_id": "tt1234567", "tmdb_id": "4400",
        "people": [], "trailers": [], **changes,
    }


def _iso(delta=timedelta()):
    return (datetime.now(UTC) + delta).isoformat(timespec="seconds")


def test_tmdb_vote_average_is_normalized():
    assert tmdb_rating(7.345) == 7.3
    assert tmdb_rating(0) is None
    assert tmdb_rating(11) is None
    assert tmdb_rating(True) is None
    assert tmdb_rating("8") is None


def test_automatic_enrichment_only_fills_a_missing_rating(store):
    rated = _movie(store, rating=6.0)
    store.save_enrichment(rated, _enrichment(tmdb_rating=8.1))
    assert store.detail(rated)["rating"] == 6.0

    store.save_enrichment(rated, _enrichment(tmdb_rating=8.1, tmdb_rating_refreshed_at=_iso()))
    assert store.detail(rated)["rating"] == 8.1


def test_missing_source_rating_uses_tmdb(store):
    movie = _movie(store)
    store.save_enrichment(movie, _enrichment(tmdb_rating=7.4))
    assert store.detail(movie)["rating"] == 7.4


def test_refresh_without_vote_keeps_last_refreshed_rating(store):
    movie = _movie(store, rating=5.0)
    store.save_enrichment(movie, _enrichment(tmdb_rating=7.9, tmdb_rating_refreshed_at=_iso()))
    store.save_enrichment(movie, _enrichment())
    assert store.detail(movie)["rating"] == 7.9


def test_device_refresh_is_merged_when_newer(store):
    movie = _movie(store, rating=5.0)
    store.save_enrichment(movie, _enrichment(
        tmdb_rating=6.5, tmdb_rating_refreshed_at=_iso(-timedelta(days=2))))
    result = store.merge_device_tmdb_enrichment([_device_item(
        movie, tmdb_rating=7.2, tmdb_rating_refreshed_at=_iso(-timedelta(days=1)))])
    assert result["accepted"] == [movie]
    assert store.detail(movie)["rating"] == 7.2

    # An older device refresh must not roll back the newer value.
    result = store.merge_device_tmdb_enrichment([_device_item(
        movie, tmdb_rating=4.0, tmdb_rating_refreshed_at=_iso(-timedelta(days=3)))])
    assert result["unchanged"] == [movie]
    assert store.detail(movie)["rating"] == 7.2


def test_device_gap_fill_without_refresh_keeps_source_rating(store):
    movie = _movie(store, rating=5.0)
    store.merge_device_tmdb_enrichment([_device_item(movie, tmdb_rating=8.0)])
    assert store.detail(movie)["rating"] == 5.0


def test_future_device_timestamp_is_clamped(store):
    movie = _movie(store, rating=5.0)
    store.merge_device_tmdb_enrichment([_device_item(
        movie, tmdb_rating=7.0, tmdb_rating_refreshed_at=_iso(timedelta(days=365)))])
    assert store.detail(movie)["rating"] == 7.0
    with store.connect() as conn:
        payload = json.loads(conn.execute(
            "SELECT payload FROM enrichment WHERE work_id=?", (movie,)
        ).fetchone()["payload"])
    stored = datetime.fromisoformat(payload["tmdb_rating_refreshed_at"])
    assert stored <= datetime.now(UTC)


def test_invalid_device_rating_is_ignored(store):
    movie = _movie(store, rating=5.0)
    store.merge_device_tmdb_enrichment([_device_item(
        movie, tmdb_rating=42, tmdb_rating_refreshed_at=_iso())])
    assert store.detail(movie)["rating"] == 5.0


def test_device_rating_publication_and_change_feed(store):
    movie = _movie(store, rating=5.0)
    assert store.rating_changes()["changes"] == []
    result = store.merge_device_tmdb_ratings([
        {"media_id": movie, "tmdb_rating": 7.6, "tmdb_rating_refreshed_at": _iso()},
        {"media_id": "unknown", "tmdb_rating": 7.0, "tmdb_rating_refreshed_at": _iso()},
        {"media_id": movie, "tmdb_rating": 99, "tmdb_rating_refreshed_at": _iso()},
    ])
    assert result["accepted"] == [movie]
    assert result["rejected"] == ["unknown", movie]
    assert store.detail(movie)["rating"] == 7.6
    feed = store.rating_changes()
    assert feed["changes"] == [{"media_id": movie, "rating": 7.6}]
    assert store.rating_changes(feed["cursor"])["changes"] == []
    # A rating-only row is not a rich enrichment: credits are still fetched.
    assert store.detail(movie)["enriched_at"] is None
    assert movie in store.tmdb_enrichment_candidates()


def test_device_rating_with_conflicting_tmdb_identity_is_rejected(store):
    source = store.save_source({"name": "CLZ", "type": "clz"})["id"]
    store.apply_snapshot(source, [Record(
        "disc", "movie", "Known film", identifiers={"tmdb": "100"})])
    movie = store.media(source_type="clz")["items"][0]["id"]
    result = store.merge_device_tmdb_ratings([{
        "media_id": movie, "tmdb_id": "200", "tmdb_rating": 6.0,
        "tmdb_rating_refreshed_at": _iso()}])
    assert result["rejected"] == [movie]


def test_unchanged_rating_is_not_published_again(store):
    movie = _movie(store)
    assert store.save_tmdb_rating(movie, 6.4) == "accepted"
    assert store.save_tmdb_rating(movie, 6.4) == "unchanged"
    assert len(store.rating_changes()["changes"]) == 1


def _rating_client(tmp_path):
    from fastapi.testclient import TestClient

    from home_media_catalog.app import create_app
    from home_media_catalog.settings import Settings
    from tests.conftest import ADMIN, INGRESS, READ

    return TestClient(create_app(Settings(
        data_dir=tmp_path, api_token=ADMIN, read_token=READ,
        ingress_token=INGRESS, tmdb_api_token="test-tmdb-token")))


def test_server_rating_job_runs_stops_and_resumes(tmp_path, admin):
    import time
    from unittest.mock import patch

    import httpx

    def upstream(request):
        if "/find/" in request.url.path:
            return httpx.Response(200, json={"movie_results": [{"id": 4400}]})
        return httpx.Response(200, json={"id": 4400, "vote_average": 8.3})

    original = httpx.AsyncClient

    def session(**kwargs):
        return original(**kwargs, transport=httpx.MockTransport(upstream))

    with patch("home_media_catalog.enrichment.httpx.AsyncClient", side_effect=session), \
            _rating_client(tmp_path) as client:
        store = client.app.state.store
        movie = _movie(store, rating=5.0)
        path = "/api/v1/catalogue/ratings/refresh"
        assert client.post(path).status_code == 401
        started = client.post(path, headers=admin).json()
        assert started["status"] == "running" and started["total"] == 1
        for _ in range(50):
            status = client.get(path, headers=admin).json()
            if status["status"] == "completed":
                break
            time.sleep(0.1)
        assert status["status"] == "completed"
        assert status["updated"] == 1 and status["remaining"] == 0
        assert store.detail(movie)["rating"] == 8.3
        changes = client.get("/api/v1/catalogue/ratings/changes", headers=admin).json()
        assert changes["changes"][0]["media_id"] == movie

        # Stop keeps the remaining queue; resume continues from it.
        job = store.tmdb_rating_job()
        job.update(status="running", queue=["missing-work"], started_at="x")
        store.save_tmdb_rating_job(job)
        assert client.delete(path, headers=admin).json()["status"] == "stopped"
        assert store.tmdb_rating_job()["queue"] == ["missing-work"]
        assert client.post(path, headers=admin).json()["remaining"] == 1


def test_device_rating_endpoint_and_capability(client, admin):
    store = client.app.state.store
    movie = _movie(store, rating=5.0)
    discovery = client.get("/api/v1/discovery").json()
    assert "tmdb_rating_refresh" in discovery["capabilities"]
    path = "/api/v1/catalogue/ratings/device"
    body = {"items": [{"media_id": movie, "tmdb_rating": 6.9, "tmdb_rating_refreshed_at": _iso()}]}
    assert client.post(path, json=body).status_code == 401
    assert client.post(path, json=body, headers=admin).json()["accepted"] == [movie]
    assert client.post(path, json={"items": []}, headers=admin).status_code == 422
