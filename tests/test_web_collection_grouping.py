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
assert.equal(groups.find(g=>g.title==='Film C').items[0].id,'c');
assert.ok(!groups.some(g=>g.title==='Autres titres'));
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
// A Kodi one-film set is redundant once the same canonical film appears
// alongside the sequel in a larger physical collection, in either merge mode.
const dune=[
  {id:'dune-one',title:'Dune',year:2021,collections:['Dune','Dune 1 & 2'],
   collection_sources:{Dune:['kodi'],'Dune 1 & 2':['clz']}},
  {id:'dune-two',title:'Dune: Part Two',year:2024,collections:['Dune 1 & 2'],
   collection_sources:{'Dune 1 & 2':['clz']}}
];
for(const merge of [false,true]){
  state.mergeEquivalentCollections=merge;
  const visible=api.collectionGroups(dune);
  assert.equal(visible.length,1);
  assert.equal(visible[0].title,'Dune 1 & 2');
  assert.deepEqual(visible[0].items.map(item=>item.id).sort(),['dune-one','dune-two']);
}
// Do not hide a singleton which does not actually share a canonical work ID.
const distinctMovies=[
  {id:'single',title:'Different Dune',collections:['Dune']},
  {id:'boxed-one',title:'Dune',collections:['Dune 1 & 2']},
  {id:'boxed-two',title:'Dune: Part Two',collections:['Dune 1 & 2']}
];
state.mergeEquivalentCollections=false;
assert.equal(api.collectionGroups(distinctMovies).length,2);
// A pair of single-film collections has no larger collection to supersede it.
assert.equal(api.collectionGroups([{id:'one',collections:['Short','Alternate']}]).length,2);
"""
    subprocess.run(["node", "-e", source, str(WEB)], check=True, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_every_clz_film_is_visible_and_title_sort_uses_collection_labels():
    """No film should disappear under a mass 'Other titles' bucket."""
    source = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const helpers = src.slice(src.indexOf('const nonSagaCollections'),
    src.indexOf('let filtersRestored=false'));
const funcs = src.slice(src.indexOf('function collectionMetric('),
    src.indexOf('function collectionArtwork('));
const state = {mergeEquivalentCollections:false, order:'asc', sort:'title'};
const $ = () => ({value:state.sort});
const {collectionGroups} = Function('state','$','displayLocale',
    helpers+'\n'+funcs+'\nreturn {collectionGroups};')(state,$,()=> 'fr');
const media = [
  ...Array.from({length:420},(_,i)=>({
    id:'clz-'+i,title:'Film CLZ '+String(i).padStart(3,'0'),
    collections:['Blu-Ray'],source_types:['clz']
  })),
  {id:'a',title:'Film Z',collections:['A - Saga']},
  {id:'b',title:'Film A',collections:['A - Saga']},
  {id:'c',title:'Film X',collections:['Z - Saga']},
  {id:'d',title:'Film B',collections:['Z - Saga']}
];
for (const merge of [false,true]) {
  state.mergeEquivalentCollections=merge;
  state.sort='title';state.order='asc';
  const groups=collectionGroups(media);
  assert.equal(groups.length,422);
  assert.ok(!groups.some(group=>group.title==='Autres titres'));
  const firstName=merge?'A':'A - Saga',lastName=merge?'Z':'Z - Saga';
  assert.equal(groups[0].title,firstName);
  assert.equal(groups[groups.length-1].title,lastName);
  assert.equal(groups.find(group=>group.title===firstName).items.length,2);
  assert.deepEqual(
    groups.filter(group=>group.id.startsWith('standalone:'))
      .map(group=>group.items[0].id).sort(),
    media.slice(0,420).map(item=>item.id).sort()
  );
  state.order='desc';
  assert.equal(collectionGroups(media)[0].title,lastName);
}
"""
    subprocess.run(["node", "-e", source, str(WEB)], check=True, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_collection_cover_prefers_server_revision_and_orders_fallback_by_year():
    """The grid uses a persisted full collection mosaic instead of partial pages."""
    source = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const helpers = src.slice(src.indexOf('const nonSagaCollections'),
    src.indexOf('let filtersRestored=false'));
const funcs = src.slice(src.indexOf('function collectionMetric('),
    src.indexOf('function renderCards('));
const state = {
  mergeEquivalentCollections:false, order:'asc', collections:[],
  manualCollections:[], internalCollectionsOnly:false,
  indexedCollections:[{
    id:'collection-1',name:'Chronology',item_count:3,is_manual:false,
    artwork:'/api/v1/collections/collection-1/thumbnails/7.jpg?layout=4'
  }]
};
const $ = () => ({value:'title'});
const api = Function('state','$','displayLocale','esc','url',
  helpers+'\n'+funcs+'\nreturn {collectionGroups,collectionMosaicItems,collectionArtwork};'
)(state,$,()=> 'fr',x=>x,x=>x);
const media = [
 {id:'new',title:'New',year:2020,collections:['Chronology'],artwork:{poster:'new.jpg'}},
 {id:'old',title:'Old',year:1980,collections:['Chronology'],artwork:{poster:'old.jpg'}},
 {id:'mid',title:'Middle',year:2005,collections:['Chronology'],artwork:{poster:'mid.jpg'}}
];
const group = api.collectionGroups(media).find(c=>c.title==='Chronology');
assert.equal(group.itemCount,3);
assert.equal(group.artwork,state.indexedCollections[0].artwork);
assert.deepEqual(api.collectionMosaicItems(media).map(m=>m.year),[1980,2005,2020]);
const markup=api.collectionArtwork(group.items,group.artwork,group.itemCount);
assert.ok(markup.includes('/thumbnails/7.jpg?layout=4'));
assert.ok(!markup.includes('new.jpg'));
assert.ok(!markup.includes('old.jpg'));
"""
    subprocess.run(["node", "-e", source, str(WEB)], check=True, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_internal_overlap_preserves_all_grouped_media():
    """Hide overlapping imported groups, retaining their works as media cards."""
    source = r"""
const assert=require('node:assert/strict'),fs=require('node:fs');
const src=fs.readFileSync(process.argv[1],'utf8');
const helpers=src.slice(src.indexOf('const nonSagaCollections'),src.indexOf('let filtersRestored=false'));
const funcs=src.slice(src.indexOf('function collectionMetric('),src.indexOf('function collectionArtwork('));
const state={
  mergeEquivalentCollections:true,order:'asc',collections:[],
  manualCollections:[{id:'manual-faves',name:'Favourites',is_manual:true,item_count:1}],
  internalCollectionsOnly:false,facets:{collections:['Batman','Marvel']},
  indexedCollections:[
    {id:'bat',name:'Batman',is_manual:false,has_manual_overlap:true,item_count:2},
    {id:'mar',name:'Marvel',is_manual:false,has_manual_overlap:false,item_count:2}]
};
const $=id=>({value:id==='search'?'':'title'});
const {collectionGroups}=Function('state','$','displayLocale',
  helpers+'\n'+funcs+'\nreturn {collectionGroups};')(state,$,()=> 'fr');
const media=[
  {id:'a',title:'A',manual_collection_ids:['manual-faves'],collections:['Favourites']},
  {id:'b',title:'B',collections:['Batman']},
  {id:'c',title:'C',collections:['Batman','Marvel']},
  {id:'d',title:'D',collections:['Marvel']},
  {id:'e',title:'E',collections:['Marvel']}
];
for(const merge of [true,false]){
  state.mergeEquivalentCollections=merge;
  const groups=collectionGroups(media);
  assert.ok(!groups.some(g=>g.title==='Batman'));
  assert.ok(groups.some(g=>g.id==='standalone:b'));
  assert.ok(groups.some(g=>g.id==='standalone:c'));
  assert.equal(groups.find(g=>g.id==='manual-faves').items.length,1);
  assert.deepEqual([...new Set(groups.flatMap(g=>g.items.map(i=>i.id)))].sort(),
    media.map(i=>i.id).sort());
}
"""
    subprocess.run(["node", "-e", source, str(WEB)], check=True, capture_output=True, text=True)
