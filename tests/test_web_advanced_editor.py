"""Web editor endpoints and the browser's advanced editing controls."""

import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import READ

from home_media_catalog.connectors.base import Record

STATIC = Path(__file__).resolve().parents[1] / "home_media_catalog" / "static"


def test_advanced_editor_controls_and_javascript_syntax():
    html = (STATIC / "index.html").read_text()
    script = (STATIC / "app.js").read_text()
    for name in ("media-edit-dialog", "edition-edit-dialog", "internal-poster-file",
                 "internal-original-title", "media-edit-studios", "media-edit-genres"):
        assert f'id="{name}"' in html
    for marker in ('data-quick-action="edit-media"', 'data-edit-disc=', 'data-edit-hdr=',
                   'data-convert-source=', '/convert-to-internal', 'remove_source:false',
                   'remove_source:true', '/metadata', 'poster_jpeg', 'override_hdr'):
        assert marker in script
    if not shutil.which("node"):
        pytest.skip("Node.js is not installed")
    subprocess.run(
        ["node", "--input-type=module", "--check"], input=script, text=True, check=True
    )


def test_web_editor_routes_permissions_and_conversion_preserve_work(client, admin):
    store = client.app.state.store
    source = store.save_source({"type": "clz", "name": "Web test collection"})
    store.apply_snapshot(source["id"], [
        Record("web-disc", "movie", "Imported title", year=2009,
               formats=["Blu-ray", "HDR10"], identifiers={"tmdb": "12345"})
    ])
    work_id = store.media()["items"][0]["id"]
    media_url = "/api/v1/media/" + work_id
    metadata_path = media_url + "/metadata"
    reader = {"Authorization": f"Bearer {READ}"}
    fields = {"custom_title": "Custom French title", "year": 2010,
              "original_title": "Original", "runtime_minutes": 114, "plot": "Synopsis",
              "genres": ["Animation"], "country": "Japan", "studios": ["Studio"]}
    assert client.patch(metadata_path, json=fields, headers=reader).status_code == 403
    changed = client.patch(metadata_path, json=fields, headers=admin)
    assert changed.status_code == 200, changed.text
    detail = changed.json()
    assert detail["title"] == fields["custom_title"]
    for key in ("year", "genres", "country", "studios", "runtime_minutes", "original_title"):
        assert detail[key] == fields[key]
    edition_id = detail["editions"][0]["id"]
    hdr_path = media_url + "/editions/" + edition_id + "/hdr"
    assert client.patch(hdr_path, json={"hdr_formats": []}, headers=reader).status_code == 403
    hdr = client.patch(hdr_path, json={"hdr_formats": ["Dolby Vision"]}, headers=admin)
    assert hdr.status_code == 200, hdr.text
    assert hdr.json()["editions"][0]["metadata"]["hdr_override"] == ["Dolby Vision"]
    formats_path = media_url + "/editions/" + edition_id + "/formats"
    assert client.patch(formats_path, json={"video_formats": ["4K"]}, headers=reader).status_code == 403
    assert client.patch(formats_path, json={"video_formats": ["Bogus"]}, headers=admin).status_code == 422
    technical = client.patch(formats_path, json={"video_formats": ["4K"], "audio_formats": ["DTS:X"]},
                             headers=admin)
    assert technical.status_code == 200, technical.text
    metadata = technical.json()["editions"][0]["metadata"]
    assert metadata["video_override"] == ["4K"] and metadata["audio_override"] == ["DTS:X"]
    assert {"4K", "DTS:X"} <= set(metadata["formats"])
    # An absent group is unchanged; null restores the detected formats.
    restored = client.patch(formats_path, json={"audio_formats": None}, headers=admin)
    assert restored.status_code == 200, restored.text
    metadata = restored.json()["editions"][0]["metadata"]
    assert metadata["video_override"] == ["4K"] and "audio_override" not in metadata
    assert "DTS:X" not in metadata["formats"]
    unknown = client.patch(media_url + "/editions/unknown/formats", json={"video_formats": None},
                           headers=admin)
    assert unknown.status_code == 404
    endpoint = "/api/v1/sources/" + source["id"] + "/convert-to-internal"
    assert client.post(endpoint, json={"remove_source": True}, headers=reader).status_code == 403
    copied = client.post(endpoint, json={"remove_source": False}, headers=admin)
    assert copied.status_code == 200, copied.text
    assert copied.json()["copied"] == 1
    assert store.source(source["id"]) is not None
    converted = client.post(endpoint, json={"remove_source": True}, headers=admin)
    assert converted.status_code == 200, converted.text
    assert store.source(source["id"]) is None
    detail = client.get(media_url, headers=admin).json()
    assert detail["title"] == fields["custom_title"]
    assert len(detail["editions"]) == 1
    assert detail["editions"][0]["source_type"] == "internal"
    assert detail["editions"][0]["metadata"]["hdr_override"] == ["Dolby Vision"]
    assert detail["editions"][0]["metadata"]["video_override"] == ["4K"]
    key = detail["editions"][0]["external_key"]
    disc = client.patch("/api/v1/internal/items/" + key,
                        json={"format": "DVD", "barcode": "036000291452"}, headers=admin)
    assert disc.status_code == 200, disc.text
    latest = client.get(media_url, headers=admin).json()
    assert "DVD" in latest["formats"]
    assert "Dolby Vision" in latest["formats"]
