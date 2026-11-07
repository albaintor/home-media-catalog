"""Static contracts for the internal catalog source filter and logo."""
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "home_media_catalog/static/app.js"
LOGO = ROOT / "home_media_catalog/static/internal-catalog-logo.svg"


def test_internal_catalog_is_filterable_but_not_an_xml_import_source():
    js = JS.read_text(encoding="utf-8")
    assert "['internal','Catalogue interne']" in js
    assert "name==='source_type'?sourceIcon(value):''" in js
    assert "const collectionTypes=['clz','mymovies','filmotech'];" in js


def test_internal_source_logo_is_a_valid_svg_and_used_in_badges():
    js = JS.read_text(encoding="utf-8")
    assert "static/internal-catalog-logo.svg" in js
    assert "if(type==='internal') return" in js
    assert "sourceIcon(type)" in js
    logo = ElementTree.parse(LOGO).getroot()
    assert logo.tag == "{http://www.w3.org/2000/svg}svg"
    assert logo.attrib.get("viewBox") == "0 0 24 24"
    assert any(child.tag.endswith("circle") for child in logo)
