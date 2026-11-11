from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from home_media_catalog.connectors.base import Record
from home_media_catalog.connectors.clz import parse_clz


def source(store, type="clz"):
    return store.save_source({"name": type, "type": type})["id"]


def test_clz_shape_and_normalization(clz_xml):
    entries = parse_clz(clz_xml)
    assert len(entries) == 4
    assert entries[0].identifiers == {"imdb": "tt0123456"}
    assert entries[0].plot == "Une équipe découvre une cité oubliée."
    assert entries[0].watched is None
    assert entries[0].user_rating is None
    assert entries[1].formats == ["Blu-ray"]
    assert entries[-1].kind == "tvshow"
    assert entries[-1].formats == ["3D", "Blu-ray 3D"]


def test_multiple_editions_and_remake_are_preserved(store, clz_xml):
    sid = source(store)
    result = store.apply_snapshot(sid, parse_clz(clz_xml))
    assert result["imported"] == 4
    assert store.status()["works"] == 3
    page = store.media(q="voyage", year=2024)
    assert page["total"] == 1
    detail = store.detail(page["items"][0]["id"])
    assert len(detail["editions"]) == 2
    assert detail["formats"] == ["Blu-ray", "Blu-ray 4K"]
    assert detail["watch_status"] == "unknown"


def test_user_michael_clz_xml_fields_and_kodi_identity(store):
    xml = b"""<collectorz><data><movieinfo><movielist><movie>
      <datasetversion>15</datasetversion>
      <hash>7958691a32ca41f4a9a277f642146bfa</hash>
      <bpmovieid>573347</bpmovieid><bpmediaid>1456697</bpmediaid>
      <title>Michael</title><upc>3701432070216</upc>
      <releasedate><year><displayname>2026</displayname></year></releasedate>
      <formats><format><displayname>4K UHD</displayname></format></formats>
      <audiotracks>
        <audiotrack><displayname>Dolby Digital Plus 7.1 [Fran&#231;ais]</displayname></audiotrack>
        <audiotrack><displayname>Dolby Atmos [Anglaise]</displayname></audiotrack>
      </audiotracks>
      <imdbnr>11378946</imdbnr><imdbrating>7.3</imdbrating>
      <myrating>0</myrating><istvseries boolvalue="0">No</istvseries>
      <collection><displayname>Blu-ray</displayname></collection>
      <collectionstatus listid="3">In Collection</collectionstatus>
    </movie></movielist></movieinfo></data></collectorz>"""
    items = parse_clz(xml)
    assert len(items) == 1
    item = items[0]
    assert item.external_key == "7958691a32ca41f4a9a277f642146bfa"
    assert item.title == "Michael" and item.year == 2026
    assert item.identifiers == {"imdb": "tt11378946"}
    assert item.collections == ["Blu-ray"]
    assert item.metadata["collection_status"] == "In Collection"
    assert item.metadata["clz_movie_id"] == "573347"
    assert item.metadata["clz_media_id"] == "1456697"
    assert item.user_rating is None
    assert {"Blu-ray 4K", "Dolby Digital+", "Dolby Atmos"} <= set(item.formats)

    clz = source(store)
    kodi = source(store, "kodi")
    store.apply_snapshot(clz, items)
    store.apply_snapshot(
        kodi,
        [Record("movie:10:1", "movie", "Michael", year=2026, identifiers={"imdb": "tt11378946"})],
    )
    page = store.media(q="Michael")
    assert page["total"] == 1
    assert page["items"][0]["source_types"] == ["clz", "kodi"]
    assert len(store.detail(page["items"][0]["id"])["editions"]) == 2


def test_import_update_reconciles_matching_imdb_despite_distinct_title_and_year(store):
    clz = source(store)
    kodi = source(store, "kodi")
    store.apply_snapshot(
        clz,
        [Record("physical", "movie", "Michael", year=2026)],
    )
    first_id = store.media()["items"][0]["id"]
    saved = store.save_list({"name": "Films", "description": ""})
    store.change_list_item(saved["id"], first_id)
    store.apply_snapshot(
        kodi,
        [
            Record(
                "movie:4", "movie", "Michael Jackson", year=2025, identifiers={"imdb": "tt11378946"}
            )
        ],
    )
    assert store.media()["total"] == 2
    # Simulates a source gaining an IMDb ID after its initial import.
    store.apply_snapshot(
        clz,
        [Record("physical", "movie", "Michael", year=2026, identifiers={"imdb": "tt11378946"})],
    )
    assert store.media()["total"] == 1
    assert store.detail(first_id)["source_types"] == ["clz", "kodi"]
    assert store.list_page(saved["id"])["total"] == 1


