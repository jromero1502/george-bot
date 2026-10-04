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


def search_by_date(tenant_id: str, since: str, until: str, limit: int = 300) -> list[dict[str, Any]]:
    """Inbound/outbound turns for this TENANT — across every chat that
    belongs to it, not just one — within [since, until) (ISO-8601 UTC),
    oldest first. Used by tools/history.py::recall_chat_history so George
    can read back a past day's real messages once they've scrolled out of
    the automatic HISTORY_TURNS window (see
    function_app.py::_history_to_messages).

    Originally scoped to a single chatId (the partition key), on the theory
    that "recupera lo que te mandé" meant one person's own messages. Real
    incident (2026-10-04): a second team member (different chatId, same
    tenant) asked to recall a day she hadn't personally written in, got an
    empty result, and George told her "la conversación no quedó
    registrada" — implying data loss, when it had simply been written by a
    teammate under a different chatId. A business's conversation history is
    shared operational data for its own team (owner/admin, same roles this
    tool is already gated to), not private per chat, so this is
    intentionally cross-partition (/chatId) and filtered by tenantId
    instead — same tradeoff already made elsewhere at this bot's scale (see
    records.py::summarize_records, or the ad-hoc audits that found this
    very bug)."""
    query = (
        f"SELECT TOP {int(limit)} c.chatId, c.userName, c.ts, c.direction, c.input, c.output FROM c "
        "WHERE c.tenantId = @tenantId AND c.ts >= @since AND c.ts < @until "
        "ORDER BY c.ts ASC"
    )
    return list(
        _container().query_items(
            query=query,
            parameters=[
                {"name": "@tenantId", "value": tenant_id},
                {"name": "@since", "value": since},
                {"name": "@until", "value": until},
            ],
            enable_cross_partition_query=True,
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
