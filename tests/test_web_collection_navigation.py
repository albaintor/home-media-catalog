"""Regression test for returning to the web collection grid without losing scroll."""

import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "home_media_catalog/static/app.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_collection_back_restores_scroll_without_reloading_media():
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const first = src.indexOf('let collectionReturnPosition=null;');
const last = src.indexOf('async function loadMedia(', first);
assert.ok(first !== -1 && last > first);

const state = {groupBy:'collection', openCollection:null, items:[{id:'movie'}]};
const scheduled = [];
const scrolls = [];
const renders = [];
const window = {
  scrollX:13,
  scrollY:1876,
  scrollTo(x,y) { scrolls.push([x,y]); this.scrollX=x; this.scrollY=y; }
};
const $ = id => {
  assert.equal(id,'results');
  return {scrollIntoView(){ window.scrollY=320; }};
};
const fn = Function('state','window','$','renderCards','requestAnimationFrame',
  src.slice(first,last)+'\nreturn {enterCollection,leaveCollection};');
const {enterCollection,leaveCollection} = fn(
  state,window,$,items=>renders.push([state.openCollection,items]),
  callback=>scheduled.push(callback)
);

enterCollection('the-saga');
assert.equal(state.openCollection,'the-saga');
assert.equal(window.scrollY,320);
assert.deepEqual(renders.map(row=>row[0]),['the-saga']);
leaveCollection();
assert.equal(state.openCollection,null);
assert.equal(scrolls.length,0);
assert.equal(scheduled.length,1);
scheduled.shift()();
assert.deepEqual(scrolls,[[13,1876]]);
assert.equal(window.scrollY,1876);
assert.deepEqual(renders.map(row=>row[0]),['the-saga',null]);
assert.strictEqual(renders[0][1],renders[1][1]);

// A later navigation supersedes a queued scroll restoration.
enterCollection('another-saga');
leaveCollection();
state.openCollection='third-saga';
scheduled.shift()();
assert.equal(scrolls.length,1);
assert.ok(src.includes('collectionReturnPosition=null;\n    ++state.seq;'));
"""
    subprocess.run(
        ["node", "-e", script, str(WEB)],
        check=True,
        capture_output=True,
        text=True,
    )