def test_shared_imdb_with_contradictory_tmdb_must_not_reconcile(store):
    clz = source(store)
    kodi = source(store, "kodi")
    store.apply_snapshot(
        clz,
        [
            Record(
                "physical",
                "movie",
                "Michael",
                year=2026,
                identifiers={"imdb": "tt11378946", "tmdb": "111"},
            )
        ],
    )
    store.apply_snapshot(
        kodi,
        [
            Record(
                "movie:4",
                "movie",
                "Michael",
                year=2026,
                identifiers={"imdb": "tt11378946", "tmdb": "222"},
            )
        ],
    )
    assert store.media()["total"] == 2


def test_match_clz_without_imdb_by_original_title_and_year(store):
    clz = source(store)
    kodi = source(store, "kodi")
    store.apply_snapshot(
        clz,
        [
            Record(
                "clz",
                "movie",
                "Film localisé",
                original_title="Example Film",
                year=2026,
                formats=["Blu-ray"],
            )
        ],
    )
    store.apply_snapshot(
        kodi,
        [Record("movie:2:3", "movie", "Example Film", year=2026, identifiers={"tmdb": "3321"})],
    )
    page = store.media(q="Example")
    assert page["total"] == 1
    assert page["items"][0]["source_types"] == ["clz", "kodi"]
    assert store.media(q="localisé")["total"] == 1


def test_title_fallback_rejects_remakes_and_conflicting_ids(store):
    clz = source(store)
    kodi = source(store, "kodi")
    store.apply_snapshot(
        clz,
        [Record("disc", "movie", "Même film", year=2026, identifiers={"imdb": "tt1111111"})],
    )
    store.apply_snapshot(
        kodi,
        [
            Record("movie:1", "movie", "Même film", year=2025),
            Record("movie:2", "movie", "Même film", year=2026, identifiers={"imdb": "tt2222222"}),
        ],
    )
    assert store.media()["total"] == 3


def test_legacy_title_reconciliation_retains_lists_and_rating(store):
    clz = source(store)
    kodi = source(store, "kodi")
    store.apply_snapshot(
        clz,
        [Record("disc", "movie", "Film traduit", year=None)],
    )
    clz_id = store.media()["items"][0]["id"]
    playlist = store.save_list({"name": "À voir", "description": ""})
    store.change_list_item(playlist["id"], clz_id)
    store.update_personal(clz_id, {"user_rating": 8.5})
    store.apply_snapshot(
        kodi,
        [Record("movie:1", "movie", "Film traduit", year=2026, identifiers={"tmdb": "1234"})],
    )
    assert store.media()["total"] == 2
    store.apply_snapshot(
        clz,
        [Record("disc", "movie", "Film traduit", year=2026)],
    )
    assert store.media()["total"] == 1
    detail = store.detail(clz_id)
    assert detail["source_types"] == ["clz", "kodi"]
    assert detail["personal"]["user_rating"] == 8.5
    assert store.list_page(playlist["id"])["items"][0]["id"] == clz_id


def test_kodi_and_clz_merge_by_imdb_and_watch_state(store, clz_xml):
    clz = source(store)
    kodi = source(store, "kodi")
    store.apply_snapshot(clz, parse_clz(clz_xml))
    record = Record(
        "movie:42:12",
        "movie",
        "Le Voyage imaginaire",
        identifiers={"imdb": "tt0123456", "tmdb": "123"},
        year=2024,
        watched=False,
        play_count=0,
        resume_seconds=1800,
        total_seconds=7500,
        date_added="2026-10-01T10:00:00",
    )
    store.apply_snapshot(kodi, [record])
    page = store.media(source_type="kodi", watch_status="in_progress", format="Blu-ray 4K")
    assert page["total"] == 1
    work_id = page["items"][0]["id"]
    detail = store.detail(work_id)
    assert detail["source_types"] == ["clz", "kodi"]
    assert len(detail["editions"]) == 3
    assert detail["identifiers"]["tmdb"] == ["123"]
    store.apply_snapshot(kodi, [replace(record, watched=True, play_count=1, resume_seconds=0)])
    assert store.detail(work_id)["watch_status"] == "watched"
    store.delete_source(kodi)
    assert store.detail(work_id)["watch_status"] == "unknown"
    assert len(store.detail(work_id)["editions"]) == 2


