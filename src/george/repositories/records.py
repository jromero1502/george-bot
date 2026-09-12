"""`records` container — owner-defined record types (stock snapshots,
attendance logs, whatever a tenant wants to track over time) plus the
records logged against them. Partition key: /tenantId, discriminated by
`docType: "type_definition" | "record"`.

A type definition's `id` is `type::<typeKey>` — Cosmos itself enforces one
definition per typeKey within a tenant (a create with a colliding id fails),
and it's a point read instead of a query. See george/record_schema.py for
the validation that produces `type_def` before it reaches this module.
"""
from __future__ import annotations

from typing import Any, Optional

from azure.cosmos import exceptions

from george.repositories.cosmos import get_container
from george.util import new_id, utc_now_iso

_DEFAULT_SUMMARY_LIMIT = 500


def _container():
    return get_container("records")


def _type_doc_id(type_key: str) -> str:
    return f"type::{type_key}"


def get_record_type(tenant_id: str, type_key: str) -> Optional[dict[str, Any]]:
    try:
        return _container().read_item(item=_type_doc_id(type_key), partition_key=tenant_id)
    except exceptions.CosmosResourceNotFoundError:
        return None


def list_record_types(tenant_id: str, include_archived: bool = False) -> list[dict[str, Any]]:
    clauses = ["c.docType = 'type_definition'"]
    if not include_archived:
        clauses.append("c.status = 'active'")
    query = f"SELECT * FROM c WHERE {' AND '.join(clauses)} ORDER BY c.name ASC"
    return list(_container().query_items(query=query, partition_key=tenant_id))


def upsert_record_type(
    tenant_id: str, type_def: dict[str, Any], created_by: str, status: Optional[str] = None
) -> dict[str, Any]:
    """`type_def` is the normalized dict from record_schema.validate_type_definition.
    Editing an existing type (same typeKey) preserves createdAt/createdBy and,
    unless `status` is given, its current status."""
    type_key = type_def["typeKey"]
    existing = get_record_type(tenant_id, type_key)
    doc = existing or {
        "id": _type_doc_id(type_key),
        "tenantId": tenant_id,
        "docType": "type_definition",
        "typeKey": type_key,
        "status": "active",
        "createdAt": utc_now_iso(),
        "createdBy": created_by,
        "custom": {},
    }
    doc.update(
        {
            "name": type_def["name"],
            "mode": type_def["mode"],
            "description": type_def["description"],
            "fields": type_def["fields"],
            "measureField": type_def["measureField"],
            "groupField": type_def["groupField"],
            "clientLink": type_def["clientLink"],
        }
    )
    if status:
        doc["status"] = status
    doc["updatedAt"] = utc_now_iso()
    return _container().upsert_item(doc)


def create_record(
    tenant_id: str,
    type_key: str,
    type_name: str,
    created_by: str,
    values: dict[str, Any],
    amount: float,
    group_key: str,
    occurred_at: str,
    period: str,
    client_id: Optional[str] = None,
    client_name: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict[str, Any]:
    doc = {
        "id": new_id(),
        "tenantId": tenant_id,
        "docType": "record",
        "typeKey": type_key,
        "typeName": type_name,
        "clientId": client_id,
        "clientName": client_name,
        "occurredAt": occurred_at,
        "period": period,
        "values": values,
        "amount": amount,
        "groupKey": group_key,
        "notes": notes,
        "createdAt": utc_now_iso(),
        "createdBy": created_by,
        "updatedAt": utc_now_iso(),
        "custom": {},
    }
    return _container().create_item(doc)


def search_records(
    tenant_id: str,
    type_key: Optional[str] = None,
    client_id: Optional[str] = None,
    group_key: Optional[str] = None,
    period: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    clauses = ["c.docType = 'record'"]
    params: list[dict[str, Any]] = []

    if type_key:
        clauses.append("c.typeKey = @typeKey")
        params.append({"name": "@typeKey", "value": type_key})
    if client_id:
        clauses.append("c.clientId = @clientId")
        params.append({"name": "@clientId", "value": client_id})
    if group_key:
        clauses.append("c.groupKey = @groupKey")
        params.append({"name": "@groupKey", "value": group_key})
    if period:
        clauses.append("c.period = @period")
        params.append({"name": "@period", "value": period})
    if since:
        clauses.append("c.occurredAt >= @since")
        params.append({"name": "@since", "value": since})
    if until:
        clauses.append("c.occurredAt <= @until")
        params.append({"name": "@until", "value": until})

    query = f"SELECT TOP {int(limit)} * FROM c WHERE {' AND '.join(clauses)} ORDER BY c.occurredAt DESC"
    return list(_container().query_items(query=query, parameters=params, partition_key=tenant_id))


def summarize_records(
    tenant_id: str,
    type_key: str,
    mode: str,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Reduces recent records of one type into a per-group summary in Python
    rather than a Cosmos GROUP BY: `mode='snapshot'` wants the single most
    recent record per group (a self-join Cosmos SQL can't express directly),
    and at the data volumes this bot deals with (small businesses, one query
    per report) fetching up to _DEFAULT_SUMMARY_LIMIT records and reducing
    them here is simpler than two different aggregation queries."""
    records = search_records(
        tenant_id, type_key=type_key, since=since, until=until, limit=_DEFAULT_SUMMARY_LIMIT
    )

    if mode == "snapshot":
        latest: dict[str, dict[str, Any]] = {}
        for record in records:  # already ordered by occurredAt DESC
            group_key = record.get("groupKey")
            if group_key not in latest:
                latest[group_key] = record
        return [
            {"groupKey": group_key, "amount": record.get("amount"), "occurredAt": record.get("occurredAt")}
            for group_key, record in sorted(latest.items())
        ]

    totals: dict[str, dict[str, Any]] = {}
    for record in records:
        group_key = record.get("groupKey")
        bucket = totals.setdefault(group_key, {"groupKey": group_key, "total": 0.0, "count": 0})
        bucket["total"] += record.get("amount") or 0.0
        bucket["count"] += 1
    return sorted(totals.values(), key=lambda bucket: bucket["groupKey"])


def delete_record(tenant_id: str, record_id: str) -> bool:
    try:
        _container().delete_item(item=record_id, partition_key=tenant_id)
        return True
    except exceptions.CosmosResourceNotFoundError:
        return False
