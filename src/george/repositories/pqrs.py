"""`pqrs` container — peticiones, quejas, reclamos y sugerencias raised by
chats (usually auto-detected by the agent). Partition key: /tenantId.
"""
from __future__ import annotations

from typing import Any, Optional

from george.repositories.cosmos import get_container
from george.util import new_id, utc_now_iso

VALID_TYPES = ("peticion", "queja", "reclamo", "sugerencia", "bug")
VALID_STATUSES = ("open", "in_progress", "resolved", "wont_fix")


def _container():
    return get_container("pqrs")


def create_pqr(
    tenant_id: str,
    chat_id: str,
    reported_by: dict[str, Any],
    type_: str,
    title: str,
    description: str,
    **fields: Any,
) -> dict[str, Any]:
    doc = {
        "id": new_id(),
        "tenantId": tenant_id,
        "chatId": chat_id,
        "createdAt": utc_now_iso(),
        "reportedBy": reported_by,
        "type": type_,
        "title": title,
        "description": description,
        "severity": fields.pop("severity", "medium"),
        "status": "open",
        "comments": [],
        "tags": [],
        "custom": {},
        **fields,
    }
    return _container().create_item(doc)


def list_pqrs(tenant_id: str, status: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
    if status:
        query = f"SELECT TOP {int(limit)} * FROM c WHERE c.status = @status ORDER BY c.createdAt DESC"
        params = [{"name": "@status", "value": status}]
    else:
        query = f"SELECT TOP {int(limit)} * FROM c ORDER BY c.createdAt DESC"
        params = []
    return list(
        _container().query_items(query=query, parameters=params, partition_key=tenant_id)
    )