def test_import_is_idempotent_and_replaces_only_selected_source(store, clz_xml):
    sid = source(store)
    records = parse_clz(clz_xml)
    store.apply_snapshot(sid, records)
    original = {
        e["external_key"]: e["id"]
        for m in store.media()["items"]
        for e in store.detail(m["id"])["editions"]
    }
    result = store.apply_snapshot(sid, records)
    assert result["created_editions"] == 0
    assert result["updated_editions"] == 4
    current = {
        e["external_key"]: e["id"]
        for m in store.media()["items"]
        for e in store.detail(m["id"])["editions"]
    }
    assert current == original
    result = store.apply_snapshot(sid, records[:1])
    assert result["removed_editions"] == 3
    assert store.status()["works"] == 1


def test_failed_snapshot_is_rolled_back(store):
    sid = source(store)
    initial = Record("one", "movie", "Initial", identifiers={"imdb": "tt1234567"})
    store.apply_snapshot(sid, [initial])
    with pytest.raises(ValueError, match="duplicate"):
        store.apply_snapshot(
            sid, [Record("new", "movie", "New"), Record("new", "movie", "Duplicate")]
        )
    assert store.media()["total"] == 1
    assert store.media()["items"][0]["title"] == "Initial"


def test_free_text_accents_prefixes_and_query_characters(store):
    sid = source(store)
    store.apply_snapshot(
        sid,
        [
            Record(
                "1",
                "movie",
                "Été à Séoul",
                plot="Une équipe découvre une cité",
                genres=["Aventure"],
            ),
            Record("2", "movie", "Une comédie"),
        ],
    )
    assert store.media(q="ete seou")["total"] == 1
    assert store.media(q="équipe cité")["total"] == 0
    assert store.media(q="équipe cité", deep_search=True)["total"] == 1
    assert store.media(q='"OR" NOT *')["total"] == 0
    assert store.media(q="'")["total"] == 2


def test_default_search_only_title_and_original_across_editions(store):
    a = source(store, "kodi")
    b = source(store, "clz")
    store.apply_snapshot(
        a,
        [
            Record(
                "movie:1:1",
                "movie",
                "Le Voyage magique",
                original_title="The Magical Journey",
                identifiers={"imdb": "tt0123456"},
                plot="Exploration à Tokyo avec Clara",
                genres=["Aventure"],
            ),
            Record("movie:2:2", "movie", "Tokyo Story", plot="Une histoire magique"),
        ],
    )
    store.apply_snapshot(
        b,
        [
            Record(
                "physical-1",
                "movie",
                "La Traversée",
                original_title="The Magical Journey",
                identifiers={"imdb": "tt0123456"},
                plot="Parcours dans les Alpes",
            )
        ],
    )
    assert store.media(q="voyage mag")["total"] == 1
    assert store.media(q="Magical Journey")["total"] == 1
    assert store.media(q="traversée")["total"] == 1
    assert store.media(q="clara")["total"] == 0
    assert store.media(q="clara", deep_search=True)["total"] == 1
    assert store.media(q="alpes", deep_search=True)["total"] == 1
    assert store.media(q="tokyo")["total"] == 1


def test_sort_pagination_combined_filters_and_unknown_watch(store):
    sid = source(store)
    recent = datetime.now(UTC).isoformat(timespec="seconds")
    old = (datetime.now(UTC) - timedelta(days=100)).isoformat(timespec="seconds")
    store.apply_snapshot(
        sid,
        [
            Record(
                "1",
                "movie",
                "Zulu",
                genres=["Action"],
                formats=["4K UHD"],
                rating=7.5,
                date_added=recent,
            ),
            Record(
                "2",
                "movie",
                "Alpha",
                genres=["Action"],
                formats=["DVD"],
                rating=8.5,
                date_added=old,
            ),
            Record("3", "tvshow", "Beta", rating=None),
        ],
    )
    assert store.media(sort="title", order="asc", limit=1)["items"][0]["title"] == "Alpha"
    assert store.media(sort="title", order="asc", limit=1, offset=1)["items"][0]["title"] == "Beta"
    assert store.media(sort="rating", order="desc")["items"][-1]["title"] == "Beta"
    assert (
        store.media(kind="movie", genre="Action", format="4K UHD", source_id=sid, recent_days=30)[
            "total"
        ]
        == 1
    )
    assert store.media(watch_status="unwatched")["total"] == 0
    assert store.media(watch_status="unknown")["total"] == 3
    with pytest.raises(ValueError):
        store.media(sort="title;DROP TABLE works")


