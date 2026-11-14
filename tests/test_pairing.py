"""Pairing security, device-specific token persistence and revocation."""

import time

from conftest import ADMIN, READ
from fastapi.testclient import TestClient

from home_media_catalog import DATA_MODEL_VERSION, __version__
from home_media_catalog.app import create_app
from home_media_catalog.settings import Settings


def test_public_discovery_is_minimal_and_pairing_is_admin_only(client, admin):
    response = client.get("/api/v1/discovery")
    assert response.status_code == 200
    assert response.json() == {
        "service": "home-kodi-library",
        "api_version": "v1",
        "pairing": True,
        "server_version": __version__,
        "data_model_version": DATA_MODEL_VERSION,
        "mobile_compatibility": {"min_contract": 1, "max_contract": 4},
        "capabilities": [
            "ai_search",
            "bulk_list_add",
            "ai_refine_results",
            "kodi_local_stream",
            "playback_locations",
            "playback_discovery",
            "playback_cloud_oauth",
            "kodi_clean",
            "media_details_batch",
        ],
    }
    assert client.post("/api/v1/pairing/start", json={}).status_code == 401
    assert (
        client.post(
            "/api/v1/pairing/start",
            headers={"Authorization": f"Bearer {READ}"},
            json={"role": "admin"},
        ).status_code
        == 403
    )
    assert client.get("/api/v1/pairing/devices").status_code == 401
    assert (
        client.post("/api/v1/pairing/start", headers=admin, json={"role": "owner"}).status_code
        == 422
    )


def test_pairing_creates_individual_scoped_token_and_can_revoke(client, admin):
    started = client.post("/api/v1/pairing/start", headers=admin, json={"role": "read"})
    assert started.status_code == 200
    code = started.json()["code"]
    assert len(code) == 8 and code.isdigit()
    assert started.json()["expires_in"] == 300
    paired = client.post(
        "/api/v1/pairing/redeem", json={"code": code, "device_name": "Test iPhone"}
    )
    assert paired.status_code == 200
    device_token = paired.json()["token"]
    assert device_token != ADMIN
    headers = {"Authorization": f"Bearer {device_token}"}
    assert client.get("/api/v1/auth/session", headers=headers).json()["role"] == "read"
    assert client.get("/api/v1/media", headers=headers).status_code == 200
    assert client.post("/api/v1/pairing/start", headers=headers, json={}).status_code == 403
    assert (
        client.post(
            "/api/v1/pairing/redeem", json={"code": code, "device_name": "Replay"}
        ).status_code
        == 400
    )
    devices = client.get("/api/v1/pairing/devices", headers=admin).json()
    assert len(devices) == 1
    assert devices[0]["name"] == "Test iPhone"
    assert devices[0]["role"] == "read"
    assert "token" not in devices[0]
    assert len(devices[0]["token_hash"]) == 64
    revoked = client.delete("/api/v1/pairing/devices/" + devices[0]["token_hash"], headers=admin)
    assert revoked.status_code == 204
    assert client.get("/api/v1/media", headers=headers).status_code == 401


def test_pairing_expiry_and_five_guesses_cannot_brute_force(client, admin):
    code = client.post("/api/v1/pairing/start", headers=admin, json={}).json()["code"]
    for _ in range(5):
        assert (
            client.post(
                "/api/v1/pairing/redeem",
                json={
                    "code": "99999999" if code != "99999999" else "00000000",
                    "device_name": "Bad",
                },
            ).status_code
            == 400
        )
    assert client.post(
        "/api/v1/pairing/redeem", json={"code": code, "device_name": "Late"}
    ).status_code in (400, 429)

    # A new code cancels the previous one and has a fixed time-to-live.
    client.app.state.auth.pairing_attempts.clear()
    client.app.state.auth.pairing_global.clear()
    new_code = client.post("/api/v1/pairing/start", headers=admin, json={}).json()["code"]
    auth = client.app.state.auth
    digest, _, role = auth.pairing
    auth.pairing = (digest, time.monotonic() - 1, role)
    assert (
        client.post(
            "/api/v1/pairing/redeem", json={"code": new_code, "device_name": "Expired"}
        ).status_code
        == 400
    )


def test_device_auth_survives_restart(client, admin, tmp_path):
    # The token hash persists in SQLite rather than server memory.
    token = None
    started = client.post("/api/v1/pairing/start", headers=admin, json={"role": "admin"})
    paired = client.post(
        "/api/v1/pairing/redeem", json={"code": started.json()["code"], "device_name": "iPad"}
    )
    token = paired.json()["token"]
    data_dir = client.app.state.store.path.parent
    with TestClient(
        create_app(Settings(data_dir=data_dir, api_token=ADMIN, read_token=READ))
    ) as restarted:
        headers = {"Authorization": f"Bearer {token}"}
        assert restarted.get("/api/v1/auth/session", headers=headers).json()["role"] == "admin"
        assert restarted.get("/api/v1/status", headers=headers).status_code == 200


def test_pinned_https_qr_keeps_legacy_eight_digit_pairing(client, admin, tmp_path):
    """The secure QR is additive: historical HTTP redemption still works."""
    import base64

    from home_media_catalog.tls import ensure_certificate, fingerprint

    start = client.post("/api/v1/pairing/start", headers=admin, json={"role": "admin"})
    assert start.status_code == 200, start.text
    data = start.json()
    assert len(data["code"]) == 8 and data["code"].isdigit()
    assert data["https_url"].startswith("https://")
    assert len(data["fingerprint"]) == 64
    assert base64.b64decode(data["qr_png"]).startswith(b"\x89PNG\r\n\x1a\n")
    saved = fingerprint(tmp_path)
    assert saved == data["fingerprint"]
    cert, key = ensure_certificate(tmp_path)
    assert cert.is_file() and key.is_file()
    assert fingerprint(tmp_path) == saved

    # The existing eight-digit HTTP endpoint remains unchanged.
    paired = client.post(
        "/api/v1/pairing/redeem",
        json={"code": data["code"], "device_name": "Old HTTP iPhone"},
    )
    assert paired.status_code == 200, paired.text
    assert paired.json()["token"]
    assert (
        client.get(
            "/api/v1/auth/session",
            headers={"Authorization": "Bearer " + paired.json()["token"]},
        ).json()["role"] == "admin"
    )


def test_incomplete_tls_identity_never_silently_changes_pin(tmp_path):
    import pytest

    from home_media_catalog.tls import ensure_certificate

    certificate, private_key = ensure_certificate(tmp_path)
    original = certificate.read_bytes()
    private_key.unlink()
    with pytest.raises(RuntimeError, match="Incomplete TLS identity"):
        ensure_certificate(tmp_path)
    assert certificate.read_bytes() == original
