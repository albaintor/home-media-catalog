"""Protocol configurations and OAuth credentials must remain private."""

from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import ADMIN, READ

from home_media_catalog.playback_cloud import CloudOAuth, CloudOAuthError
from home_media_catalog.settings import Settings


def test_network_fields_persist_and_password_is_redacted(client, admin):
    path = "/api/v1/playback/locations"
    password = "the-secret-password"
    data = {
        "name": "JEDI",
        "kind": "smb",
        "target": "smb://jedi:445/Series",
        "host": "jedi",
        "port": 445,
        "share": "Series",
        "username": "damien",
        "workgroup": "WORKGROUP",
        "password": password,
    }
    response = client.post(path, headers=admin, json=data)
    assert response.status_code == 201, response.text
    location_id = response.json()["id"]
    assert response.json()["has_password"] is True
    assert "password" not in response.json()
    assert password not in response.text
    assert response.json()["host"] == "jedi"
    assert response.json()["port"] == 445
    assert response.json()["workgroup"] == "WORKGROUP"

    connection = client.app.state.store.connect()
    with connection as db:
        row = db.execute(
            "SELECT password_cipher FROM playback_location_options WHERE location_id=?",
            (location_id,),
        ).fetchone()
        encrypted = row["password_cipher"]
    assert password not in encrypted
    assert encrypted

    # No password in response; PUT without password preserves stored credential.
    data.pop("password")
    data["name"] = "JEDI edited"
    updated = client.put(f"{path}/{location_id}", headers=admin, json=data)
    assert updated.status_code == 200, updated.text
    assert updated.json()["has_password"] is True
    read = {"Authorization": f"Bearer {READ}"}
    snapshot = client.get(path, headers=read)
    assert snapshot.status_code == 200
    assert password not in snapshot.text
    assert "password_cipher" not in snapshot.text
    assert client.get("/api/v1/status", headers=read).status_code == 200
    assert password not in client.get("/api/v1/status", headers=read).text


def test_cloud_not_configured_and_icloud_uses_files(client, admin):
    path = "/api/v1/playback/locations"
    cloud = client.post(
        path, headers=admin, json={"name": "Dropbox", "kind": "dropbox", "target": ""}
    ).json()
    state = client.get("/api/v1/playback/cloud/status", headers=admin).json()
    assert state[0]["id"] == cloud["id"]
    assert state[0]["configured"] is False and state[0]["connected"] is False
    # Readiness is available BEFORE a location is added and never leaks IDs
    # or OAuth secrets. The browser can avoid opening a useless blank popup.
    readiness = client.get("/api/v1/playback/cloud/providers", headers=admin)
    assert readiness.status_code == 200
    dropbox = next(p for p in readiness.json() if p["provider"] == "dropbox")
    assert dropbox["configured"] is False
    assert "application OAuth" in dropbox["reason"]
    assert "client_secret" not in readiness.text
    result = client.post(f"/api/v1/playback/cloud/{cloud['id']}/authorize", headers=admin)
    assert result.status_code == 422
    assert client.post("/api/v1/playback/cloud/bad/authorize", headers=admin).status_code == 404
    icloud = client.post(
        path, headers=admin, json={"name": "iCloud", "kind": "icloud", "target": ""}
    ).json()
    assert (
        client.post(f"/api/v1/playback/cloud/{icloud['id']}/authorize", headers=admin).status_code
        == 422
    )