def test_cumulative_formats_and_legacy_4k_aliases(store):
    sid = source(store)
    store.apply_snapshot(
        sid,
        [
            Record("a", "movie", "Toutes options", formats=["4K", "HDR10+", "Dolby Atmos"]),
            Record("b", "movie", "Sans Atmos", formats=["4K", "HDR10+"]),
            Record("c", "movie", "Disque UHD", formats=["4K UHD", "Dolby Atmos"]),
        ],
    )
    assert store.media(format=["4K", "HDR10+", "Dolby Atmos"])["total"] == 1
    assert store.media(format=["Blu-ray 4K", "Dolby Atmos"])["total"] == 1
    assert store.media(format=["4K UHD", "Dolby Atmos"])["total"] == 1
    assert store.media(format="4K")["total"] == 2
    assert "Blu-ray 4K" in store.facets()["formats"]
    with pytest.raises(ValueError):
        store.media(format=["x"] * 21)


def test_4k_disc_is_separate_from_kodi_video_resolution(store):
    clz = source(store)
    kodi = source(store, "kodi")
    store.apply_snapshot(clz, [Record("disc", "movie", "Disque", formats=["4K UHD", "DVD"])])
    store.apply_snapshot(kodi, [Record("file", "movie", "Vidéo", formats=["4K"])])
    assert store.media(format="Blu-ray 4K")["total"] == 1
    assert store.media(format="4K UHD")["total"] == 1
    assert store.media(format="4K")["total"] == 1
    assert store.media(format="DVD")["total"] == 1


def test_conflicting_external_ids_do_not_merge(store):
    sid = source(store)
    store.apply_snapshot(
        sid,
        [
            Record("a", "movie", "Original", identifiers={"imdb": "tt1234567", "tmdb": "1"}),
            Record("b", "movie", "Different", identifiers={"imdb": "tt1234567", "tmdb": "2"}),
        ],
    )
    assert store.status()["works"] == 2


def test_recycled_kodi_id_does_not_replace_physical_collection_work(store):
    clz, kodi = source(store), source(store, "kodi")
    store.apply_snapshot(
        clz, [Record("disc", "movie", "Original", identifiers={"imdb": "tt1234567"})]
    )
    store.apply_snapshot(
        kodi, [Record("movie:1:1", "movie", "Original", identifiers={"imdb": "tt1234567"})]
    )
    old_id = store.media()["items"][0]["id"]
    store.apply_snapshot(
        kodi, [Record("movie:1:1", "movie", "Replacement", identifiers={"imdb": "tt7654321"})]
    )
    assert store.status()["works"] == 2
    assert store.detail(old_id)["title"] == "Original"


def test_parent_links_survive_show_and_episode_import(store):
    sid = source(store, "kodi")
    store.apply_snapshot(
        sid,
        [
            Record(
                "episode:2",
                "episode",
                "Episode",
                parent_key="tvshow:1",
                season=1,
                episode=2,
                watched=False,
            ),
            Record("tvshow:1", "tvshow", "Series", watched=False),
        ],
    )
    parent = store.media(kind="tvshow")["items"][0]["id"]
    assert store.media(parent_id=parent)["total"] == 1
    assert store.media(parent_id=parent)["items"][0]["episode"] == 2


