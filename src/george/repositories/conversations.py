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


def search_by_date(
    chat_id: str, tenant_id: str, since: str, until: str, limit: int = 300
) -> list[dict[str, Any]]:
    """Inbound/outbound turns for this chat within [since, until) (ISO-8601
    UTC), oldest first, restricted to `tenant_id` — used by
    tools/history.py::recall_chat_history so George can read back a past
    day's real messages once they've scrolled out of the automatic
    HISTORY_TURNS window (see function_app.py::_history_to_messages). The
    tenantId filter matters for a dual-tenant chat: don't surface another
    business's conversation just because it shares this chatId."""
    query = (
        f"SELECT TOP {int(limit)} c.ts, c.direction, c.input, c.output FROM c "
        "WHERE c.chatId = @chatId AND c.tenantId = @tenantId AND c.ts >= @since AND c.ts < @until "
        "ORDER BY c.ts ASC"
    )
    return list(
        _container().query_items(
            query=query,
            parameters=[
                {"name": "@chatId", "value": chat_id},
                {"name": "@tenantId", "value": tenant_id},
                {"name": "@since", "value": since},
                {"name": "@until", "value": until},
            ],
            partition_key=chat_id,
        )
    )


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
