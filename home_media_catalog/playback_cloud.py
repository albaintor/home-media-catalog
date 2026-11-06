"""Server-side cloud OAuth registration for playback locations.

OAuth secrets and access/refresh tokens never enter the iOS configuration cache.
This component registers accounts; cloud directory browsing is a separate layer.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import time
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken

LOG = logging.getLogger(__name__)

# A client ID is public (unlike a client secret). The registered Home Kodi
# Library OneDrive application can therefore be used on LAN-only servers.
# Override with ONEDRIVE_CLIENT_ID for a different installation/registration.
ONEDRIVE_PUBLIC_CLIENT_ID = "145e06bb-b35b-4d96-8cf8-4077cd5e5528"
MICROSOFT_DEVICE_ENDPOINT = "https://login.microsoftonline.com/common/oauth2/v2.0/devicecode"
MICROSOFT_TOKEN_ENDPOINT = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
MICROSOFT_DEVICE_SCOPE = "offline_access Files.Read"
# Microsoft documents this canonical device-code verification page for the
# /common public-client flow. Do not send clients an arbitrary provider URL.
MICROSOFT_DEVICE_VERIFICATION_URL = "https://microsoft.com/devicelogin"


class CloudOAuthError(Exception):
    pass


PROVIDERS = {
    "onedrive": (
        "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "offline_access Files.Read",
    ),
    "google_drive": (
        "https://accounts.google.com/o/oauth2/v2/auth",
        "https://oauth2.googleapis.com/token",
        "https://www.googleapis.com/auth/drive.readonly",
    ),
    "dropbox": (
        "https://www.dropbox.com/oauth2/authorize",
        "https://api.dropboxapi.com/oauth2/token",
        "files.metadata.read files.content.read",
    ),
    "box": (
        "https://account.box.com/api/oauth2/authorize",
        "https://api.box.com/oauth2/token",
        "",
    ),
    "pcloud": (
        "https://my.pcloud.com/oauth2/authorize",
        "https://api.pcloud.com/oauth2_token",
        "",
    ),
}


class CloudOAuth:
    supported = frozenset(PROVIDERS)

    def __init__(self, settings, store):
        self.store = store
        self.clients = settings.cloud_oauth_clients
        self.redirect = settings.cloud_oauth_redirect_url
        # Dedicated key is preferred. The API token fallback preserves existing
        # installations; changing API_TOKEN then requires reconnecting accounts.
        key = settings.playback_encryption_key
        if key:
            try:
                self.fernet = Fernet(key.encode("ascii"))
            except (ValueError, TypeError) as exc:
                raise ValueError("PLAYBACK_ENCRYPTION_KEY must be a Fernet key.") from exc
        else:
            derived = base64.urlsafe_b64encode(
                hashlib.sha256(settings.api_token.encode("utf-8")).digest()
            )
            self.fernet = Fernet(derived)
        self.pending = {}
        self.pending_devices: dict[str, dict] = {}
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        # Only non-secret display information is cached here. Account identity
        # is keyed by location, not provider, to distinguish multiple OneDrives.
        self._account_labels: dict[str, tuple[float, str | None]] = {}

    def location_grant_lock(self, location_id: str) -> asyncio.Lock:
        """Serialize server refreshes and imported device grants per location."""
        return self._refresh_locks.setdefault(location_id, asyncio.Lock())

    def encrypt_password(self, value: str) -> str:
        return self.fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt_secret(self, value: str) -> str:
        return self.fernet.decrypt(value.encode("ascii")).decode("utf-8")

    def provider_options(self, provider: str) -> tuple[dict, str]:
        saved = self.store.playback_oauth_app(provider)
        if saved:
            secret = None
            if saved.get("secret_cipher"):
                try:
                    secret = self.fernet.decrypt(saved["secret_cipher"].encode("ascii")).decode(
                        "utf-8"
                    )
                except Exception:
                    LOG.warning("Cannot decrypt OAuth app secret for %s", provider)
            return {"client_id": saved["client_id"], "client_secret": secret or ""}, saved[
                "redirect_url"
            ]
        return self.clients.get(provider, {}), self.redirect

    def device_client_id(self, provider: str) -> str:
        if provider != "onedrive":
            return ""
        from uuid import UUID

        client, _ = self.provider_options("onedrive")
        value = str(
            client.get("client_id")
            or os.environ.get("ONEDRIVE_CLIENT_ID", ONEDRIVE_PUBLIC_CLIENT_ID)
        ).strip()
        try:
            UUID(value)
        except (ValueError, AttributeError, TypeError):
            return ""
        return value

    def device_configuration_error(self, provider: str) -> str | None:
        if provider != "onedrive":
            return "La connexion sans domaine n’est pas encore disponible pour ce fournisseur."
        if not self.device_client_id(provider):
            return "Identifiant public OneDrive absent ou invalide."
        return None

    def _prune_device_sessions(self) -> None:
        now = time.monotonic()
        self.pending_devices = {
            key: entry for key, entry in self.pending_devices.items() if entry["deadline"] > now
        }

    async def start_device(self, location_id: str, provider: str) -> dict:
        """Start an RFC 8628 session without redirect URI or client secret."""
        problem = self.device_configuration_error(provider)
        if problem:
            raise CloudOAuthError(problem)
        location = self.store.playback_location(location_id)
        if not location or location["kind"] != "onedrive":
            raise CloudOAuthError("Emplacement OneDrive introuvable.")
        self._prune_device_sessions()
        if len(self.pending_devices) >= 64:
            raise CloudOAuthError("Trop de connexions en cours. Réessayez plus tard.")
        client_id = self.device_client_id(provider)
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as http:
                response = await http.post(
                    MICROSOFT_DEVICE_ENDPOINT,
                    data={"client_id": client_id, "scope": MICROSOFT_DEVICE_SCOPE},
                )
                result = response.json()
                response.raise_for_status()
        except (httpx.HTTPError, ValueError) as exc:
            LOG.warning("OneDrive device authorization failed (%s)", type(exc).__name__)
            raise CloudOAuthError(
                "Microsoft n’a pas démarré l’autorisation. Vérifiez dans Entra "
                "« Autoriser les flux de clients publics = Oui » et les autorisations Files.Read."
            ) from exc
        if not isinstance(result, dict):
            raise CloudOAuthError("Réponse Microsoft inattendue.")
        try:
            device_code = result["device_code"]
            user_code = result["user_code"]
            verification_uri = result["verification_uri"]
            expires_in = max(1, min(1800, int(result["expires_in"])))
            interval = max(5, min(60, int(result.get("interval", 5))))
        except (ValueError, KeyError, TypeError) as exc:
            raise CloudOAuthError("Réponse Microsoft incomplète.") from exc
        if not all(
            isinstance(x, str) and x for x in (device_code, user_code, verification_uri)
        ) or not verification_uri.startswith("https://"):
            raise CloudOAuthError("Code ou adresse de validation Microsoft invalide.")
        request_id = secrets.token_urlsafe(32)
        now = time.monotonic()
        self.pending_devices[request_id] = {
            "location_id": location_id,
            "client_id": client_id,
            "device_code": device_code,  # stays in server RAM only
            "deadline": now + expires_in,
            "next_poll": now + interval,
            "interval": interval,
            "lock": asyncio.Lock(),
        }
        return {
            "request_id": request_id,
            "user_code": user_code,
            # Use the Microsoft-documented canonical page regardless of
            # region-specific or alternate verification_uri hostnames.
            "verification_uri": MICROSOFT_DEVICE_VERIFICATION_URL,
            "expires_in": expires_in,
            "interval": interval,
        }

    async def poll_device(self, location_id: str, request_id: str) -> dict:
        """One upstream poll per Microsoft interval, even across web/iOS clients."""
        self._prune_device_sessions()
        state = self.pending_devices.get(request_id)
        if not state or state["location_id"] != location_id:
            return {"status": "expired", "message": "Code expiré. Recommencez la connexion."}
        async with state["lock"]:
            now = time.monotonic()
            if state["deadline"] <= now:
                self.pending_devices.pop(request_id, None)
                return {"status": "expired", "message": "Code expiré. Recommencez la connexion."}
            if now < state["next_poll"]:
                return {
                    "status": "pending",
                    "retry_after": max(1, int(state["next_poll"] - now + 0.99)),
                }
            state["next_poll"] = now + state["interval"]
            try:
                async with httpx.AsyncClient(timeout=15, follow_redirects=False) as http:
                    response = await http.post(
                        MICROSOFT_TOKEN_ENDPOINT,
                        data={
                            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                            "client_id": state["client_id"],
                            "device_code": state["device_code"],
                        },
                    )
                    result = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                LOG.warning("OneDrive device polling failed (%s)", type(exc).__name__)
                return {"status": "pending", "retry_after": state["interval"]}
            if not isinstance(result, dict):
                return {"status": "pending", "retry_after": state["interval"]}
            if response.status_code >= 400:
                code = result.get("error")
                if code == "authorization_pending":
                    return {"status": "pending", "retry_after": state["interval"]}
                if code == "slow_down":
                    state["interval"] = min(120, state["interval"] + 5)
                    state["next_poll"] = time.monotonic() + state["interval"]
                    return {"status": "pending", "retry_after": state["interval"]}
                self.pending_devices.pop(request_id, None)
                if code in {"authorization_declined", "access_denied"}:
                    return {"status": "denied", "message": "Autorisation Microsoft refusée."}
                if code in {"expired_token", "bad_verification_code"}:
                    return {"status": "expired", "message": "Code Microsoft expiré ou invalide."}
                if code in {"invalid_client", "unauthorized_client"}:
                    return {
                        "status": "error",
                        "message": "Activez « Autoriser les flux de clients publics » "
                        "dans Microsoft Entra pour cette application.",
                    }
                return {"status": "error", "message": "Microsoft a refusé la connexion OneDrive."}
            access_token = result.get("access_token")
            refresh_token = result.get("refresh_token")
            if not isinstance(access_token, str) or not access_token:
                self.pending_devices.pop(request_id, None)
                return {"status": "error", "message": "Microsoft n’a pas fourni de jeton."}
            if not isinstance(refresh_token, str) or not refresh_token:
                self.pending_devices.pop(request_id, None)
                return {
                    "status": "error",
                    "message": "Jeton de renouvellement absent : vérifiez offline_access.",
                }
            current = self.store.playback_location(location_id)
            if not current or current["kind"] != "onedrive":
                self.pending_devices.pop(request_id, None)
                return {"status": "error", "message": "Emplacement OneDrive supprimé."}
            result["expires_at"] = time.time() + max(0, float(result.get("expires_in") or 3600))
            # Record the exact client used to issue the rotating refresh token.
            result["_hkl_client_id"] = state["client_id"]
            encrypted = self.encrypt_password(json.dumps(result, separators=(",", ":")))
            self.store.set_playback_cloud_token(location_id, "onedrive", encrypted)
            self._account_labels.pop(location_id, None)
            self.pending_devices.pop(request_id, None)
            return {"status": "connected"}

    def configuration_error(self, provider: str) -> str | None:
        """Return a safe actionable explanation, never an OAuth app secret."""
        from urllib.parse import urlsplit

        if provider not in PROVIDERS:
            return "Fournisseur cloud non pris en charge."
        client, redirect = self.provider_options(provider)
        if not client.get("client_id"):
            return (
                "Aucune application OAuth n’est enregistrée pour ce fournisseur sur le serveur. "
                "La configuration est à effectuer une seule fois par l’administrateur."
            )
        if provider in {"onedrive", "google_drive", "box", "pcloud"} and not client.get(
            "client_secret"
        ):
            return "Le secret de l’application OAuth manque dans la configuration du serveur."
        parsed = urlsplit(redirect or "")
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.path != "/api/v1/playback/cloud/callback"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            return (
                "L’URL de retour OAuth doit être une adresse HTTPS publique "
                "se terminant par /api/v1/playback/cloud/callback."
            )
        return None

    def configured(self, provider: str) -> bool:
        return self.configuration_error(provider) is None

    def start(self, location_id: str, provider: str, return_to: str = "web") -> str:
        if return_to not in {"web", "ios"}:
            raise CloudOAuthError("Destination OAuth non reconnue.")
        problem = self.configuration_error(provider)
        if problem:
            raise CloudOAuthError(problem)
        # Expire old one-time CSRF states on each attempt.
        now = time.monotonic()
        self.pending = {k: v for k, v in self.pending.items() if now - v["at"] < 600}
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        self.pending[state] = {
            "at": now,
            "location_id": location_id,
            "provider": provider,
            "verifier": verifier,
            "redirect": self.provider_options(provider)[1],
            "return_to": return_to,
        }
        client, redirect = self.provider_options(provider)
        params = {
            "client_id": client["client_id"],
            "redirect_uri": redirect,
            "response_type": "code",
            "state": state,
        }
        if provider != "pcloud":
            params.update(
                {
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                }
            )
        if PROVIDERS[provider][2]:
            params["scope"] = PROVIDERS[provider][2]
        if provider == "google_drive":
            params["access_type"] = "offline"
            params["prompt"] = "select_account consent"
        if provider == "dropbox":
            params["token_access_type"] = "offline"
            # A new location must be able to authorize a different account,
            # even when Dropbox already has a browser SSO session.
            params["force_reauthentication"] = "true"
        return PROVIDERS[provider][0] + "?" + urlencode(params)

    async def complete(self, *, state: str, code: str, error: str, hostname: str = "") -> str:
        info = self.pending.pop(state, None)
        if info is None or time.monotonic() - info["at"] > 600 or error or not code:
            raise CloudOAuthError("Autorisation expirée ou refusée.")
        provider = info["provider"]
        client, _ = self.provider_options(provider)
        payload = {
            "client_id": client["client_id"],
            "code": code,
        }
        if client.get("client_secret"):
            payload["client_secret"] = client["client_secret"]
        if provider != "pcloud":
            payload.update(
                {
                    "grant_type": "authorization_code",
                    "redirect_uri": info["redirect"],
                    "code_verifier": info["verifier"],
                }
            )
        token_url = PROVIDERS[provider][1]
        # pCloud returns the trusted data-center hostname from the callback;
        # accept only documented hosts, never arbitrary attacker URLs.
        if provider == "pcloud":
            if hostname not in ("", "api.pcloud.com", "eapi.pcloud.com"):
                raise CloudOAuthError("Centre de données pCloud non reconnu.")
            if hostname:
                token_url = "https://" + hostname + "/oauth2_token"
        try:
            async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client_http:
                response = await client_http.post(token_url, data=payload)
                response.raise_for_status()
                token = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            LOG.warning(
                "Cloud OAuth token exchange failed for %s (%s)", provider, type(exc).__name__
            )
            raise CloudOAuthError("Impossible de valider le compte auprès du fournisseur.") from exc
        if (
            not isinstance(token, dict)
            or not isinstance(token.get("access_token"), str)
            or not token["access_token"]
            or (provider == "pcloud" and token.get("result") != 0)
        ):
            raise CloudOAuthError("Réponse d'authentification non valide.")
        if provider == "pcloud":
            token["api_hostname"] = hostname or "api.pcloud.com"
        if token.get("expires_in") is not None:
            token["expires_at"] = time.time() + max(0, float(token["expires_in"]))
        location = self.store.playback_location(info["location_id"])
        if not location or location["kind"] != provider:
            raise CloudOAuthError("L'emplacement a été supprimé ou modifié.")
        serialized = json.dumps(token, separators=(",", ":"))
        encrypted = self.fernet.encrypt(serialized.encode("utf-8")).decode("ascii")
        self.store.set_playback_cloud_token(info["location_id"], provider, encrypted)
        self._account_labels.pop(info["location_id"], None)
        return info.get("return_to", "web")

    async def account_email(self, location_id: str, provider: str) -> str | None:
        """Return a display-only email for an authenticated account.

        OAuth tokens remain server-side. Missing user-information scopes or
        providers that hide an email simply yield None; listing stays usable.
        Avoid a provider-level cache: several accounts of the same type can
        be connected to different playback locations.
        """
        now = time.monotonic()
        cached = self._account_labels.get(location_id)
        if cached and now - cached[0] < 900:
            return cached[1]
        email: str | None = None
        try:
            token = await self.credentials(location_id, provider)
            access = token.get("access_token")
            if not isinstance(access, str) or not access:
                return None
            headers = {"Authorization": "Bearer " + access}
            async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
                if provider == "onedrive":
                    # Files.Read already authorizes /me/drive; User.Read is not
                    # needed just to retrieve the drive owner's email.
                    response = await client.get(
                        "https://graph.microsoft.com/v1.0/me/drive", headers=headers
                    )
                    if response.status_code == 200:
                        owner = response.json().get("owner") or {}
                        user = owner.get("user") or {}
                        email = user.get("email") or user.get("userPrincipalName")
                elif provider == "google_drive":
                    response = await client.get(
                        "https://www.googleapis.com/drive/v3/about",
                        params={"fields": "user(emailAddress)"},
                        headers=headers,
                    )
                    if response.status_code == 200:
                        email = (response.json().get("user") or {}).get("emailAddress")
                elif provider == "box":
                    response = await client.get(
                        "https://api.box.com/2.0/users/me",
                        params={"fields": "login"},
                        headers=headers,
                    )
                    if response.status_code == 200:
                        email = response.json().get("login")
                elif provider == "dropbox":
                    response = await client.post(
                        "https://api.dropboxapi.com/2/users/get_current_account",
                        headers=headers,
                    )
                    if response.status_code == 200:
                        email = response.json().get("email")
        except (CloudOAuthError, httpx.HTTPError, ValueError, TypeError, AttributeError):
            # This metadata is optional; do not break the entire list if a
            # cloud profile endpoint or permission is unavailable.
            email = None
        if not isinstance(email, str) or "@" not in email or len(email) > 254:
            email = None
        self._account_labels[location_id] = (time.monotonic(), email)
        return email

    async def credentials(self, location_id: str, provider: str) -> dict:
        """Return a live provider token server-side; refresh without exposing it."""
        # Concurrent thumbnails and byte-range reads may all discover an
        # expired token at once. Re-read under one lock before refreshing so a
        # rotating refresh token is never redeemed concurrently.
        async with self.location_grant_lock(location_id):
            saved = self.store.playback_cloud_credentials(location_id)
            if not saved or saved["provider"] != provider:
                raise CloudOAuthError("Connectez ce compte cloud dans les réglages.")
            try:
                token = json.loads(self.decrypt_secret(saved["token_cipher"]))
            except (InvalidToken, ValueError, TypeError, KeyError, UnicodeError) as exc:
                raise CloudOAuthError("Jeton cloud illisible. Reconnectez ce compte.") from exc
            if not isinstance(token, dict) or not token.get("access_token"):
                raise CloudOAuthError("Jeton cloud absent. Reconnectez ce compte.")
            try:
                expires_at = float(token.get("expires_at") or 0)
            except (ValueError, TypeError) as exc:
                raise CloudOAuthError("Date d’expiration du jeton cloud invalide.") from exc
            refresh = token.get("refresh_token")
            if not refresh or provider == "pcloud":
                return token
            if expires_at > time.time() + 90:
                return token
            client, _ = self.provider_options(provider)
            public_device_id = token.get("_hkl_client_id") if provider == "onedrive" else None
            client_id = public_device_id or client.get("client_id")
            if not client_id:
                raise CloudOAuthError("Application OAuth supprimée. Reconfigurez ce fournisseur.")
            payload = {
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": client_id,
            }
            # A public device-flow client cannot use a confidential client secret.
            if client.get("client_secret") and not public_device_id:
                payload["client_secret"] = client["client_secret"]
            try:
                async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client_http:
                    response = await client_http.post(PROVIDERS[provider][1], data=payload)
                    response.raise_for_status()
                    fresh = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                LOG.warning("Cloud OAuth refresh failed for %s (%s)", provider, type(exc).__name__)
                raise CloudOAuthError(
                    "La session cloud a expiré et son renouvellement a échoué. Reconnectez ce compte."
                ) from exc
            if not isinstance(fresh, dict) or not fresh.get("access_token"):
                raise CloudOAuthError("Le fournisseur a refusé le renouvellement du jeton.")
            token.update(fresh)
            if not token.get("refresh_token"):
                token["refresh_token"] = refresh
            # Fall back to a short-lived expiry if a provider omits expires_in.
            token["expires_at"] = time.time() + max(0, float(fresh.get("expires_in") or 3600))
            encrypted = self.encrypt_password(json.dumps(token, separators=(",", ":")))
            self.store.set_playback_cloud_token(location_id, provider, encrypted)
            return token
