"""`clients` container — customers and their "items" (whatever the tenant's
business revolves around — pets, vehicles, properties, ...). Partition key:
/tenantId. id == clientId (unique within the tenant's partition, not globally).
"""
from __future__ import annotations

from typing import Any, Optional

from azure.cosmos import exceptions

from george.repositories.cosmos import get_container
from george.util import new_id, utc_now_iso


def _container():
    return get_container("clients")


def get_client(tenant_id: str, client_id: str) -> Optional[dict[str, Any]]:
    try:
        return _container().read_item(item=client_id, partition_key=tenant_id)
    except exceptions.CosmosResourceNotFoundError:
        return None


def search_clients(tenant_id: str, text: str, limit: int = 10) -> list[dict[str, Any]]:
    """Case-insensitive substring match against name and aliases, scoped to
    one tenant's partition — never touches another tenant's data."""
    query = f"""
    SELECT TOP {int(limit)} * FROM c
    WHERE CONTAINS(LOWER(c.name), @needle)
       OR EXISTS(SELECT VALUE a FROM a IN c.aliases WHERE CONTAINS(LOWER(a), @needle))
    """
    return list(
        _container().query_items(
            query=query,
            parameters=[{"name": "@needle", "value": text.lower()}],
            partition_key=tenant_id,
        )
    )


def list_active_clients(tenant_id: str, limit: int = 200) -> list[dict[str, Any]]:
    query = f"SELECT TOP {int(limit)} * FROM c WHERE c.status = 'active'"
    return list(_container().query_items(query=query, partition_key=tenant_id))


def upsert_client(tenant_id: str, client_id: Optional[str], created_by: str, **fields: Any) -> dict[str, Any]:
    client_id = client_id or new_id()
    existing = get_client(tenant_id, client_id) or {
        "id": client_id,
        "clientId": client_id,
        "tenantId": tenant_id,
        "createdAt": utc_now_iso(),
        "createdBy": created_by,
        "items": [],
        "aliases": [],
        "tags": [],
        "notes": [],
        "custom": {},
        "status": "active",
    }
    existing.update({k: v for k, v in fields.items() if v is not None})
    existing["updatedAt"] = utc_now_iso()
    return _container().upsert_item(existing)


def upsert_item(tenant_id: str, client_id: str, item_id: Optional[str], **fields: Any) -> dict[str, Any]:
    """Creates or updates one of a client's generic 'items' — a pet, a
    vehicle, a property, whatever the tenant's business is organized around."""
    client = get_client(tenant_id, client_id)
    if client is None:
        raise ValueError(f"Unknown clientId {client_id!r} for this tenant")

    items = client.setdefault("items", [])
    item_id = item_id or new_id()
    item = next((i for i in items if i.get("itemId") == item_id), None)
    if item is None:
        item = {"itemId": item_id, "attributes": {}, "custom": {}}
        items.append(item)
    item.update({k: v for k, v in fields.items() if v is not None})

    client["updatedAt"] = utc_now_iso()
    return _container().upsert_item(client)
