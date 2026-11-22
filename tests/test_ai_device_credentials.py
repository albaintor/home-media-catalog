"""Administrator-scoped, TLS-only AI API key exchange with iOS."""
import stat

import conftest
from fastapi.testclient import TestClient

from home_media_catalog.app import create_app
from home_media_catalog.settings import Settings

PATH = "/api/v1/ai/device-credentials"
ADMIN_AUTH = {"Authorization": f"Bearer {conftest.ADMIN}"}
READ_AUTH = {"Authorization": f"Bearer {conftest.READ}"}
PERSONAL_KEY = "sk-test-personal-credential-only"


def test_ai_device_credentials_require_https_admin_bearer(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, api_token=conftest.ADMIN, read_token=conftest.READ))
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
    app = create_app(Settings(data_dir=tmp_path, api_token=conftest.ADMIN, read_token=conftest.READ))
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
        data_dir=tmp_path, api_token=conftest.ADMIN, read_token=conftest.READ,
    )), base_url="https://testserver") as client:
        assert client.get(PATH, headers=ADMIN_AUTH).json()["api_key"] == PERSONAL_KEY


def test_active_ai_configuration_is_one_versioned_record(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, api_token=ADMIN, read_token=READ))
    with TestClient(app, base_url="https://testserver") as client:
        initial = client.get(PATH, headers=ADMIN_AUTH).json()
        assert initial["revision"] == 0
        new = {
            "mode": "replace", "provider": "gemini",
            "model": "gemini-2.5-flash", "api_key": PERSONAL_KEY,
            "expected_revision": initial["revision"],
        }
        updated = client.put(PATH, json=new, headers=ADMIN_AUTH)
        assert updated.status_code == 200
        assert updated.json()["revision"] == 1
        assert updated.json()["provider"] == "gemini"
        assert client.get(PATH, headers=ADMIN_AUTH).json()["model"] == new["model"]
        status = client.get("/api/v1/ai/status", headers=READ_AUTH).json()
        assert status["provider"] == "gemini"
        assert status["model"] == new["model"]
        assert status["revision"] == 1
        stale = client.put(PATH, json={**new, "model": "gemini-other"},
                           headers=ADMIN_AUTH)
        assert stale.status_code == 409
        assert client.get(PATH, headers=ADMIN_AUTH).json()["model"] == new["model"]
        assert client.put(PATH, json={**new, "expected_revision": 1,
                                     "api_key": "replaced-personal-key"},
                          headers=READ_AUTH).status_code == 403
        # The regular server settings edit also increments the SAME version.
        changed = client.put(
            "/api/v1/ai/settings", headers=ADMIN_AUTH,
            json={"provider": "gemini", "model": "gemini-2.5-pro"},
        )
        assert changed.status_code == 200
        assert changed.json()["revision"] == 2
        assert client.get(PATH, headers=ADMIN_AUTH).json()["model"] == "gemini-2.5-pro"
        assert client.put(PATH, json={**new, "expected_revision": 1},
                          headers=ADMIN_AUTH).status_code == 409


def test_ai_config_can_be_changed_from_device_without_losing_other_settings(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, api_token=ADMIN, read_token=READ))
    with TestClient(app, base_url="https://testserver") as client:
        assert client.put(
            "/api/v1/ai/settings", headers=ADMIN_AUTH,
            json={"provider": "openai", "model": "gpt-4o-mini",
                  "monthly_budget_usd": 7},
        ).status_code == 200
        before = client.get(PATH, headers=ADMIN_AUTH).json()
        updated = client.put(PATH, json={
            "mode": "replace", "provider": "openai", "model": "gpt-4o",
            "api_key": PERSONAL_KEY, "expected_revision": before["revision"],
        }, headers=ADMIN_AUTH)
        assert updated.status_code == 200
        status = client.get("/api/v1/ai/settings", headers=ADMIN_AUTH).json()
        assert status["monthly_budget_usd"] == 7
        assert status["model"] == "gpt-4o"
        # Clear the only active key, without switching to another hidden engine.
        assert client.put(PATH, json={
            "mode": "replace", "provider": "openai", "model": "gpt-4o",
            "api_key": "", "expected_revision": updated.json()["revision"],
        }, headers=ADMIN_AUTH).status_code == 200
        assert client.get(PATH, headers=ADMIN_AUTH).json()["api_key"] is None
        assert client.get("/api/v1/ai/status", headers=READ_AUTH).json()["configured"] is False
