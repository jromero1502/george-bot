"""Thin Telegram Bot API client. All outbound calls honor DRY_RUN — set it to
`true` locally to run the whole pipeline without a real bot token or hitting
Telegram's servers.
"""
from __future__ import annotations

import hmac
import logging
from typing import Any, Optional

import httpx

from george.config import settings

logger = logging.getLogger("george.telegram")

_API_BASE = "https://api.telegram.org"


def _method_url(method: str) -> str:
    return f"{_API_BASE}/bot{settings.telegram_bot_token}/{method}"


def _post(method: str, json: dict[str, Any]) -> dict[str, Any]:
    if settings.dry_run:
        logger.info("[DRY_RUN] telegram.%s payload=%s", method, json)
        return {"ok": True, "result": {}}
    with httpx.Client(timeout=20.0) as client:
        response = client.post(_method_url(method), json=json)
        response.raise_for_status()
        return response.json()


def verify_webhook_secret(header_value: Optional[str]) -> bool:
    """Constant-time comparison of the X-Telegram-Bot-Api-Secret-Token header
    against TELEGRAM_WEBHOOK_SECRET. This is the actual auth boundary of the
    webhook — the function-level key and the URL path are only extra layers."""
    if not header_value:
        return False
    return hmac.compare_digest(header_value, settings.telegram_webhook_secret)


def send_message(chat_id: str, text: str, reply_to_message_id: Optional[int] = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = reply_to_message_id
    return _post("sendMessage", payload)


def send_chat_action(chat_id: str, action: str = "typing") -> dict[str, Any]:
    return _post("sendChatAction", {"chat_id": chat_id, "action": action})


def get_file(file_id: str) -> str:
    """Returns the Telegram-internal file_path for a file_id, needed to build
    the download URL. Not meaningful under DRY_RUN (there's no real bot to ask) —
    use LOCAL_AUDIO_PATH in the queue message for local voice-note testing instead
    (see scripts/simulate_update.py --voice)."""
    if settings.dry_run:
        raise RuntimeError(
            "telegram.get_file() called under DRY_RUN — pass localAudioPath in the "
            "queue message instead of relying on Telegram's getFile/download"
        )
    result = _post("getFile", {"file_id": file_id})
    return result["result"]["file_path"]


def download_file(file_path: str) -> bytes:
    url = f"{_API_BASE}/file/bot{settings.telegram_bot_token}/{file_path}"
    with httpx.Client(timeout=30.0) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.content