def test_kodi_reconciliation_deletes_missing_files_but_retains_clz_work(store):
    """Removing Kodi editions must not erase the linked physical collection."""
    clz = source(store)
    kodi = source(store, "kodi")
    store.apply_snapshot(
        clz,
        [
            Record(
                "disc-1",
                "movie",
                "Film en collection",
                identifiers={"imdb": "tt0123456"},
                formats=["Blu-ray"],
                watched=None,
            )
        ],
    )
    version_a = Record(
        "movie:42:10",
        "movie",
        "Film dans Kodi",
        identifiers={"imdb": "tt0123456", "tmdb": "123"},
        formats=["4K"],
        watched=False,
        metadata={"file_id": 10, "file": "video.mkv"},
    )
    version_b = replace(
        version_a,
        external_key="movie:42:11",
        formats=["1080p"],
        metadata={"file_id": 11, "file": "alternative.mkv"},
    )
    kodi_only = Record(
        "movie:99:19",
        "movie",
        "Film numérique seul",
        identifiers={"imdb": "tt7654321"},
        watched=True,
    )
    store.apply_snapshot(kodi, [version_a, version_b, kodi_only])
    shared_id = store.media(q="Film dans Kodi")["items"][0]["id"]
    gone_id = store.media(q="Film numérique seul")["items"][0]["id"]
    collection = store.save_list({"name": "Ma sélection", "description": ""})
    store.change_list_item(collection["id"], shared_id)
    store.update_personal(shared_id, {"user_rating": 9.0})

    # Kodi removes an alternative version and a digital-only film while
    # updating the remaining Kodi version's metadata.
    result = store.apply_snapshot(
        kodi, [replace(version_a, title="Titre Kodi actualisé", formats=["HDR10+"])]
    )
    assert result["removed_editions"] == 2
    assert result["removed_works"] == 1
    assert result["kept_clz_works"] == 0
    assert store.detail(gone_id) is None
    current = store.detail(shared_id)
    assert current["title"] == "Titre Kodi actualisé"
    assert current["formats"] == ["Blu-ray", "HDR10+"]
    assert len(current["editions"]) == 2
    assert {e["external_key"] for e in current["editions"]} == {"disc-1", "movie:42:10"}

    # When the final Kodi file disappears, CLZ and user data survive.
    result = store.apply_snapshot(kodi, [])
    assert result["removed_editions"] == 1
    assert result["removed_works"] == 0
    assert result["kept_clz_works"] == 1
    assert store.media(source_type="kodi")["total"] == 0
    assert store.media(source_type="clz")["total"] == 1
    current = store.detail(shared_id)
    assert current["title"] == "Film en collection"
    assert current["formats"] == ["Blu-ray"]
    assert current["source_types"] == ["clz"]
    assert current["user_rating"] == 9.0
    assert len(current["editions"]) == 1
    assert current["editions"][0]["source_type"] == "clz"
    assert store.lists()[0]["item_count"] == 1

    # The same CLZ work UUID is reused if Kodi later reimports the film.
    result = store.apply_snapshot(kodi, [version_a])
    assert result["created_editions"] == 1
    assert store.media()["total"] == 1
    assert store.detail(shared_id)["source_types"] == ["clz", "kodi"]


def test_empty_kodi_snapshot_does_not_remove_unrelated_clz_entries(store):
    clz, kodi = source(store), source(store, "kodi")
    store.apply_snapshot(clz, [Record("disc", "movie", "CLZ indépendant")])
    store.apply_snapshot(kodi, [Record("movie:1:5", "movie", "Kodi seul")])
    result = store.apply_snapshot(kodi, [])
    assert result["removed_editions"] == 1
    assert result["removed_works"] == 1
    assert result["kept_clz_works"] == 0
    assert store.media()["total"] == 1
    assert store.media()["items"][0]["title"] == "CLZ indépendant"


def test_removed_kodi_only_title_is_deleted_even_when_watched_and_listed(store):
    kodi = source(store, "kodi")
    initial = Record(
        "movie:19:20",
        "movie",
        "Film supprimé",
        identifiers={"imdb": "tt0000019"},
    )
    store.apply_snapshot(kodi, [initial])
    work_id = store.media()["items"][0]["id"]
    collection = store.save_list({"name": "À revoir", "description": ""})
    store.change_list_item(collection["id"], work_id)
    store.update_personal(work_id, {"watched": True, "user_rating": 8.5})
    store.save_enrichment(work_id, {"trailers": []})

    assert store.detail(work_id)["watch_status"] == "watched"
    assert store.lists()[0]["item_count"] == 1

    result = store.apply_snapshot(kodi, [])
    assert result["removed_editions"] == 1
    assert result["removed_works"] == 1
    assert result["removed_titles"] == ["Film supprimé"]
    assert result["removed_titles_total"] == 1
    assert store.media()["total"] == 0
    assert store.detail(work_id) is None
    assert store.lists()[0]["item_count"] == 0
    with store.connect() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM personal WHERE work_id=?", (work_id,)).fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM enrichment WHERE work_id=?", (work_id,)).fetchone()[
                0
            ]
            == 0
        )


