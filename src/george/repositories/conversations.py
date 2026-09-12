"""`conversations` container — one document per turn (audit trail + memory).
Partition key: /chatId.
"""
from __future__ import annotations

from typing import Any

from george.repositories.cosmos import get_container
from george.util import new_id, utc_now_iso


def _container():
    return get_container("conversations")


def create_conversation(chat_id: str, **fields: Any) -> dict[str, Any]:
    doc = {
        "id": new_id(),
        "chatId": chat_id,
        "ts": utc_now_iso(),
        "custom": {},
        **fields,
    }
    return _container().create_item(doc)


def get_recent(chat_id: str, limit: int) -> list[dict[str, Any]]:
    """Last `limit` turns for this chat, returned oldest-first (ready to feed
    straight into the Anthropic `messages` array)."""
    query = f"SELECT TOP {int(limit)} * FROM c WHERE c.chatId = @chatId ORDER BY c.ts DESC"
    items = list(
        _container().query_items(
            query=query,
            parameters=[{"name": "@chatId", "value": chat_id}],
            partition_key=chat_id,
        )
    )
    items.reverse()
    return items


def already_processed(chat_id: str, update_id: int) -> bool:
    """Idempotency guard: a queue message can be delivered more than once
    (at-least-once delivery + retries on transient failures)."""
    query = "SELECT VALUE COUNT(1) FROM c WHERE c.chatId = @chatId AND c.trace.updateId = @updateId"
    result = list(
        _container().query_items(
            query=query,
            parameters=[
                {"name": "@chatId", "value": chat_id},
                {"name": "@updateId", "value": update_id},
            ],
            partition_key=chat_id,
        )
    )
    return bool(result) and result[0] > 0
