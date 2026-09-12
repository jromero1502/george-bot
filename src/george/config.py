"""Centralized configuration read from environment variables (app settings in Azure,
local.settings.json locally). Import `settings` — don't read os.environ elsewhere.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required app setting: {name}")
    return value or ""


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class Settings:
    # Cosmos DB
    cosmos_endpoint: str = field(default_factory=lambda: _env("COSMOS_ENDPOINT", required=True))
    cosmos_database: str = field(default_factory=lambda: _env("COSMOS_DATABASE", "george"))
    # Only set locally, against the Cosmos DB Emulator. In Azure this is absent
    # and repositories/cosmos.py falls back to DefaultAzureCredential (managed identity).
    cosmos_key: str = field(default_factory=lambda: _env("COSMOS_KEY"))

    # Telegram
    telegram_bot_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN", required=True))
    telegram_webhook_secret: str = field(default_factory=lambda: _env("TELEGRAM_WEBHOOK_SECRET", required=True))
    telegram_webhook_path: str = field(default_factory=lambda: _env("TELEGRAM_WEBHOOK_PATH", required=True))

    # LLM providers
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY", required=True))
    anthropic_model: str = field(default_factory=lambda: _env("ANTHROPIC_MODEL", "claude-haiku-4-5"))
    groq_api_key: str = field(default_factory=lambda: _env("GROQ_API_KEY", required=True))
    groq_stt_model: str = field(default_factory=lambda: _env("GROQ_STT_MODEL", "whisper-large-v3-turbo"))

    # Multi-tenant platform
    default_timezone: str = field(default_factory=lambda: _env("DEFAULT_TIMEZONE", "America/Bogota"))
    # The platform operator's chat — role=platform_admin in `chats`, receives
    # poison-queue alerts. Not tied to any one tenant/business (see
    # george.tools.tenants for how tenants get created).
    platform_admin_chat_id: str = field(default_factory=lambda: _env("PLATFORM_ADMIN_CHAT_ID", required=True))
    history_turns: int = field(default_factory=lambda: _env_int("HISTORY_TURNS", 12))

    # When true, telegram.py logs outbound messages instead of calling the
    # Telegram API — lets the whole pipeline run against a fake/absent bot.
    dry_run: bool = field(default_factory=lambda: _env_bool("DRY_RUN", False))

    # Queue names (kept in sync with infra/modules/storage.bicep)
    queue_incoming: str = "george-incoming"
    queue_reminders: str = "george-reminders"


settings = Settings()

# Anthropic pricing for claude-haiku-4-5, per the claude-api skill's cached
# model table (USD per million tokens). Used only to estimate costUsd on
# audit records — not billed against, so a stale constant here is low-risk.
HAIKU_INPUT_USD_PER_MTOK = 1.00
HAIKU_OUTPUT_USD_PER_MTOK = 5.00


def estimate_cost_usd(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens / 1_000_000) * HAIKU_INPUT_USD_PER_MTOK + (
        output_tokens / 1_000_000
    ) * HAIKU_OUTPUT_USD_PER_MTOK
