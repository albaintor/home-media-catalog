"""Contracts for web film-saga grouping and sort parity with iOS."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "home_media_catalog/static/app.js"
HTML = ROOT / "home_media_catalog/static/index.html"


def test_collection_grouping_controls_present():
    html = HTML.read_text()
    script = WEB.read_text()
    assert 'id="merge-collections"' in html
    assert "'merge-collections').addEventListener('change'" in script
    assert "isSagaCollection" in script
    assert "normalizedSagaName" in script
    assert "collectionMetric" in script


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_collection_grouping_numeric_aliases_disc_formats_and_aggregates():
    source = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const helpers = src.slice(src.indexOf('const nonSagaCollections'),
    src.indexOf('let filtersRestored=false'));
const funcs = src.slice(src.indexOf('function collectionMetric('),
    src.indexOf('function collectionArtwork('));
const state = {mergeEquivalentCollections:true, order:'desc', sort:'date_added'};
const $ = () => ({value:state.sort});
const api = Function('state','$','displayLocale',helpers+'\n'+funcs+
    '\nreturn {collectionGroups,isSagaCollection};')(state,$,()=> 'fr');
const media = [
    {id:'a',title:'Film A',collections:['OSS 117 la trilogie','Blu-Ray'],
        date_added:'2020-01-01',year:2018,rating:8,user_rating:9},
    {id:'b',title:'Film B',collections:['OSS 117 la trilogie (2)','Blu-Ray'],
        date_added:'2024-01-01',year:2022,rating:6,user_rating:7},
    {id:'c',title:'Film C',collections:['Blu-Ray'],
        date_added:'2025-01-01',year:2025,rating:4},
    {id:'d',title:'Film D',collections:['Une autre saga'],
        date_added:'2026-01-01',year:2026,rating:10}
];
assert.equal(api.isSagaCollection('Blu-Ray'),false);
let groups = api.collectionGroups(media);
let oss = groups.find(g=>g.title==='OSS 117 la trilogie');
assert.equal(oss.items.length,2);
assert.equal(groups.find(g=>g.title==='Autres titres').items.length,1);
assert.equal(groups[0].title,'Une autre saga');
state.sort='rating';
groups = api.collectionGroups(media);
oss = groups.find(g=>g.title==='OSS 117 la trilogie');
assert.equal(oss.metric,7);
assert.equal(groups[0].title,'Une autre saga');
state.sort='year';
assert.equal(api.collectionGroups(media).find(g=>g.title==='OSS 117 la trilogie').metric,2022);
state.mergeEquivalentCollections=false;
assert.equal(api.collectionGroups(media).filter(g=>g.title.startsWith('OSS 117')).length,1);
state.sort='title';
const distinct=[{id:'v',collections:['Avengers']},{id:'s',collections:['Avengers - Saga']}];
assert.equal(api.collectionGroups(distinct).length,2);
state.mergeEquivalentCollections=true;
assert.equal(api.collectionGroups(distinct).length,1);
const batman=[
  {id:'b1',collections:['Batman','Batman collection'],
   collection_sources:{Batman:['kodi'], 'Batman collection':['clz']}},
  {id:'b2',collections:['Batman','Batman collection'],
   collection_sources:{Batman:['kodi'], 'Batman collection':['clz']}}
];
const mergedBatman=api.collectionGroups(batman);
assert.equal(mergedBatman.length,1);
assert.equal(mergedBatman[0].title,'Batman');
assert.equal(mergedBatman[0].items.length,2);
const asterix=[
  ...['one','two'].map(id=>({id,collections:['Astérix & Obélix (Animation)','Astérix et Obélix (animation)'],
    collection_sources:{'Astérix & Obélix (Animation)':['kodi'],'Astérix et Obélix (animation)':['clz']}})),
  ...['three','four'].map(id=>({id,collections:['Astérix & Obélix (Animation)'],
    collection_sources:{'Astérix & Obélix (Animation)':['kodi']}})),
  ...['five','six','seven','eight'].map(id=>({id,collections:['Astérix et Obélix (animation)'],
    collection_sources:{'Astérix et Obélix (animation)':['clz']}}))
];
assert.equal(api.collectionGroups(asterix).length,1);
assert.equal(api.collectionGroups(asterix)[0].items.length,8);
assert.equal(api.collectionGroups(asterix)[0].title,'Astérix et Obélix (animation)');
const sameSource=batman.map(item=>({...item,
  collection_sources:{Batman:['kodi'],'Batman collection':['kodi']}}));
assert.equal(api.collectionGroups(sameSource).length,2);
assert.equal(api.collectionGroups(batman.slice(0,1)).length,2);
state.mergeEquivalentCollections=false;
assert.equal(api.collectionGroups(batman).length,2);
"""
    subprocess.run(["node", "-e", source, str(WEB)], check=True, capture_output=True, text=True)
