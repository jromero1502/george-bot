"""Per-tenant Google OAuth credentials (personal @gmail accounts) -> access tokens.

Each tenant has ONE Key Vault secret, `google-<tenantId>`, written by hand, whose
value is JSON: {"client_id": "...", "client_secret": "...", "refresh_token": "..."}.
Nothing here ever persists a token: the refresh token stays in Key Vault, and the
short-lived access token only lives in this process's memory.

The refresh token must have been granted (by the account owner, once) the scopes
https://www.googleapis.com/auth/gmail.readonly and
https://www.googleapis.com/auth/calendar.events. Keep the OAuth consent screen
in "In production" publishing status — in "Testing" Google expires refresh
tokens after 7 days (surfaces here as `invalid_grant`).
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

import httpx

from george.config import settings

logger = logging.getLogger("george.google_auth")

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_SECRET_PREFIX = "google-"
_CREDENTIALS_TTL_S = 300
_EXPIRY_MARGIN_S = 60

# tenant_id -> (loaded_at_monotonic, credentials dict) / (expires_at_monotonic, token)
_credentials_cache: dict[str, tuple[float, dict[str, str]]] = {}
_token_cache: dict[str, tuple[float, str]] = {}
_secret_client: Any = None


class GoogleAuthError(Exception):
    """Credentials missing/invalid or Google refused the refresh. The message is
    user-facing (tools/google.py turns it into a ToolError)."""


def secret_name(tenant_id: str) -> str:
    return f"{_SECRET_PREFIX}{tenant_id}"


def _client() -> Any:
    global _secret_client
    if _secret_client is None:
        # Imported lazily so tests / deployments without Key Vault never need the SDK.
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient

        _secret_client = SecretClient(vault_url=settings.key_vault_uri, credential=DefaultAzureCredential())
    return _secret_client


def _load_credentials(tenant_id: str) -> dict[str, str]:
    cached = _credentials_cache.get(tenant_id)
    if cached and time.monotonic() - cached[0] < _CREDENTIALS_TTL_S:
        return cached[1]

    if not settings.key_vault_uri:
        raise GoogleAuthError("Google no está disponible en esta plataforma (falta configurar el Key Vault).")

    try:
        raw = _client().get_secret(secret_name(tenant_id)).value
    except Exception as exc:  # noqa: BLE001 — SDK raises several unrelated types
        if type(exc).__name__ == "ResourceNotFoundError":
            raise GoogleAuthError(
                "Este negocio todavía no tiene una cuenta de Google conectada. El administrador de la "
                f"plataforma debe cargar el secreto '{secret_name(tenant_id)}' en el Key Vault."
            ) from None
        logger.exception("key vault read failed for tenant %s", tenant_id)
        raise GoogleAuthError(f"No pude leer las credenciales de Google del Key Vault: {type(exc).__name__}.") from None

    try:
        creds = json.loads(raw or "")
        missing = [k for k in ("client_id", "client_secret", "refresh_token") if not creds.get(k)]
    except (ValueError, AttributeError):
        creds, missing = {}, ["JSON válido"]
    if missing:
        raise GoogleAuthError(
            f"El secreto '{secret_name(tenant_id)}' está mal formado (falta: {', '.join(missing)}). "
            'Debe ser JSON con client_id, client_secret y refresh_token.'
        )

    _credentials_cache[tenant_id] = (time.monotonic(), creds)
    return creds


def get_access_token(tenant_id: str, *, force_refresh: bool = False) -> str:
    cached: Optional[tuple[float, str]] = None if force_refresh else _token_cache.get(tenant_id)
    if cached and time.monotonic() < cached[0]:
        return cached[1]

    creds = _load_credentials(tenant_id)
    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.post(
                _TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": creds["client_id"],
                    "client_secret": creds["client_secret"],
                    "refresh_token": creds["refresh_token"],
                },
            )
    except httpx.HTTPError as exc:
        raise GoogleAuthError(f"No pude contactar a Google para renovar el acceso: {type(exc).__name__}.") from None

    if response.status_code != 200:
        error = ""
        try:
            error = response.json().get("error", "")
        except ValueError:
            pass
        # Drop what we cached: the secret may have just been replaced with a fixed one.
        _credentials_cache.pop(tenant_id, None)
        _token_cache.pop(tenant_id, None)
        if error == "invalid_grant":
            raise GoogleAuthError(
                "Google rechazó el refresh token (revocado o vencido; si la app OAuth está en modo "
                "'Testing' vence cada 7 días). Hay que generar uno nuevo y actualizar el secreto "
                f"'{secret_name(tenant_id)}'."
            )
        raise GoogleAuthError(f"Google rechazó la renovación de acceso ({response.status_code} {error}).")

    payload = response.json()
    token = payload["access_token"]
    _token_cache[tenant_id] = (time.monotonic() + int(payload.get("expires_in", 3600)) - _EXPIRY_MARGIN_S, token)
    return token


def clear_cache() -> None:
    _credentials_cache.clear()
    _token_cache.clear()
