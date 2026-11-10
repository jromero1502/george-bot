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

import re
from typing import Any, Optional

from azure.cosmos import exceptions

from george.repositories.cosmos import get_container
from george.util import new_id, utc_now_iso

_DEFAULT_SUMMARY_LIMIT = 500
_SAFE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


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
            "dateField": type_def.get("dateField"),
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


def _value_path(key: str) -> str:
    """Property path for a field inside `values`. Cosmos SQL cannot parametrize
    a property name, so the key is interpolated. Callers MUST have passed it
    through record_schema.validate_query (only declared field keys, which match
    ^[a-z][a-z0-9_]{0,39}$); this re-check is defense in depth."""
    if not _SAFE_KEY_RE.match(key):
        raise ValueError(f"Clave de campo invalida: {key!r}")
    return f'c.values["{key}"]'


def _where(
    type_key: Optional[str],
    client_id: Optional[str],
    group_key: Optional[str],
    period: Optional[str],
    since: Optional[str],
    until: Optional[str],
    value_filters: Optional[list[tuple[str, Any]]],
) -> tuple[list[str], list[dict[str, Any]]]:
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
    for i, (key, value) in enumerate(value_filters or []):
        clauses.append(f"{_value_path(key)} = @vf{i}")
        params.append({"name": f"@vf{i}", "value": value})
    return clauses, params


def search_records(
    tenant_id: str,
    type_key: Optional[str] = None,
    client_id: Optional[str] = None,
    group_key: Optional[str] = None,
    period: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 50,
    value_filters: Optional[list[tuple[str, Any]]] = None,
) -> list[dict[str, Any]]:
    clauses, params = _where(type_key, client_id, group_key, period, since, until, value_filters)
    query = f"SELECT TOP {int(limit)} * FROM c WHERE {' AND '.join(clauses)} ORDER BY c.occurredAt DESC"
    return list(_container().query_items(query=query, parameters=params, partition_key=tenant_id))


def count_records(
    tenant_id: str,
    type_key: Optional[str] = None,
    client_id: Optional[str] = None,
    group_key: Optional[str] = None,
    period: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    value_filters: Optional[list[tuple[str, Any]]] = None,
) -> int:
    clauses, params = _where(type_key, client_id, group_key, period, since, until, value_filters)
    query = f"SELECT VALUE COUNT(1) FROM c WHERE {' AND '.join(clauses)}"
    rows = list(_container().query_items(query=query, parameters=params, partition_key=tenant_id))
    return int(rows[0]) if rows else 0


def _group_expr(group_by: Optional[str], type_group_field: Optional[str]) -> str:
    if group_by is None or group_by == type_group_field:
        return "c.groupKey"  # denormalized, same value as values[groupField]
    if group_by == "period":
        return "c.period"
    return _value_path(group_by)


def _sort_key(bucket: dict[str, Any]) -> str:
    return "" if bucket["groupKey"] is None else str(bucket["groupKey"])


