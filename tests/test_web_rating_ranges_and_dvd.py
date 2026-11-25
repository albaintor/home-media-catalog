"""Regression checks for the DVD mark and the shared web/iOS score controls."""

import shutil
import subprocess
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

STATIC = Path(__file__).resolve().parents[1] / "home_media_catalog" / "static"


def test_catalogue_shows_both_film_and_series_rating_ranges():
    html = (STATIC / "index.html").read_text()
    js = (STATIC / "app.js").read_text()
    for prefix in ("catalog-rating", "catalog-user-rating"):
        assert f'data-rating-range="{prefix}"' in html
        for edge in ("min", "max"):
            assert f'id="{prefix}-{edge}" type="range"' in html
            assert f'id="{prefix}-{edge}-value"' in html
            assert f'id="{prefix}-{edge}" type="number"' not in html
    assert "Note publique (IMDb / TMDB)" in html
    assert "Notes des films et séries" in html
    assert "bindRatingRange('catalog-rating'" in js
    assert "bindRatingRange('catalog-user-rating'" in js
    for key in ("rating_min", "rating_max", "user_rating_min", "user_rating_max"):
        assert f"'{key}'" in js
    assert "ratingRanges:{public:cataloguePublicRating.get()" in js
    assert "cataloguePublicRating.set(0,10)" in js
    assert "cataloguePersonalRating.set(0,10)" in js
    assert "if(min>0||max<10)" in js


def test_streaming_uses_same_dual_handle_ranges_and_iOS_preferences():
    html = (STATIC / "index.html").read_text()
    js = (STATIC / "streaming.js").read_text()
    for prefix in ("streaming-rating", "streaming-user-rating"):
        assert f'data-rating-range="{prefix}"' in html
        for edge in ("min", "max"):
            assert f'id="{prefix}-{edge}" type="range"' in html
    assert "bindRatingRange(prefix,({min,max})=>" in js
    assert "persistPreference(minKey,String(min))" in js
    assert "persistPreference(maxKey,String(max))" in js
    assert "streamingRatingRanges.public?.set(" in js
    assert "streamingRatingRanges.personal?.set(" in js


def test_dvd_uses_standard_vector_shapes_and_theme_color():
    root = ET.fromstring((STATIC / "format-logos.svg").read_text())
    symbol = root.find(".//{http://www.w3.org/2000/svg}symbol[@id='dvd']")
    assert symbol is not None
    path = symbol.find("{http://www.w3.org/2000/svg}path")
    assert path is not None
    assert len(path.attrib["d"]) > 1900
    assert path.attrib["fill"] == "currentColor"
    assert symbol.find("{http://www.w3.org/2000/svg}text") is None
    js = (STATIC / "app.js").read_text()
    assert "'DVD':['dvd','disc']" in js
    assert "'DVD':'format-dvd'" not in js
    css = (STATIC / "app.css").read_text()
    assert ".format-badge.format-dvd{color:var(--text)" in css


def test_filter_layout_has_aligned_groups_and_narrow_layouts():
    html = (STATIC / "index.html").read_text()
    css = (STATIC / "app.css").read_text()
    assert 'class="catalogue-filter-primary"' in html
    assert 'class="catalogue-filter-ratings"' in html
    assert 'class="catalogue-filter-secondary"' in html
    assert "grid-template-columns:repeat(5,minmax(0,1fr))" in css
    assert "grid-template-columns:repeat(2,minmax(0,1fr))" in css
    assert "grid-template-columns:minmax(0,1fr)" in css
    assert "grid-template-areas:\"watch discovery\" \"public personal\"" in (
        STATIC / "streaming.css"
    ).read_text()


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is unavailable")
def test_range_controller_clamps_crossing_and_commits_only_on_change():
    js = r"""
const assert=require('node:assert/strict'),fs=require('node:fs');
const content=fs.readFileSync(process.argv[1],'utf8').replace(/^export /gm,'');
const factory=new Function(content+'\nreturn bindRatingRange;');
const bind=factory();
const values={};
const styles={};
const root={style:{setProperty:(k,v)=>{styles[k]=v;}}};
function input(id,value){
 const handlers={},attrs={};
 const el={id,type:'range',value,handlers,attrs,style:{},
   closest:()=>root,addEventListener:(k,fn)=>{handlers[k]=fn;},
   setAttribute:(k,v)=>{attrs[k]=v;}};
 values[id]=el;return el;
}
const low=input('test-min','0'),high=input('test-max','10');
const minOut={textContent:''},maxOut={textContent:''};
const elems={'test-min':low,'test-max':high,
             'test-min-value':minOut,'test-max-value':maxOut};
global.document={getElementById:id=>elems[id]};
const commits=[];
const control=bind('test',r=>commits.push(r));
assert.deepEqual(control.get(),{min:0,max:10});
control.set(3.5,8);
assert.deepEqual(control.get(),{min:3.5,max:8});
assert.equal(styles['--range-low'],'35%');
assert.equal(styles['--range-high'],'80%');
low.value='9';
low.handlers.input();
assert.deepEqual(control.get(),{min:8,max:8});
assert.equal(commits.length,0);
low.handlers.change();
assert.deepEqual(commits,[{min:8,max:8}]);
high.value='9.5';
high.handlers.input();
high.handlers.change();
assert.deepEqual(control.get(),{min:8,max:9.5});
assert.equal(commits.length,2);
control.set(0,10);
assert.equal(commits.length,2);
"""
    result = subprocess.run(
        ["node", "-e", js, str(STATIC / "rating-range.js")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