def test_removed_titles_report_is_distinct_and_limited(store):
    kodi = source(store, "kodi")
    records = [Record(f"movie:{number}:1", "movie", f"Film {number}") for number in range(25)]
    records.append(Record("movie:1:2", "movie", "Film 1"))
    store.apply_snapshot(kodi, records)
    result = store.apply_snapshot(kodi, [])
    assert result["removed_editions"] == 26
    assert result["removed_titles_total"] == 25
    assert len(result["removed_titles"]) == 20
    assert result["removed_titles"][0] == "Film 0"


def test_kodi_snapshot_replacement_removes_absent_editions_after_file_id_changes(store):
    """An import is an authoritative snapshot, not an append-only Kodi cache."""
    kodi = source(store, "kodi")
    a = Record("movie:21:100", "movie", "Retired movie", identifiers={"imdb": "tt0000021"})
    b = Record("movie:22:200", "movie", "Active movie", identifiers={"imdb": "tt0000022"})
    store.apply_snapshot(kodi, [a, b])
    doomed = store.media(q="Retired movie")["items"][0]["id"]
    collection = store.save_list({"name": "Mes films", "description": ""})
    store.change_list_item(collection["id"], doomed)
    store.update_personal(doomed, {"watched": True})

    # The retained movie may change its file ID during a new Kodi scan.
    # Its identity and personal properties must not affect deletion of A.
    updated = Record("movie:22:201", "movie", "Active movie", identifiers=b.identifiers)
    result = store.apply_snapshot(kodi, [updated])
    assert result["source_records_before"] == 2
    assert result["source_records_after"] == 1
    assert result["removed_editions"] == 2
    assert result["created_editions"] == 1
    assert store.media(q="Retired movie")["total"] == 0
    assert store.detail(doomed) is None
    assert store.lists()[0]["item_count"] == 0
    assert store.media(source_type="kodi")["total"] == 1
    with store.connect() as conn:
        assert [
            row[0]
            for row in conn.execute("SELECT external_key FROM records WHERE source_id=?", (kodi,))
        ] == ["movie:22:201"]


def test_recent_jobs_include_running_and_completed_snapshots(store):
    sid = source(store, "kodi")
    pending = store.new_job(sid)
    assert store.jobs()[0]["status"] == "pending"
    store.start_job(pending["id"])
    assert store.jobs()[0]["status"] == "running"
    store.finish_job(
        pending["id"],
        result={
            "database": "MyVideos131",
            "removed_editions": 2,
            "source_records_after": 10,
        },
    )
    report = store.jobs()[0]
    assert report["status"] == "completed"
    assert report["result"]["database"] == "MyVideos131"
    assert report["result"]["removed_editions"] == 2


def test_job_progress_tracks_actual_stage_counts_and_finishes(store):
    sid = source(store, "kodi")
    job = store.new_job(sid)
    store.start_job(job["id"])
    store.update_job_progress(job["id"], "movies", 12, 40)
    current = store.job(job["id"])
    assert current["status"] == "running"
    assert current["progress_phase"] == "movies"
    assert current["progress_current"] == 12
    assert current["progress_total"] == 40
    store.update_job_progress(job["id"], "publishing")
    assert store.job(job["id"])["progress_phase"] == "publishing"
    store.finish_job(job["id"], result={"imported": 40})
    assert store.job(job["id"])["progress_phase"] == "completed"


def test_job_progress_migration_preserves_old_jobs(tmp_path):
    import sqlite3

    from home_media_catalog.store import Store

    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE jobs (id TEXT PRIMARY KEY, source_id TEXT NOT NULL, "
            "status TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT, "
            "result TEXT, error TEXT)"
        )
        connection.execute(
            "INSERT INTO jobs(id, source_id, status, created_at) VALUES('job', 'source', 'completed', '2026-01-01')"
        )
    migrated = Store(database)
    migrated.initialize()
    job = migrated.job("job")
    assert job["progress_current"] == 0
    assert job["progress_total"] == 0
    assert job["status"] == "completed"


