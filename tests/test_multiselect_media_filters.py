"""Catalogue multi-select facets remain OR within a category and AND across categories."""

import pytest

from home_media_catalog.connectors.base import Record


def test_multiselect_filter_uses_or_within_facets_and_and_between_them(store):
    clz = store.save_source({"name": "Collection", "type": "clz"})["id"]
    kodi = store.save_source({"name": "Kodi", "type": "kodi"})["id"]
    store.apply_snapshot(
        clz,
        [
            Record("a", "movie", "First Drama", genres=["Drama"], watched=True, collections=["Saga A - saga"]),
            Record("b", "movie", "Second Fantasy", genres=["Fantasy"], watched=False, collections=["Saga B"]),
        ],
    )
    store.apply_snapshot(
        kodi,
        [
            Record("c", "movie", "Third Drama", genres=["Drama"], watched=False, collections=["Saga A - saga"]),
            Record("d", "movie", "Fourth Comedy", genres=["Comedy"], watched=None),
        ],
    )

    assert store.media(watch_status=["watched", "unwatched"])["total"] == 3
    assert store.media(genre=["Drama", "Fantasy"])["total"] == 3
    assert store.media(source_type=["clz", "kodi"])["total"] == 4

    page = store.media(
        watch_status=["watched", "unwatched"],
        genre=["Drama", "Fantasy"],
        source_type=["clz"],
    )
    assert {item["title"] for item in page["items"]} == {"First Drama", "Second Fantasy"}
    assert page["total"] == 2

    # Scalar callers and existing collection constraints keep working.
    assert store.media(genre="Drama", source_type="kodi")["total"] == 1
    assert store.media(collection="Saga A - saga")["total"] == 2


@pytest.mark.parametrize(
    "filters",
    [
        {"watch_status": ["watched", "invalid"]},
        {"watch_status": ["watched"] * 5},
        {"genre": ["Drama"] * 41},
        {"source_type": ["x" * 201]},
    ],
)
def test_multiselect_rejects_invalid_values(store, filters):
    with pytest.raises(ValueError):
        store.media(**filters)
