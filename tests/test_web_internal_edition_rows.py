"""Internal edition summaries mirror the iOS detail view without merging sources."""
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "home_media_catalog" / "static"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js unavailable")
def test_internal_edition_summaries():
    script = r"""
const fs=require('node:fs'), assert=require('node:assert/strict');
const code=fs.readFileSync(process.argv[1],'utf8');
const helper=code.slice(code.indexOf('function editionStringValues('),
                       code.indexOf('function editionMarkup('));
const canonical=code.slice(code.indexOf('const canonicalFormat ='),
                          code.indexOf('function formatCategory('));
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const summary=Function('esc','tr','sourceIcon','editionProviderLogos',
  canonical+helper+'return internalEditionSummary;')(
    esc,v=>v,type=>'<icon>'+type+'</icon>',keys=>keys.length?'LOGOS:'+Array.from(new Set(keys)).join(','):''
  );
const item={streaming_services:['netflix','prime_video']};
const physical=summary({metadata:{formats:['4K Ultra HD Blu-ray','HDR10'],
  audio_streams:[{language:'French',codec:'DTS'},{language:'French'},{language:'English'}],
  subtitles:['French','English','French'],physical_season:2}},item);
assert.match(physical,/Blu-ray 4K/);
assert.match(physical,/French, English/);
assert.match(physical,/Saison.*2/);
assert.doesNotMatch(physical,/LOGOS|HDR10|DTS|Internal catalog|Catalogue interne/);
for(const format of ['Blu-ray 3D','Blu-ray','DVD']){
  const out=summary({metadata:{formats:[format]}},item);
  assert.ok(out.includes(format));assert.doesNotMatch(out,/LOGOS/);
}
const streaming=summary({metadata:{streaming_reference:true,streaming_services:['netflix']}},item);
assert.match(streaming,/LOGOS:netflix,prime_video/);
assert.doesNotMatch(streaming,/Édition physique|Sous-titres/);
assert.doesNotMatch(summary({metadata:{barcode:'123',formats:[]}},item),/LOGOS/);
assert.match(summary({metadata:{streaming_reference:true}},{}),/Streaming/);
assert.match(summary({metadata:{audio_tracks:['English DTS 5.1','French Dolby Digital'],subtitles:['<unsafe>']}},{}),/English, French/);
assert.match(summary({metadata:{subtitles:['<unsafe>']}},{}),/&lt;unsafe&gt;/);
"""
    result = subprocess.run(["node", "-e", script, str(WEB / "app.js")],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_provider_logo_integration():
    app = (WEB / "app.js").read_text()
    streaming = (WEB / "streaming.js").read_text()
    assert "item.editions.map(e=>editionMarkup(e,item))" in app
    assert "void ensureEditionProviderLogos();" in app
    assert "#detail-content [data-provider-logo]" in streaming
    assert "export function editionProviderLogos(keys)" in streaming
    assert "edition-source-languages" in (WEB / "app.css").read_text()
