"""Offline iPhone TMDB enrichment publication and rate-limit coordination."""

from datetime import UTC, datetime, timedelta
from hashlib import sha256

from home_media_catalog.connectors.base import Record


def _device_item(media_id, **changes):
    return {
        "media_id": media_id,
        "imdb_id": "tt1234567",
        "tmdb_id": "4400",
        "people": [{
            "name": "Actor from iPhone", "category": "cast",
            "tmdb_id": "10", "photos": []
        }],
        "trailers": ["https://www.youtube.com/watch?v=abcdefghijk"],
        "poster": "https://image.tmdb.org/t/p/w500/test.jpg",
        **changes,
    }


def test_device_upload_is_idempotent_and_preserves_rich_metadata(store):
    source = store.save_source({"name": "CLZ", "type": "clz"})["id"]
    store.apply_snapshot(source, [Record(
        "disc", "movie", "Replicated film",
        identifiers={"imdb": "tt1234567"}
    )])
    movie_id = store.media(source_type="clz")["items"][0]["id"]
    result = store.merge_device_tmdb_enrichment([_device_item(movie_id)])
    assert result == {"accepted": [movie_id], "unchanged": [], "rejected": []}
    detail = store.detail(movie_id)
    assert detail["people"][0]["name"] == "Actor from iPhone"
    assert detail["trailers"] == ["https://www.youtube.com/watch?v=abcdefghijk"]
    assert detail["artwork"]["poster"] is not None
    assert detail["enriched_at"] is not None
    assert store.tmdb_enrichment_candidates() == []
    assert store.merge_device_tmdb_enrichment([_device_item(movie_id)])["unchanged"] == [
        movie_id
    ]
    assert any(
        row["media_id"] == movie_id for row in store.tmdb_enrichment_revisions()
    )


def test_device_upload_rejects_mismatched_provider_identity(store):
    source = store.save_source({"name": "Filmotech", "type": "filmotech"})["id"]
    store.apply_snapshot(source, [Record(
        "edition", "movie", "Other film",
        identifiers={"imdb": "tt7654321", "tmdb": "999"}
    )])
    movie_id = store.media(source_type="filmotech")["items"][0]["id"]
    assert store.merge_device_tmdb_enrichment([_device_item(movie_id)])["rejected"] == [
        movie_id
    ]
    assert store.detail(movie_id)["enriched_at"] is None


def test_device_upload_does_not_overwrite_server_cast_or_poster(store):
    source = store.save_source({"name": "CLZ", "type": "clz"})["id"]
    store.apply_snapshot(source, [Record(
        "disc", "movie", "Already enriched",
        identifiers={"imdb": "tt1234567"}
    )])
    movie_id = store.media(source_type="clz")["items"][0]["id"]
    store.save_enrichment(movie_id, {
        "tmdb_id": "4400",
        "people": [{"name": "Server Actor", "category": "cast", "photos": []}],
        "trailers": ["https://www.youtube.com/watch?v=12345678901"],
        "poster": "https://image.tmdb.org/t/p/w500/server.jpg",
    })
    result = store.merge_device_tmdb_enrichment([_device_item(movie_id)])
    assert result["accepted"] == [movie_id]
    detail = store.detail(movie_id)
    assert detail["people"][0]["name"] == "Server Actor"
    assert len(detail["trailers"]) == 2
    assert detail["artwork"]["poster"] is not None


def test_enrichment_upload_api_rejects_non_admin(client, admin):
    source = client.app.state.store.save_source(
        {"name": "CLZ", "type": "clz"}
    )["id"]
    client.app.state.store.apply_snapshot(source, [Record(
        "disc", "movie", "Mobile", identifiers={"imdb": "tt1234567"}
    )])
    movie_id = client.app.state.store.media(source_type="clz")["items"][0]["id"]
    path = "/api/v1/catalogue/enrichment/device"
    request = {"items": [_device_item(movie_id)]}
    assert client.post(path, json=request).status_code == 401
    assert client.post(path, json=request, headers=admin).status_code == 200
    assert client.post(path, json=request, headers=admin).json()["unchanged"] == [
        movie_id
    ]


def test_coordination_only_shares_same_tmdb_key(client, admin):
    path = "/api/v1/catalogue/enrichment/coordination"
    current = client.get(path, headers=admin)
    assert current.status_code == 200
    # This test's default server has no TMDB token configured.
    assert current.json()["key_fingerprint"] is None
    future = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
    response = client.post(path, headers=admin, json={
        "key_fingerprint": sha256(b"another key").hexdigest(),
        "retry_at": future,
    })
    assert response.status_code == 200
    assert response.json()["shared"] is False
    assert client.app.state.store.tmdb_retry_state()["retry_at"] is None