def test_repeat_sync_skips_identical_works_and_fts_writes(store, monkeypatch):
    """A large unchanged MyVideos snapshot must not trigger reindexing."""
    kodi = source(store, "kodi")
    records = [
        Record("movie:1:10", "movie", "Film A", identifiers={"imdb": "tt0000001"}),
        Record("movie:2:20", "movie", "Film B", identifiers={"imdb": "tt0000002"}),
    ]
    store.apply_snapshot(kodi, records)
    original = {v["title"]: v["id"] for v in store.media()["items"]}
    from home_media_catalog.store import Store

    rebuilt = []
    original_rebuild = Store._rebuild

    def record_rebuild(conn, work_id):
        rebuilt.append(work_id)
        return original_rebuild(conn, work_id)

    monkeypatch.setattr(Store, "_rebuild", staticmethod(record_rebuild))
    unchanged = store.apply_snapshot(kodi, records)
    assert unchanged["updated_editions"] == 2
    assert unchanged["unchanged_editions"] == 2
    assert unchanged["rebuilt_works"] == 0
    assert unchanged["removed_editions"] == 0
    assert rebuilt == []
    # Only the modified work is indexed; both UUIDs remain stable.
    altered = store.apply_snapshot(kodi, [records[0], replace(records[1], title="Film B revu")])
    assert altered["unchanged_editions"] == 1
    assert altered["rebuilt_works"] == 1
    assert len(rebuilt) == 1
    assert store.media(q="Film B revu")["items"][0]["id"] == original["Film B"]


def test_overall_progress_is_monotonic_between_movies_series_episodes(store):
    source_id = source(store, "kodi")
    job = store.new_job(source_id)
    store.start_job(job["id"])
    values = [store.job(job["id"])["overall_percent"]]
    for phase, current, total in (
        ("loading", 0, 0),
        ("movies", 0, 30),
        ("movies", 15, 30),
        ("movies", 30, 30),
        ("series", 0, 3),
        ("series", 3, 3),
        ("episodes", 0, 45),
        ("episodes", 45, 45),
        ("publishing", 0, 0),
    ):
        store.update_job_progress(job["id"], phase, current, total)
        values.append(store.job(job["id"])["overall_percent"])
    store.finish_job(job["id"], result={"imported": 78})
    values.append(store.job(job["id"])["overall_percent"])
    assert values == sorted(values)
    assert values[-1] == 100
    assert 100 not in values[:-1]
    assert values[3] < values[4]


def test_advanced_full_credit_name_avoids_independent_token_false_positives(store):
    sid = source(store)
    store.apply_snapshot(
        sid,
        [
            Record(
                "john-williams-score",
                "movie",
                "Les étoiles lointaines",
                plot="Une aventure dans les étoiles.",
                metadata={"people": [{"name": "John Williams", "role": "Music", "tmdb_id": "491"}]},
            ),
            Record(
                "unrelated-tokens",
                "movie",
                "John sur la route",
                plot="Le détective Williams revient au village.",
            ),
            Record(
                "split-credits",
                "movie",
                "La comédie",
                metadata={
                    "people": [
                        {"name": "John Smith", "role": "Actor"},
                        {"name": "Jane Williams", "role": "Actor"},
                    ]
                },
            ),
            Record(
                "reversed-title",
                "movie",
                "Williams John",
                plot="Aucun lien avec le compositeur.",
            ),
            Record(
                "documentary",
                "movie",
                "John Williams : la biographie",
                plot="Portrait d'un musicien, sans crédit de composition.",
            ),
        ],
    )
    advanced = store.media(q="John Williams", deep_search=True)
    assert [item["title"] for item in advanced["items"]] == ["Les étoiles lointaines"]
    assert advanced["items"][0]["matched_roles"] == ["Music"]
    assert store.media(q="john", deep_search=True)["total"] > 1
    # Title-only search retains its existing token-prefix behavior.
    assert store.media(q="John Williams", deep_search=False)["total"] == 2


def test_advanced_multiword_search_falls_back_to_contiguous_phrase(store):
    sid = source(store)
    store.apply_snapshot(
        sid,
        [
            Record(
                "one", "movie", "Un film sur le cinéma", plot="John Williams compose la musique."
            ),
            Record("two", "movie", "John dans les bois", plot="Williams est un détective."),
            Record("three", "movie", "Williams John", plot="Ordre inverse."),
        ],
    )
    page = store.media(q="John Williams", deep_search=True)
    assert [item["title"] for item in page["items"]] == ["Un film sur le cinéma"]
    assert store.media(q="John Williams", deep_search=False)["total"] == 1

