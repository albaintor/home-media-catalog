"""Regression tests for search within the web collection-grouped catalogue."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest


APP = Path(__file__).resolve().parents[1] / "home_media_catalog" / "static" / "app.js"


def _grouped(search, *, items=(), indexed=(), internal=False):
    """Run the *actual* collectionGroups JS with a minimal DOM fixture."""
    if not shutil.which("node"):
        pytest.skip("Node.js is not installed")
    fixture = dict(search=search, items=list(items), indexed=list(indexed), internal=internal)
    runner = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const fixture = JSON.parse(process.argv[2]);
const js = fs.readFileSync(process.argv[1], 'utf8');
const source = 'function collectionGroups(items) {' +
  js.split('function collectionGroups(items) {')[1].split('\n// Mosaic slots')[0];
const collectionFold = name => String(name || '').normalize('NFD')
  .replace(/[\u0300-\u036f]/g, '').toLocaleLowerCase().trim();
const state = {
  indexedCollections: fixture.indexed,
  manualCollections: fixture.indexed.filter(c => c.is_manual),
  collections: [],
  internalCollectionsOnly: fixture.internal,
  mergeEquivalentCollections: false,
  order: 'asc'
};
const context = {
  state,
  items: fixture.items,
  $: id => ({value: id === 'search' ? fixture.search : 'title'}),
  collectionFold,
  collectionSelectionKey: collectionFold,
  collectionName: name => name,
  normalizedSagaName: name => name,
  isSagaCollection: name => Boolean(name),
  mergeCollectionGroupsByMembers: groups => [...groups.values()],
  collectionMetric: () => null,
  displayLocale: () => 'fr'
};
const result = vm.runInNewContext(source +
  '\ncollectionGroups(items).map(g => ({id:g.id,title:g.title,count:g.itemCount||g.items.length}))',
  context);
process.stdout.write(JSON.stringify(result));
"""
    run = subprocess.run(
        ["node", "-e", runner, str(APP), json.dumps(fixture)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(run.stdout)


def _manual(id, name):
    return {"id": id, "name": name, "item_count": 2, "artwork": "/cover.png", "is_manual": True}


def _external(id, name):
    return {"id": id, "name": name, "item_count": 3, "artwork": "/cover.png", "is_manual": False}


def test_search_by_collection_name_does_not_show_every_manual_collection():
    groups = _grouped(
        "scream",
        items=[{"id": "s1", "title": "Scream", "manual_collection_ids": ["manual-scream"]}],
        indexed=[_manual("manual-scream", "Scream"), _manual("manual-other", "Batman")],
    )
    assert [g["title"] for g in groups] == ["Scream"]


def test_search_by_name_finds_collection_even_if_no_member_title_matches():
    groups = _grouped("scream", indexed=[
        _external("external-scream", "Scream Collection"),
        _manual("manual-other", "Batman"),
    ])
    assert [(g["id"], g["count"]) for g in groups] == [("external-scream", 3)]


def test_search_prefers_exact_collection_name_over_partial_matches():
    groups = _grouped("scream", indexed=[
        _manual("manual-scream", "Scream"),
        _manual("manual-universe", "Scream Universe"),
    ])
    assert [g["title"] for g in groups] == ["Scream"]


def test_internal_name_match_suppresses_imported_duplicate():
    groups = _grouped("scream", items=[
        {"id": "a", "title": "Scream", "collections": ["Scream"]},
        {"id": "b", "title": "Scream 2", "manual_collection_ids": ["manual-scream"]},
    ], indexed=[
        _manual("manual-scream", "Scream"),
        _external("external-scream", "Scream"),
    ])
    assert [g["id"] for g in groups] == ["manual-scream"]


def test_no_name_match_only_shows_groups_containing_matching_media():
    groups = _grouped(
        "alien",
        items=[{"id": "a", "title": "Alien",
                "manual_collection_ids": ["manual-favorites"]}],
        indexed=[_manual("manual-favorites", "Favorites"),
                 _manual("manual-unrelated", "Classics")],
    )
    assert [g["title"] for g in groups] == ["Favorites"]


def test_no_name_or_media_results_returns_no_collections():
    assert _grouped("nonexistent", indexed=[
        _manual("manual-favorites", "Favorites"),
        _external("external-scream", "Scream"),
    ]) == []


def test_internal_only_never_shows_external_collection_name_matches():
    groups = _grouped("scream", internal=True, indexed=[
        _external("external-scream", "Scream Collection"),
        _manual("manual-scream", "Scream"),
    ])
    assert [g["id"] for g in groups] == ["manual-scream"]


def test_opening_name_only_search_loads_full_collection_members():
    js = APP.read_text()
    open_collection = js.split("function enterCollection(collectionId) {", 1)[1].split(
        "\nfunction leaveCollection()", 1
    )[0]
    assert "const query=new URLSearchParams({collection_id:indexed.id" in open_collection
    assert "state.openCollectionMembers=members" in open_collection
    assert "state.seq!==sequence||state.openCollection!==collectionId" in open_collection
    assert "items=state.openCollectionMembers??group?.items??[]" in js
    assert "visibleGroups=state.groupBy==='collection'?collectionGroups(state.items):[]" in js
