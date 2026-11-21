"""Strict 0-10 score thresholds and read-only Streaming catalogue metrics."""
from home_media_catalog.models import InternalMediaInput
from home_media_catalog.store import Store


def test_movie_rating_thresholds_filter_public_and_personal_independently(tmp_path):
    store = Store(tmp_path / "scores.sqlite")
    store.initialize()
    a = store.save_internal_media(InternalMediaInput(
        id="dbba2222-1234-4234-9234-220022002201",
        kind="movie", title="Above",
        tmdb_id="17771", rating=8.4, format="",
    ).model_dump())["id"]
    b = store.save_internal_media(InternalMediaInput(
        id="dbba2222-1234-4234-9234-220022002202",
        kind="movie", title="Below",
        tmdb_id="17772", rating=6.1, format="",
    ).model_dump())["id"]
    store.update_personal(a, {"user_rating": 4.5})
    store.update_personal(b, {"user_rating": 9.0})
    assert {row["id"] for row in store.media(rating_gt=7)["items"]} == {a}
    assert {row["id"] for row in store.media(rating_lt=7)["items"]} == {b}
    assert {row["id"] for row in store.media(user_rating_gt=7)["items"]} == {b}
    assert {row["id"] for row in store.media(user_rating_lt=7)["items"]} == {a}
    assert not store.media(rating_gt=7, user_rating_gt=7)["items"]
    assert store.media(rating_lt=7, user_rating_gt=7)["total"] == 1


def test_unrated_are_excluded_and_bounds_are_validated(tmp_path):
    from pytest import raises
    store = Store(tmp_path / "scores.sqlite")
    store.initialize()
    store.save_internal_media(InternalMediaInput(
        id="dbba2222-1234-4234-9234-220022002203",
        kind="movie", title="Unrated", tmdb_id="17773", format="",
    ).model_dump())
    assert store.media(user_rating_gt=0)["total"] == 0
    assert store.media(user_rating_lt=10)["total"] == 0
    with raises(ValueError):
        store.media(rating_gt=11)



def test_streaming_catalogue_metrics_use_canonical_tmdb_and_personal_state(tmp_path):
    from types import SimpleNamespace

    from home_media_catalog.streaming import build_streaming_router

    store = Store(tmp_path / "metrics.sqlite")
    store.initialize()
    media_id = store.save_internal_media(InternalMediaInput(
        id="dbba2222-1234-4234-9234-220022002204",
        kind="tvshow", title="Watchable Series", tmdb_id="17774",
        rating=7.7, format="",
    ).model_dump())["id"]
    store.update_personal(media_id, {"watched": True, "user_rating": 9.5})

    router = build_streaming_router(store, SimpleNamespace(tmdb_api_token=""))
    endpoint = next(
        route.endpoint for route in router.routes
        if route.path == "/api/v1/streaming/catalogue-metrics"
    )
    result = endpoint()
    assert result["items"] == [{
        "kind": "tv", "tmdb_id": 17774,
        "watch_status": "watched", "rating": 7.7, "user_rating": 9.5,
    }]