def test_reset_content_preserves_source_players_and_empty_lists(store):
    from home_media_catalog.connectors.base import Record

    sid = source(store, "kodi")
    player = store.save_player({
        "name": "Kodi Chambre", "source_id": sid,
        "host": "192.0.2.20", "port": 8080,
        "username": "kodi", "password": "private-local-secret",
    })
    store.apply_snapshot(sid, [Record("movie:41", "movie", "Sample movie")])
    movie_id = store.media()["items"][0]["id"]
    saved_list = store.save_list({"name": "Favorites", "description": ""})
    store.change_list_item(saved_list["id"], movie_id)

    result = store.maintain_catalogue(reset_content=True)

    assert result["status"] == "content_reset"
    assert store.media()["total"] == 0
    assert store.source(sid) is not None
    assert store.player(player["id"])["config"]["password"] == "private-local-secret"
    assert store.list_page(saved_list["id"])["total"] == 0
    assert any(row["id"] == player["id"] for row in store.players())
    assert any(row["id"] == sid for row in store.sources())
    # Existing source IDs remain valid for subsequent imports.
    store.apply_snapshot(sid, [Record("movie:41", "movie", "Sample movie")])
    assert store.media()["total"] == 1


def test_reset_content_requires_confirmation_for_api(client, admin):
    endpoint = "/api/v1/catalogue/maintenance/reset_content"
    assert client.post(endpoint).status_code == 401
    assert client.post(endpoint, headers=admin).status_code == 422
    headers = {**admin, "X-Confirm-Catalogue-Maintenance": "reset_content"}
    response = client.post(endpoint, headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "content_reset"


def test_fast_scroll_title_uses_server_sort_title_and_both_directions(store):
    """An imported sort title can differ from the poster's displayed title."""
    sid = source(store)
    store.apply_snapshot(
        sid,
        [
            Record("a", "movie", "Alpha"),
            Record("m", "movie", "The Matrix", sort_title="Matrix, The"),
            Record("n1", "movie", "Nerve"),
            Record("n2", "movie", "Nope"),
            Record("z", "movie", "Zootopia"),
        ],
    )
    expected = {
        "asc": {"A": 0, "M": 1, "N": 2, "Z": 4},
        "desc": {"Z": 0, "N": 1, "M": 3, "A": 4},
    }
    for direction, letters in expected.items():
        ordered = store.media(sort="title", order=direction, limit=20)["items"]
        for letter, position in letters.items():
            page = store.media(
                sort="title", order=direction, limit=1,
                fast_scroll_target=letter,
            )
            assert page["target_offset"] == position
            assert ordered[position]["id"] == (
                store.media(
                    sort="title", order=direction, offset=position, limit=1
                )["items"][0]["id"]
            )


def test_clz_physical_seasons_keep_separate_editions(store):
    def entry(number):
        return f"""<movie><hash>season-{number}</hash>
          <bpmediaid>{1000+number}</bpmediaid>
          <title>The Mandalorian: Season {number}</title>
          <series><displayname>The Mandalorian</displayname></series>
          <istvseries boolvalue="1">Yes</istvseries>
          <imdbnr>8111088</imdbnr><upc>{9000+number}</upc>
          <formats><format><displayname>4K UHD</displayname></format></formats>
          <coverfront>https://example.com/season{number}.jpg</coverfront>
        </movie>"""
    xml = ("<collectorz><data><movieinfo><movielist>" +
           "".join(entry(n) for n in (1, 2, 3)) +
           "</movielist></movieinfo></data></collectorz>").encode()
    records = parse_clz(xml)
    assert [r.metadata["physical_season"] for r in records] == [1, 2, 3]
    assert [r.title for r in records] == ["The Mandalorian"] * 3
    sid = source(store)
    store.apply_snapshot(sid, records)
    page = store.media()
    assert page["total"] == 1
    work_id = page["items"][0]["id"]
    assert page["items"][0]["title"] == "The Mandalorian"
    detail = store.detail(work_id)
    assert {e["metadata"]["barcode"] for e in detail["editions"]} == {"9001", "9002", "9003"}
    assert len(detail["editions"]) == 3
    seasons = store.seasons(work_id)
    assert [s["season"] for s in seasons] == [1, 2, 3]
    assert all(s["owned_physical"] and s["physical_edition_count"] == 1
               and s["episode_count"] == 0 for s in seasons)
    assert store.season_poster(work_id, 2) == ["https://example.com/season2.jpg"]
    store.apply_snapshot(sid, parse_clz(xml))
    assert store.media()["total"] == 1
    assert len(store.detail(work_id)["editions"]) == 3