def test_oauth_pkce_and_single_use_callback(tmp_path):
    class FakeStore:
        def __init__(self):
            self.saved = []

        def playback_location(self, location_id):
            return {"id": location_id, "kind": "dropbox"}

        def playback_oauth_app(self, provider):
            return None

        def set_playback_cloud_token(self, location_id, provider, cipher):
            self.saved.append((location_id, provider, cipher))

    settings = Settings(
        data_dir=tmp_path,
        api_token=ADMIN,
        cloud_oauth_redirect_url="https://example.org/api/v1/playback/cloud/callback",
        cloud_oauth_clients={"dropbox": {"client_id": "test-client"}},
    )
    settings.validate()
    store = FakeStore()
    manager = CloudOAuth(settings, store)
    url = manager.start("a", "dropbox", return_to="ios")
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    state = query["state"][0]
    assert query["client_id"] == ["test-client"]
    assert query["token_access_type"] == ["offline"]
    assert query["force_reauthentication"] == ["true"]
    assert query["scope"] == ["files.metadata.read files.content.read"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"][0]
    assert "access_token" not in url

    class FakeReply:
        def raise_for_status(self):
            pass

        def json(self):
            return {"access_token": "private-cloud-token", "refresh_token": "refresh-private"}

    class FakeHttp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, url, data):
            assert url == "https://api.dropboxapi.com/oauth2/token"
            assert data["code_verifier"]
            assert data["code"] == "one-time-code"
            return FakeReply()

    import asyncio

    with patch("home_media_catalog.playback_cloud.httpx.AsyncClient", return_value=FakeHttp()):
        result = asyncio.run(manager.complete(state=state, code="one-time-code", error=""))
    assert result == "ios"
    assert len(store.saved) == 1
    assert "private-cloud-token" not in store.saved[0][2]
    assert "refresh-private" not in store.saved[0][2]
    assert (
        manager.fernet.decrypt(store.saved[0][2].encode()).decode().find("private-cloud-token") >= 0
    )

    with pytest.raises(CloudOAuthError):
        asyncio.run(manager.complete(state=state, code="one-time-code", error=""))


def test_cloud_oauth_configuration_diagnostics(tmp_path):
    class FakeStore:
        def playback_oauth_app(self, provider):
            return None

    settings = Settings(
        data_dir=tmp_path,
        api_token=ADMIN,
        cloud_oauth_clients={"onedrive": {"client_id": "1234"}},
        cloud_oauth_redirect_url="",
    )
    manager = CloudOAuth(settings, FakeStore())
    assert "secret" in manager.configuration_error("onedrive")
    manager.clients["onedrive"]["client_secret"] = "server-only-secret"
    assert "HTTPS" in manager.configuration_error("onedrive")
    manager.redirect = "https://example.org/api/v1/playback/cloud/callback"
    assert manager.configuration_error("onedrive") is None
    assert manager.configured("onedrive")
    with pytest.raises(CloudOAuthError):
        manager.start("id", "unknown")


def test_cloud_token_auto_refresh(tmp_path):
    import asyncio
    import json

    class FakeStore:
        def __init__(self):
            self.cipher = None

        def playback_oauth_app(self, provider):
            return None

        def playback_cloud_credentials(self, location_id):
            return {"provider": "onedrive", "token_cipher": self.cipher}

        def set_playback_cloud_token(self, location_id, provider, cipher):
            assert location_id == "onedrive-1"
            assert provider == "onedrive"
            self.cipher = cipher

    settings = Settings(
        data_dir=tmp_path,
        api_token=ADMIN,
        cloud_oauth_redirect_url="https://example.org/api/v1/playback/cloud/callback",
        cloud_oauth_clients={
            "onedrive": {"client_id": "test-client", "client_secret": "server-secret"}
        },
    )
    store = FakeStore()
    oauth = CloudOAuth(settings, store)
    store.cipher = oauth.encrypt_password(
        json.dumps(
            {"access_token": "expired", "refresh_token": "rotating-refresh", "expires_at": 1}
        )
    )

    class FakeReply:
        def raise_for_status(self):
            pass

        def json(self):
            return {"access_token": "new-token", "expires_in": 3600}

    class FakeHttp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, url, data):
            assert url.endswith("/token")
            assert data["refresh_token"] == "rotating-refresh"
            assert data["client_secret"] == "server-secret"
            return FakeReply()

    with patch("home_media_catalog.playback_cloud.httpx.AsyncClient", return_value=FakeHttp()):
        credential = asyncio.run(oauth.credentials("onedrive-1", "onedrive"))
    assert credential["access_token"] == "new-token"
    assert credential["refresh_token"] == "rotating-refresh"
    assert "new-token" not in store.cipher
    assert json.loads(oauth.decrypt_secret(store.cipher))["access_token"] == "new-token"


