import stat

from conftest import ADMIN, READ
from fastapi.testclient import TestClient

from home_media_catalog.app import create_app
from home_media_catalog.settings import Settings

PATH = "/api/v1/catalogue/tmdb/device-credentials"
ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN}"}
READ_HEADERS = {"Authorization": f"Bearer {READ}"}
TEST_TOKEN = "test-device-credential-not-a-real-secret"


def test_tmdb_device_credentials_require_https_bearer(tmp_path):
    settings = Settings(data_dir=tmp_path, api_token=ADMIN, read_token=READ,
                        tmdb_api_token=TEST_TOKEN)
    app = create_app(settings)
    with TestClient(app, base_url="http://testserver") as client:
        assert client.get(PATH, headers=ADMIN_HEADERS).status_code == 403
        assert client.get(PATH, headers={
            **ADMIN_HEADERS, "X-Forwarded-Proto": "https"
        }).status_code == 403
        assert client.put(PATH, headers=ADMIN_HEADERS, json={"token": "other"}).status_code == 403
    with TestClient(app, base_url="https://testserver") as client:
        assert client.get(PATH).status_code == 401
        assert client.get(PATH, headers=READ_HEADERS).status_code == 403
        assert client.put(PATH, headers=READ_HEADERS, json={"token": "other"}).status_code == 403
        result = client.get(PATH, headers=ADMIN_HEADERS)
        assert result.json() == {"token": TEST_TOKEN}
        assert "no-store" in result.headers["cache-control"]
        assert client.put(PATH, headers=ADMIN_HEADERS, json={"token": "other"}).json() == {
            "configured": True, "imported": False
        }
        assert client.get(PATH, headers=ADMIN_HEADERS).json() == {"token": TEST_TOKEN}
        assert TEST_TOKEN not in client.get("/api/v1/discovery").text


def test_device_provisioning_persists_private_token(tmp_path, monkeypatch):
    async def valid_probe(token):
        assert token == TEST_TOKEN

    monkeypatch.setattr("home_media_catalog.app.probe_tmdb", valid_probe)
    settings = Settings(data_dir=tmp_path, api_token=ADMIN, read_token=READ)
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        assert client.get(PATH, headers=ADMIN_HEADERS).json() == {"token": None}
        result = client.put(PATH, headers=ADMIN_HEADERS, json={"token": TEST_TOKEN})
        assert result.status_code == 200
        assert result.json() == {"configured": True, "imported": True}
        assert "no-store" in result.headers["cache-control"]
        assert client.get(PATH, headers=ADMIN_HEADERS).json() == {"token": TEST_TOKEN}
    path = tmp_path / "tmdb-device-token"
    assert path.read_text() == TEST_TOKEN
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    restarted = Settings(data_dir=tmp_path, api_token=ADMIN, read_token=READ)
    create_app(restarted)
    assert restarted.tmdb_api_token == TEST_TOKEN
    explicit = Settings(data_dir=tmp_path, api_token=ADMIN, read_token=READ,
                        tmdb_api_token="explicit-environment-token")
    create_app(explicit)
    assert explicit.tmdb_api_token == "explicit-environment-token"