def summarize_records(
    tenant_id: str,
    type_key: str,
    mode: str,
    since: Optional[str] = None,
    until: Optional[str] = None,
    client_id: Optional[str] = None,
    period: Optional[str] = None,
    value_filters: Optional[list[tuple[str, Any]]] = None,
    group_by: Optional[str] = None,
    type_group_field: Optional[str] = None,
) -> dict[str, Any]:
    """Returns {"summary": [...], "truncated": bool, "ungrouped": {...}|None}.

    `mode='event'` aggregates in Cosmos (COUNT/SUM ... GROUP BY), so there is
    no row cap and no silent truncation. Records missing the group_by field
    (an optional field left empty) are dropped by Cosmos' GROUP BY, so a second
    ungrouped total is taken and the difference is reported as `ungrouped`.

    `mode='snapshot'` wants the single most recent record per group, a
    self-join Cosmos SQL can't express, so it reduces in Python over up to
    _DEFAULT_SUMMARY_LIMIT records and says so via `truncated` if there were
    more."""
    expr = _group_expr(group_by, type_group_field)
    clauses, params = _where(type_key, client_id, None, period, since, until, value_filters)
    where = " AND ".join(clauses)

    if mode == "snapshot":
        records = search_records(
            tenant_id,
            type_key=type_key,
            client_id=client_id,
            period=period,
            since=since,
            until=until,
            limit=_DEFAULT_SUMMARY_LIMIT + 1,
            value_filters=value_filters,
        )
        truncated = len(records) > _DEFAULT_SUMMARY_LIMIT
        records = records[:_DEFAULT_SUMMARY_LIMIT]

        def group_of(record: dict[str, Any]) -> Any:
            if expr == "c.groupKey":
                return record.get("groupKey")
            if expr == "c.period":
                return record.get("period")
            return (record.get("values") or {}).get(group_by)

        latest: dict[Any, dict[str, Any]] = {}
        for record in records:  # already ordered by occurredAt DESC
            latest.setdefault(group_of(record), record)
        summary = [
            {"groupKey": key, "amount": record.get("amount"), "occurredAt": record.get("occurredAt")}
            for key, record in latest.items()
        ]
        return {"summary": sorted(summary, key=_sort_key), "truncated": truncated, "ungrouped": None}

    grouped_query = (
        f"SELECT {expr} AS groupKey, COUNT(1) AS n, SUM(c.amount) AS total "
        f"FROM c WHERE {where} GROUP BY {expr}"
    )
    rows = list(_container().query_items(query=grouped_query, parameters=params, partition_key=tenant_id))
    summary = [
        {"groupKey": row.get("groupKey"), "total": float(row.get("total") or 0.0), "count": int(row["n"])}
        for row in rows
    ]

    totals_query = f"SELECT COUNT(1) AS n, SUM(c.amount) AS total FROM c WHERE {where}"
    totals = list(_container().query_items(query=totals_query, parameters=params, partition_key=tenant_id))
    ungrouped = None
    if totals:
        missing = int(totals[0]["n"]) - sum(b["count"] for b in summary)
        if missing > 0:
            ungrouped = {
                "count": missing,
                "total": float(totals[0].get("total") or 0.0) - sum(b["total"] for b in summary),
            }
    return {"summary": sorted(summary, key=_sort_key), "truncated": False, "ungrouped": ungrouped}


def get_record(tenant_id: str, record_id: str) -> Optional[dict[str, Any]]:
    try:
        doc = _container().read_item(item=record_id, partition_key=tenant_id)
    except exceptions.CosmosResourceNotFoundError:
        return None
    return doc if doc.get("docType") == "record" else None


def update_record(
    tenant_id: str,
    record_id: str,
    values: dict[str, Any],
    amount: float,
    group_key: str,
    occurred_at: str,
    period: str,
    client_id: Optional[str] = None,
    client_name: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict[str, Any]:
    doc = _container().read_item(item=record_id, partition_key=tenant_id)
    doc.update(
        {
            "values": values,
            "amount": amount,
            "groupKey": group_key,
            "occurredAt": occurred_at,
            "period": period,
            "clientId": client_id,
            "clientName": client_name,
            "notes": notes,
            "updatedAt": utc_now_iso(),
        }
    )
    return _container().replace_item(item=record_id, body=doc)


def rederive_dates(tenant_id: str, type_def: dict[str, Any], limit: int = 2000) -> int:
    """Re-aligns `occurredAt`/`period` of every existing record of a type with
    its `dateField` (used when define_record_type sets or changes it, so old
    records stop sitting in the month they were LOADED instead of the month
    they belong to). Returns how many records changed. Records without a value
    in the date field are left alone."""
    from george import record_schema  # local import: record_schema has no Cosmos deps, keep it that way

    if not type_def.get("dateField"):
        return 0
    docs = list(
        _container().query_items(
            query=f"SELECT TOP {int(limit)} * FROM c WHERE c.docType = 'record' AND c.typeKey = @t",
            parameters=[{"name": "@t", "value": type_def["typeKey"]}],
            partition_key=tenant_id,
        )
    )
    changed = 0
    for doc in docs:
        occurred_at = record_schema.date_field_occurred_at(type_def, doc.get("values") or {})
        if not occurred_at:
            continue
        period = record_schema.derive_period(occurred_at)
        if doc.get("occurredAt") == occurred_at and doc.get("period") == period:
            continue
        doc.update({"occurredAt": occurred_at, "period": period, "updatedAt": utc_now_iso()})
        _container().replace_item(item=doc["id"], body=doc)
        changed += 1
    return changed


def delete_record(tenant_id: str, record_id: str) -> bool:
    try:
        _container().delete_item(item=record_id, partition_key=tenant_id)
        return True
    except exceptions.CosmosResourceNotFoundError:
        return False
