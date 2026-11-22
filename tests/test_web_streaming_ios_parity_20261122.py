"""Regression checks for iOS streaming parity on the web (22 November 2026)."""
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET

import pytest

WEB = Path(__file__).resolve().parents[1] / "home_media_catalog" / "static"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js unavailable")
def test_streaming_javascript_parses():
    result = subprocess.run(
        ["node", "--check", str(WEB / "streaming.js")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js unavailable")
def test_personal_shelves_hide_when_search_or_services_do_not_match():
    script = r"""
const fs=require('node:fs'),assert=require('node:assert/strict');
const code=fs.readFileSync(process.argv[1],'utf8');
const section=code.slice(code.indexOf('function matchesTitleText('),
                         code.indexOf('function subscribedServices()'));
assert.ok(section.startsWith('function matchesTitleText('));
const state={query:'',kind:'both',visible:['netflix'],shelves:new Map()};
const boxes={'streaming-saved':{innerHTML:''}};
const rows=[
  {payload:{kind:'movie',title:'Amélie',original_title:'Le Fabuleux Destin d Amélie Poulain',service_keys:['netflix']}},
  {payload:{kind:'tv',title:'Another series',original_title:'Another series',service_keys:['prime_video']}}
];
let allowed=true;
const render=Function('state','$','personalRows','normalizeTitle',
  'matchesStreamingFilters','servicesFor','shelf','card',
  section+'\nreturn renderSaved;')(
    state,id=>boxes[id],kind=>rows.filter(x=>x.payload.kind===kind),
    row=>row.payload,()=>allowed,title=>title.service_keys,
    id=>'SHELF:'+id,()=>'<article></article>'
  );
render();
assert.equal(boxes['streaming-saved'].innerHTML,'SHELF:saved-movie');
state.query='not available';render();
assert.equal(boxes['streaming-saved'].innerHTML,'');
state.query='amelie';render();
assert.equal(boxes['streaming-saved'].innerHTML,'SHELF:saved-movie');
state.query='';state.visible=[];render();
assert.equal(boxes['streaming-saved'].innerHTML,'');
state.visible=['prime_video'];render();
assert.equal(boxes['streaming-saved'].innerHTML,'SHELF:saved-tv');
state.kind='movie';render();
assert.equal(boxes['streaming-saved'].innerHTML,'');
state.kind='both';allowed=false;render();
assert.equal(boxes['streaming-saved'].innerHTML,'');
"""
    result = subprocess.run(
        ["node", "-e", script, str(WEB / "streaming.js")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_empty_discovery_shelves_are_hidden_after_loading():
    code = (WEB / "streaming.js").read_text()
    assert "element.hidden=!visible.length;" in code
    assert "if(!visible.length)return;" in code
    assert "if(def?.loaded)renderDiscoveryShelf(element,def);" in code
    assert "if(!visible.length)return ''" in code


def test_web_search_remains_independent_from_discovery_filters():
    code = (WEB / "streaming.js").read_text()
    search = code[code.index("async function performSearch()"):
                  code.index("function renderBrowser()")]
    assert "kind:'both'" in search
    assert "for(const kind of ['tv','movie'])" in search
    assert "filter(matchesStreamingFilters)" not in search
    assert "const items=(result.items||[]).filter(x=>x.kind===kind)" in search


def test_full_category_search_is_remote_and_category_scoped():
    code = (WEB / "streaming.js").read_text()
    html = (WEB / "index.html").read_text()
    assert 'id="streaming-subview-search"' in html
    search = code[code.index("async function performSubviewSearch()"):
                  code.index("let streamingSubviewObserver=null;")]
    for required in ("streaming/search?", "kind:def.kind",
                     "origin_country", "original_language",
                     "publicCachedRequest", "state.subview!==subview"):
        assert required in search
    assert "subviewSearchTimer=setTimeout" in code


def test_genre_artwork_matches_ios_and_contains_all_new_categories():
    js = (WEB / "streaming.js").read_text()
    css = (WEB / "streaming.css").read_text()
    expected = ("action","adventure","animation","comedy","documentary",
                "drama","family","fantasy","history","horror","kids",
                "romance","science-fiction","war","western")
    for name in expected:
        asset = WEB / "streaming-genres" / f"{name}.svg"
        assert asset.exists(), f"Missing illustration: {name}"
        assert ET.parse(asset).getroot().tag.endswith("svg")
    for key in ("kids","western","history","war_movies","war_politics"):
        assert f"'{key}'" in js
    assert "streaming-kind-mixed" in css
    assert "mask:var(--genre-art)" in css
    assert "#b49aff" in css and "#4bd7cd" in css
