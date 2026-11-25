"""Only genuine artwork reference changes invalidate previously cached media."""

from home_media_catalog.connectors.base import Record


def test_source_refresh_does_not_invalidate_unchanged_artwork(client, admin):
    response = client.post(
        "/api/v1/sources",
        headers=admin,
        json={
            "name": "Artwork test source",
            "type": "kodi",
            "config": {"host": "nas", "database": "MyVideos131", "username": "reader"},
        },
    )
    assert response.status_code == 201, response.text
    source_id = response.json()["id"]
    store = client.app.state.store

    def snapshot(poster="https://example.org/poster-a.jpg",
                 fanart="https://example.org/fanart-a.jpg", rating=7):
        store.apply_snapshot(source_id, [
            Record("movie:42", "movie", "Example",
                   identifiers={"tmdb": "987654"},
                   poster=[poster], fanart=[fanart], rating=rating)
        ])

    baseline = store.artwork_changes(0)["cursor"]
    snapshot()
    after_initial = store.artwork_changes(baseline)
    assert after_initial["changes"] == []

    snapshot(rating=8)
    after_metadata = store.artwork_changes(baseline)
    assert after_metadata["changes"] == []

    snapshot(poster="https://example.org/poster-b.jpg", rating=8)
    poster_change = store.artwork_changes(baseline)
    assert len(poster_change["changes"]) == 1
    media_id = poster_change["changes"][0]["media_id"]

    snapshot(poster="https://example.org/poster-b.jpg", rating=8)
    assert store.artwork_changes(poster_change["cursor"])["changes"] == []

    snapshot(poster="https://example.org/poster-b.jpg",
             fanart="https://example.org/fanart-b.jpg", rating=8)
    fanart_change = store.artwork_changes(poster_change["cursor"])
    assert len(fanart_change["changes"]) == 1
    assert fanart_change["changes"][0]["media_id"] == media_id
