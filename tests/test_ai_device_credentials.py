"""Administrator-scoped, TLS-only AI API key exchange with iOS."""
import stat

from fastapi.testclient import TestClient

from conftest import ADMIN, READ
from home_media_catalog.app import create_app
from home_media_catalog.settings import Settings

PATH = "/api/v1/ai/device-credentials"
ADMIN_AUTH = {"Authorization": f"Bearer {ADMIN}"}
READ_AUTH = {"Authorization": f"Bearer {READ}"}
PERSONAL_KEY = "sk-test-personal-credential-only"


def test_ai_device_credentials_require_https_admin_bearer(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, api_token=ADMIN, read_token=READ))
    with TestClient(app, base_url="http://testserver") as client:
        assert client.get(PATH, headers=ADMIN_AUTH).status_code == 403
        assert client.put(PATH, headers=ADMIN_AUTH, json={
            "provider": "openai", "model": "gpt-4o-mini",
            "api_key": PERSONAL_KEY,
        }).status_code == 403
        assert client.get(PATH, headers={
            **ADMIN_AUTH, "X-Forwarded-Proto": "https",
        }).status_code == 403
    with TestClient(app, base_url="https://testserver") as client:
        assert client.get(PATH).status_code == 401
        assert client.get(PATH, headers=READ_AUTH).status_code == 403
        assert client.put(PATH, headers=READ_AUTH, json={
            "provider": "openai", "model": "gpt-4o-mini", "api_key": PERSONAL_KEY,
        }).status_code == 403
        response = client.get(PATH, headers=ADMIN_AUTH)
        assert response.status_code == 200
        assert response.json()["api_key"] is None
        assert "no-store" in response.headers["cache-control"]


def test_ai_key_provisioning_is_fill_only_and_private(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, api_token=ADMIN, read_token=READ))
    with TestClient(app, base_url="https://testserver") as client:
        value = {"provider": "anthropic", "model": "claude-haiku-5-5",
                 "api_key": PERSONAL_KEY}
        imported = client.put(PATH, headers=ADMIN_AUTH, json=value)
        assert imported.status_code == 200
        assert imported.json()["imported"] is True
        repeated = client.put(PATH, headers=ADMIN_AUTH,
                              json={**value, "api_key": "new-secret-ignored"})
        assert repeated.status_code == 200
        assert repeated.json()["imported"] is False
        secret = client.get(PATH, headers=ADMIN_AUTH)
        assert secret.json() == value
        assert "no-store" in secret.headers["cache-control"]
        assert PERSONAL_KEY not in client.get("/api/v1/discovery").text
        assert PERSONAL_KEY not in client.get("/api/v1/ai/status",
                                               headers=READ_AUTH).text
        assert client.put(PATH, headers=ADMIN_AUTH, json={
            **value, "provider": "unknown-provider",
        }).status_code == 422
    config = tmp_path / "ai-settings.json"
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    with TestClient(create_app(Settings(
        data_dir=tmp_path, api_token=ADMIN, read_token=READ,
    )), base_url="https://testserver") as client:
        assert client.get(PATH, headers=ADMIN_AUTH).json()["api_key"] == PERSONAL_KEY
