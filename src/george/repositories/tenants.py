"""`tenants` container — one document per business using George. Partition
key: /id. Small, platform-admin-managed container: every other container's
isolation depends on the `id` here matching a real, active tenant.
"""
from __future__ import annotations

from typing import Any, Optional

from azure.cosmos import exceptions

from george.repositories.cosmos import get_container
from george.util import new_id, utc_now_iso

VALID_STATUSES = ("active", "suspended")


def _container():
    return get_container("tenants")


def get_tenant(tenant_id: str) -> Optional[dict[str, Any]]:
    try:
        return _container().read_item(item=tenant_id, partition_key=tenant_id)
    except exceptions.CosmosResourceNotFoundError:
        return None


def get_active_tenant(tenant_id: str) -> Optional[dict[str, Any]]:
    """Like get_tenant, but returns None for a suspended tenant too — the
    single check callers should use before letting a chat act on tenant data."""
    tenant = get_tenant(tenant_id)
    if tenant is None or tenant.get("status") != "active":
        return None
    return tenant


def list_tenants(status: Optional[str] = None, limit: int = 200) -> list[dict[str, Any]]:
    if status:
        query = f"SELECT TOP {int(limit)} * FROM c WHERE c.status = @status"
        params = [{"name": "@status", "value": status}]
    else:
        query = f"SELECT TOP {int(limit)} * FROM c"
        params = []
    return list(
        _container().query_items(query=query, parameters=params, enable_cross_partition_query=True)
    )


def create_tenant(
    name: str,
    business_type: str,
    created_by: str,
    description: str = "",
    currency: str = "COP",
    timezone: str = "America/Bogota",
    locale: str = "es-CO",
    item_label_singular: str = "cliente",
    item_label_plural: str = "clientes",
    **fields: Any,
) -> dict[str, Any]:
    tenant_id = new_id()
    doc = {
        "id": tenant_id,
        "name": name,
        "businessType": business_type,
        "description": description,
        "currency": currency,
        "timezone": timezone,
        "locale": locale,
        "itemLabelSingular": item_label_singular,
        "itemLabelPlural": item_label_plural,
        "status": "active",
        "createdAt": utc_now_iso(),
        "createdBy": created_by,
        "settings": {},
        "custom": {},
        **fields,
    }
    return _container().create_item(doc)