def test_onedrive_device_configured_without_domain(tmp_path):
    from home_media_catalog.playback_cloud import ONEDRIVE_PUBLIC_CLIENT_ID

    class Store:
        def playback_oauth_app(self, provider):
            return None

    manager = CloudOAuth(Settings(data_dir=tmp_path, api_token=ADMIN), Store())
    assert manager.device_configuration_error("onedrive") is None
    assert manager.device_client_id("onedrive") == ONEDRIVE_PUBLIC_CLIENT_ID
    assert not manager.configured("onedrive")
    assert manager.device_configuration_error("dropbox")


def test_onedrive_device_code_start_pending_and_success(tmp_path):
    import asyncio
    import json
    from unittest.mock import patch

    from home_media_catalog.playback_cloud import (
        MICROSOFT_DEVICE_ENDPOINT,
        MICROSOFT_DEVICE_VERIFICATION_URL,
        MICROSOFT_TOKEN_ENDPOINT,
    )

    class Store:
        cipher = None

        def playback_oauth_app(self, provider):
            return None

        def playback_location(self, location_id):
            return {"id": location_id, "kind": "onedrive"}

        def set_playback_cloud_token(self, location_id, provider, cipher):
            assert location_id == "test-location"
            assert provider == "onedrive"
            self.cipher = cipher

        def playback_cloud_credentials(self, location_id):
            return {"provider": "onedrive", "token_cipher": self.cipher}

    class Reply:
        def __init__(self, status, payload):
            self.status_code = status
            self.payload = payload

        def json(self):
            return self.payload

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError("bad status")

    class Http:
        attempts = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, url, data):
            if url == MICROSOFT_DEVICE_ENDPOINT:
                assert data["scope"] == "offline_access Files.Read"
                return Reply(
                    200,
                    {
                        "device_code": "a-private-code",
                        "user_code": "QWER-1234",
                        # Microsoft may return a different verification domain.
                        # No provider-supplied URL may become a user-facing link.
                        "verification_uri": "https://aka.ms/devicelogin",
                        "expires_in": 900,
                        "interval": 5,
                    },
                )
            assert url == MICROSOFT_TOKEN_ENDPOINT
            assert data["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
            self.attempts += 1
            if self.attempts == 1:
                return Reply(400, {"error": "authorization_pending"})
            return Reply(
                200,
                {
                    "access_token": "secret-access",
                    "refresh_token": "secret-refresh",
                    "expires_in": 3600,
                },
            )

    store = Store()
    oauth = CloudOAuth(Settings(data_dir=tmp_path, api_token=ADMIN), store)
    transport = Http()

    async def scenario():
        with patch("home_media_catalog.playback_cloud.httpx.AsyncClient", return_value=transport):
            started = await oauth.start_device("test-location", "onedrive")
            assert started["user_code"] == "QWER-1234"
            assert started["verification_uri"] == MICROSOFT_DEVICE_VERIFICATION_URL
            assert "aka.ms" not in started["verification_uri"]
            assert "device_code" not in started
            assert "access_token" not in json.dumps(started)
            request_id = started["request_id"]
            assert (await oauth.poll_device("test-location", request_id))["status"] == "pending"
            assert transport.attempts == 0  # obey 5s Microsoft interval
            oauth.pending_devices[request_id]["next_poll"] = 0
            assert (await oauth.poll_device("test-location", request_id))["status"] == "pending"
            assert transport.attempts == 1
            oauth.pending_devices[request_id]["next_poll"] = 0
            assert await oauth.poll_device("test-location", request_id) == {"status": "connected"}
            assert (await oauth.poll_device("test-location", request_id))["status"] == "expired"

    asyncio.run(scenario())
    assert "secret-access" not in store.cipher
    token = json.loads(oauth.decrypt_secret(store.cipher))
    assert token["access_token"] == "secret-access"
    assert token["_hkl_client_id"] == oauth.device_client_id("onedrive")


def test_onedrive_public_client_api_without_redirect(client, admin):
    from home_media_catalog.playback_cloud import ONEDRIVE_PUBLIC_CLIENT_ID

    saved = client.put(
        "/api/v1/playback/cloud/apps/onedrive",
        headers=admin,
        json={"client_id": ONEDRIVE_PUBLIC_CLIENT_ID, "redirect_url": ""},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["device_configured"] is True
    providers = client.get("/api/v1/playback/cloud/providers", headers=admin)
    assert providers.status_code == 200
    one = next(row for row in providers.json() if row["provider"] == "onedrive")
    assert one["device_supported"] is True
    assert one["device_configured"] is True
    assert one["configured"] is False
    assert one["device_client_id"] == ONEDRIVE_PUBLIC_CLIENT_ID


def test_onedrive_device_routes_do_not_expose_microsoft_secrets(client, admin, monkeypatch):
    from home_media_catalog.playback_cloud import CloudOAuth

    cloud = client.post(
        "/api/v1/playback/locations",
        headers=admin,
        json={"name": "OneDrive", "kind": "onedrive", "target": ""},
    ).json()

    async def mock_start(self, location_id, provider):
        assert location_id == cloud["id"] and provider == "onedrive"
        return {
            "request_id": "x" * 32,
            "user_code": "BCDF-HJKL",
            "verification_uri": "https://microsoft.com/devicelogin",
            "expires_in": 900,
            "interval": 5,
        }

    async def mock_poll(self, location_id, request_id):
        assert location_id == cloud["id"] and request_id == "x" * 32
        return {"status": "pending", "retry_after": 5}

    monkeypatch.setattr(CloudOAuth, "start_device", mock_start)
    monkeypatch.setattr(CloudOAuth, "poll_device", mock_poll)
    start = client.post(f"/api/v1/playback/cloud/{cloud['id']}/device/start", headers=admin)
    assert start.status_code == 200
    assert "user_code" in start.json()
    assert "device_code" not in start.text
    assert "access_token" not in start.text
    polling = client.post(
        f"/api/v1/playback/cloud/{cloud['id']}/device/poll",
        headers=admin,
        json={"request_id": "x" * 32},
    )
    assert polling.status_code == 200
    assert polling.json() == {"status": "pending", "retry_after": 5}
    invalid = client.post(
        f"/api/v1/playback/cloud/{cloud['id']}/device/poll",
        headers=admin,
        json={"request_id": "../bad"},
    )
    assert invalid.status_code == 422


def test_cloud_account_email_uses_location_identity_and_never_exposes_tokens(tmp_path):
    import asyncio

    class Store:
        def playback_oauth_app(self, _provider):
            return None

    oauth = CloudOAuth(Settings(data_dir=tmp_path, api_token=ADMIN), Store())

    async def credentials(location_id, provider):
        assert provider == "onedrive"
        return {"access_token": f"private-token-{location_id}"}

    oauth.credentials = credentials

    class Reply:
        status_code = 200

        def __init__(self, token):
            self.token = token

        def json(self):
            return {"owner": {"user": {"email": f"{self.token}@example.net"}}}

    class HTTP:
        calls = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def get(self, url, headers):
            assert url == "https://graph.microsoft.com/v1.0/me/drive"
            token = headers["Authorization"].removeprefix("Bearer private-token-")
            self.calls.append(token)
            return Reply(token)

    http = HTTP()

    async def check():
        with patch("home_media_catalog.playback_cloud.httpx.AsyncClient", return_value=http):
            assert await oauth.account_email("one", "onedrive") == "one@example.net"
            assert await oauth.account_email("two", "onedrive") == "two@example.net"
            assert await oauth.account_email("one", "onedrive") == "one@example.net"
        assert http.calls == ["one", "two"]
        # The profile cache stores only display emails, never OAuth tokens.
        assert all("private-token" not in str(value) for value in oauth._account_labels.values())

    asyncio.run(check())


def test_multiple_accounts_per_google_drive_and_dropbox_are_independent(client, admin, monkeypatch):
    """Each cloud instance has its own location UUID and OAuth credential."""
    from home_media_catalog.playback_cloud import CloudOAuth

    async def label(_self, location_id, provider):
        return f"{location_id[:8]}@{provider}.example"

    monkeypatch.setattr(CloudOAuth, "account_email", label)
    store = client.app.state.store
    accounts = {}
    for provider in ("google_drive", "dropbox"):
        for number in (1, 2):
            response = client.post(
                "/api/v1/playback/locations",
                headers=admin,
                json={"name": f"{provider} {number}", "kind": provider, "target": ""},
            )
            assert response.status_code == 201
            location_id = response.json()["id"]
            accounts.setdefault(provider, []).append(location_id)
            store.set_playback_cloud_token(
                location_id, provider, "encrypted-test-credential-" + location_id
            )

    response = client.get("/api/v1/playback/cloud/status", headers=admin)
    assert response.status_code == 200
    connected = {item["id"]: item for item in response.json() if item["connected"]}
    for provider, location_ids in accounts.items():
        assert len(set(location_ids)) == 2
        for location_id in location_ids:
            assert connected[location_id]["provider"] == provider
            assert connected[location_id]["account_email"].startswith(location_id[:8])
            assert store.playback_cloud_credentials(location_id)["provider"] == provider

    # Disconnect exactly one Google Drive, preserving the other three tokens.
    first_google, second_google = accounts["google_drive"]
    response = client.delete(f"/api/v1/playback/cloud/{first_google}", headers=admin)
    assert response.status_code == 204
    assert store.playback_cloud_credentials(first_google) is None
    assert store.playback_cloud_credentials(second_google) is not None
    for location_id in accounts["dropbox"]:
        assert store.playback_cloud_credentials(location_id) is not None


def test_web_settings_can_add_and_reconnect_onedrive_instances():
    from pathlib import Path

    script = (Path(__file__).resolve().parents[1] / "home_media_catalog/static/app.js").read_text()
    assert "const accounts=locations.filter(row=>row.kind===kind)" in script
    assert "data-cloud-place-id" in script
    assert "data-web-cloud-disconnect" in script
    assert "Ajouter un compte " in script
    assert "locations.find(row=>row.kind===kind)" not in script


def test_web_only_offers_onedrive_cloud_provider():
    """Unsupported cloud services must not appear in settings or file browsers."""
    from pathlib import Path

    script = (Path(__file__).resolve().parents[1] / "home_media_catalog/static/app.js").read_text()
    assert "const playbackCloudProviders=[['onedrive','OneDrive']];" in script
    assert "const browsableLocationKinds=new Set(['smb','nfs','ftp','sftp','onedrive']);" in script
    assert "locations.filter(l=>browsableLocationKinds.has(l.kind))" in script
    assert "browser.locations.filter(loc=>browsableLocationKinds.has(loc.kind))" in script
    assert "networkExplorer.locations=(await api('playback/locations')).filter(" in script


def test_one_drive_public_grant_is_available_to_paired_admin(client, admin):
    """Transfer public-client grants, never confidential OAuth app secrets."""
    import json

    endpoint = "/api/v1/playback/locations"
    place = client.post(
        endpoint, headers=admin,
        json={"name": "OneDrive Mobile", "kind": "onedrive", "target": ""},
    )
    assert place.status_code == 201, place.text
    location_id = place.json()["id"]
    route = f"/api/v1/playback/cloud/{location_id}/device-credentials"
    assert client.get(route, headers=admin).status_code == 409
    oauth = client.app.state.playback_oauth
    store = client.app.state.store

    def save(token):
        store.set_playback_cloud_token(
            location_id, "onedrive", oauth.encrypt_password(json.dumps(token))
        )

    save({"access_token": "web-access", "refresh_token": "web-refresh",
          "expires_at": 12345.0})
    assert client.get(route, headers=admin).status_code == 409

    save({
        "_hkl_client_id": "145e06bb-b35b-4d96-8cf8-4077cd5e5528",
        "access_token": "public-access", "refresh_token": "public-refresh",
        "expires_at": 9876543210.0,
    })
    viewer = {"Authorization": f"Bearer {READ}"}
    assert client.get(route).status_code == 401
    assert client.get(route, headers=viewer).status_code == 403
    result = client.get(route, headers=admin)
    assert result.status_code == 200, result.text
    assert "no-store" in result.headers["cache-control"]
    assert result.json()["refresh_token"] == "public-refresh"
    assert result.json()["client_id"] == "145e06bb-b35b-4d96-8cf8-4077cd5e5528"
    assert "public-refresh" not in client.get(
        "/api/v1/playback/locations", headers=admin
    ).text


def test_import_public_onedrive_grant_from_iphone(client, admin):
    """The server stores iOS credentials encrypted and shares them back with admin only."""
    import json
    import time

    place = client.post(
        "/api/v1/playback/locations", headers=admin,
        json={"name": "Mobile OneDrive", "kind": "onedrive", "target": ""},
    )
    assert place.status_code == 201, place.text
    loc_id = place.json()["id"]
    route = f"/api/v1/playback/cloud/{loc_id}/device-credentials"
    future = time.time() + 3600
    grant = {
        "client_id": "145e06bb-b35b-4d96-8cf8-4077cd5e5528",
        "access_token": "ios-access-secret",
        "refresh_token": "ios-refresh-secret",
        "expires_at": future,
    }
    viewer = {"Authorization": f"Bearer {READ}"}
    assert client.put(route, json=grant).status_code == 401
    assert client.put(route, headers=viewer, json=grant).status_code == 403
    assert client.put(route, headers=admin, json={
        **grant, "client_id": "not-a-uuid"
    }).status_code == 422
    assert client.put(route, headers=admin, json={
        **grant, "refresh_token": ""
    }).status_code == 422

    uploaded = client.put(route, headers=admin, json=grant)
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json() == {"id": loc_id, "imported": True}
    assert uploaded.headers["cache-control"] == "no-store"
    stored = client.app.state.store.playback_cloud_credentials(loc_id)
    assert stored and stored["provider"] == "onedrive"
    assert "ios-refresh-secret" not in stored["token_cipher"]
    assert "ios-access-secret" not in stored["token_cipher"]

    read = client.get(route, headers=admin)
    assert read.status_code == 200
    assert read.json()["refresh_token"] == "ios-refresh-secret"
    assert read.json()["client_id"] == grant["client_id"]
    assert "ios-refresh-secret" not in client.get(
        "/api/v1/playback/locations", headers=admin
    ).text

    # An older iPhone grant cannot undo a newer server refresh.
    outdated = {**grant, "refresh_token": "stale", "expires_at": future - 100}
    replay = client.put(route, headers=admin, json=outdated)
    assert replay.status_code == 200
    assert replay.json()["imported"] is False
    assert client.get(route, headers=admin).json()["refresh_token"] == "ios-refresh-secret"

    other = client.post(
        "/api/v1/playback/locations", headers=admin,
        json={"name": "Other", "kind": "smb", "target": "smb://example/share"},
    ).json()
    assert client.put(
        f"/api/v1/playback/cloud/{other['id']}/device-credentials",
        headers=admin, json=grant,
    ).status_code == 404
