"""Collection attribution must preserve the exact source of each collection name."""

from home_media_catalog.connectors.base import Record


def test_collection_sources_are_per_import_record_not_per_merged_media(store):
    kodi = store.save_source({"name": "Kodi", "type": "kodi"})["id"]
    clz = store.save_source({"name": "CLZ", "type": "clz"})["id"]
    ids = ["tt0372784", "tt0468569"]
    store.apply_snapshot(
        kodi,
        [
            Record(
                external_key=f"kodi:{index}", kind="movie", title=f"Batman film {index}",
                identifiers={"imdb": imdb}, collections=["Batman"]
            )
            for index, imdb in enumerate(ids)
        ],
    )
    store.apply_snapshot(
        clz,
        [
            Record(
                external_key=f"clz:{index}", kind="movie", title=f"Batman film {index}",
                identifiers={"imdb": imdb}, collections=["Batman collection"]
            )
            for index, imdb in enumerate(ids)
        ],
    )
    page = store.media(sort="title", limit=50)
    assert page["total"] == 2
    for item in page["items"]:
        assert item["collection_sources"] == {
            "Batman": [kodi], "Batman collection": [clz]
        }
        assert {source["id"] for source in item["media_sources"]} == {kodi, clz}
        assert set(item["collections"]) == {"Batman", "Batman collection"}


def test_same_source_can_supply_two_labels_without_falsely_claiming_another(store):
    source_id = store.save_source({"name": "Kodi", "type": "kodi"})["id"]
    store.apply_snapshot(
        source_id,
        [Record("movie:1", "movie", "Film", collections=["Batman", "Batman collection"])],
    )
    (item,) = store.media()["items"]
    assert item["collection_sources"] == {
        "Batman": [source_id], "Batman collection": [source_id]
    }


def test_single_title_with_same_collection_from_two_sources_keeps_both_origins(store):
    kodi = store.save_source({"name": "Kodi", "type": "kodi"})["id"]
    clz = store.save_source({"name": "CLZ", "type": "clz"})["id"]
    matching = {"imdb": "tt0372784"}
    store.apply_snapshot(kodi, [Record("k", "movie", "Batman", identifiers=matching,
                                      collections=["Batman"])])
    store.apply_snapshot(clz, [Record("c", "movie", "Batman", identifiers=matching,
                                     collections=["Batman"])])
    (item,) = store.media()["items"]
    assert item["collection_sources"]["Batman"] == sorted([kodi, clz])
