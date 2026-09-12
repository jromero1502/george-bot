"""`reminders` container — recurring (cron) and one-off reminders, both
fixed (seeded per-tenant on tenant creation) and user-defined via chat.
Partition key: /id — each reminder is its own logical partition, because the
timer-triggered scheduler scans due reminders *across every tenant* in one
query (see get_due), so there's no single tenant partition to scope that scan
to. Per-tenant isolation for the "list/edit/delete my reminders" tools is
enforced explicitly here instead (tenantId filter on list, ownership check
before mutating).
"""
from __future__ import annotations

from typing import Any, Optional

from azure.core import MatchConditions
from azure.cosmos import exceptions

from george.repositories.cosmos import get_container
from george.util import new_id, utc_now_iso

VALID_KINDS = ("message", "prompt")
VALID_TARGET_TYPES = ("role", "chat")


def _container():
    return get_container("reminders")


def get_reminder(reminder_id: str) -> Optional[dict[str, Any]]:
    try:
        return _container().read_item(item=reminder_id, partition_key=reminder_id)
    except exceptions.CosmosResourceNotFoundError:
        return None


def get_due(now_iso: str, limit: int = 50) -> list[dict[str, Any]]:
    """Due reminders across *all* tenants — the timer scheduler serves the
    whole deployment, not one tenant."""
    query = f"""
    SELECT TOP {int(limit)} * FROM c
    WHERE c.status = 'active' AND c.nextRunAt <= @now
    ORDER BY c.nextRunAt ASC
    """
    return list(
        _container().query_items(
            query=query,
            parameters=[{"name": "@now", "value": now_iso}],
            enable_cross_partition_query=True,
        )
    )


def list_reminders(tenant_id: str, status: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
    clauses = ["c.tenantId = @tenantId"]
    params = [{"name": "@tenantId", "value": tenant_id}]
    if status:
        clauses.append("c.status = @status")
        params.append({"name": "@status", "value": status})
    query = f"SELECT TOP {int(limit)} * FROM c WHERE {' AND '.join(clauses)} ORDER BY c.nextRunAt ASC"
    return list(
        _container().query_items(query=query, parameters=params, enable_cross_partition_query=True)
    )


def upsert_reminder(tenant_id: str, reminder_id: Optional[str], created_by: str, **fields: Any) -> dict[str, Any]:
    existing = get_reminder(reminder_id) if reminder_id else None
    if existing is not None and existing.get("tenantId") != tenant_id:
        raise ValueError(f"No existe un recordatorio con id={reminder_id!r} en este negocio.")

    reminder_id = reminder_id or new_id()
    doc = existing or {
        "id": reminder_id,
        "tenantId": tenant_id,
        "createdAt": utc_now_iso(),
        "createdBy": created_by,
        "status": "active",
        "runCount": 0,
        "failureCount": 0,
        "lastRunAt": None,
        "lastStatus": None,
        "system": False,
        "payload": {},
        "custom": {},
    }
    doc.update({k: v for k, v in fields.items() if v is not None})
    doc["updatedAt"] = utc_now_iso()
    return _container().upsert_item(doc)


def delete_reminder(tenant_id: str, reminder_id: str) -> bool:
    """Returns False both when the reminder doesn't exist and when it
    belongs to a different tenant — the caller shouldn't be able to tell
    those two cases apart."""
    reminder = get_reminder(reminder_id)
    if reminder is None or reminder.get("tenantId") != tenant_id:
        return False
    try:
        _container().delete_item(item=reminder_id, partition_key=reminder_id)
        return True
    except exceptions.CosmosResourceNotFoundError:
        return False


def try_claim_and_advance(reminder: dict[str, Any], updates: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Optimistic-concurrency update used by the timer-triggered scheduler:
    only one caller can successfully advance a given reminder's nextRunAt for
    a given firing. Returns the updated document, or None if another writer
    got there first (the reminder's ETag no longer matches)."""
    etag = reminder.get("_etag")
    merged = {**reminder, **updates, "updatedAt": utc_now_iso()}
    try:
        return _container().replace_item(
            item=reminder["id"],
            body=merged,
            etag=etag,
            match_condition=MatchConditions.IfNotModified,
        )
    except exceptions.CosmosAccessConditionFailedError:
        return None


def record_run_result(reminder_id: str, ok: bool) -> Optional[dict[str, Any]]:
    reminder = get_reminder(reminder_id)
    if reminder is None:
        return None
    reminder["lastRunAt"] = utc_now_iso()
    reminder["lastStatus"] = "ok" if ok else "error"
    reminder["runCount"] = reminder.get("runCount", 0) + (1 if ok else 0)
    reminder["failureCount"] = reminder.get("failureCount", 0) + (0 if ok else 1)
    reminder["updatedAt"] = utc_now_iso()
    return _container().upsert_item(reminder)
