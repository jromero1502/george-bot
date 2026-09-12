"""Small shared helpers used across the codebase."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    """ISO-8601 with a literal 'Z' suffix (Cosmos DB / Telegram / JS friendly)."""
    return utc_now().isoformat(timespec="seconds").replace("+00:00", "Z")


def new_id() -> str:
    return str(uuid.uuid4())
