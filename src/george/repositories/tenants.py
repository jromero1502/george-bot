"""`tenants` container — one document per business using George. Partition
key: /id. Small, platform-admin-managed container: every other container's
isolation depends on the `id` here matching a real, active tenant.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from azure.core import MatchConditions
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


def update_business_rules(tenant_id: str, mutate: Callable[[list[dict[str, Any]]], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Read-modify-write of `businessRules` guarded by the document's ETag, so two
    owners editing at once can't silently drop each other's change. `mutate`
    gets the current list and returns the new one (it may raise ValueError to
    abort). Retries once on a lost race."""
    for attempt in (0, 1):
        tenant = get_tenant(tenant_id)
        if tenant is None:
            raise LookupError(f"Tenant {tenant_id} no existe.")
        rules = mutate(list(tenant.get("businessRules") or []))
        tenant["businessRules"] = rules
        tenant["updatedAt"] = utc_now_iso()
        try:
            _container().replace_item(
                item=tenant_id,
                body=tenant,
                etag=tenant.get("_etag"),
                match_condition=MatchConditions.IfNotModified,
            )
            return rules
        except exceptions.CosmosAccessConditionFailedError:
            if attempt == 1:
                raise
    return []  # unreachable
