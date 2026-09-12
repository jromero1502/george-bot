"""`finance` container — one document per financial movement (charge,
payment, adjustment, expense). Partition key: /tenantId.
"""
from __future__ import annotations

from typing import Any, Optional

from azure.cosmos import exceptions

from george.repositories.cosmos import get_container
from george.util import new_id, utc_now_iso


def _container():
    return get_container("finance")


def get_movement(tenant_id: str, movement_id: str) -> Optional[dict[str, Any]]:
    try:
        return _container().read_item(item=movement_id, partition_key=tenant_id)
    except exceptions.CosmosResourceNotFoundError:
        return None


def create_charge(
    tenant_id: str,
    client_id: str,
    client_name: str,
    created_by: str,
    amount: float,
    concept: str,
    currency: str = "COP",
    **fields: Any,
) -> dict[str, Any]:
    doc = {
        "id": new_id(),
        "tenantId": tenant_id,
        "clientId": client_id,
        "clientName": client_name,
        "createdAt": utc_now_iso(),
        "createdBy": created_by,
        "type": "charge",
        "amount": amount,
        "balance": amount,
        "status": "pending",
        "currency": currency,
        "concept": concept,
        "comments": [],
        "items": [],
        "custom": {},
        **fields,
    }
    return _container().create_item(doc)


def create_expense(
    tenant_id: str,
    created_by: str,
    amount: float,
    concept: str,
    currency: str = "COP",
    **fields: Any,
) -> dict[str, Any]:
    """A business expense — unlike a charge/payment, never tied to a client
    (rent, supplies, wages, ...). `clientId`/`clientName` stay None rather
    than absent so callers that index r["clientId"] (e.g. search_finance's
    slim projection) don't need a special case for this movement type."""
    doc = {
        "id": new_id(),
        "tenantId": tenant_id,
        "clientId": None,
        "clientName": None,
        "createdAt": utc_now_iso(),
        "createdBy": created_by,
        "type": "expense",
        "amount": amount,
        "balance": 0,
        "status": "paid",
        "currency": currency,
        "concept": concept,
        "comments": [],
        "custom": {},
        **fields,
    }
    return _container().create_item(doc)


def register_payment(
    tenant_id: str,
    client_id: str,
    client_name: str,
    created_by: str,
    amount: float,
    applies_to: list[str],
    currency: str = "COP",
    **fields: Any,
) -> dict[str, Any]:
    """Records a payment and applies it against the given charge IDs (oldest
    first if `applies_to` is empty — otherwise exactly the charges listed),
    reducing each charge's remaining balance."""
    remaining = amount
    container = _container()

    targets = applies_to
    if not targets:
        pending = search_finance(
            tenant_id, client_id=client_id, type_="charge", status_in=("pending", "partial")
        )
        pending.sort(key=lambda c: c.get("dueDate") or c.get("createdAt") or "")
        targets = [c["id"] for c in pending]

    applied_ids: list[str] = []
    for charge_id in targets:
        if remaining <= 0:
            break
        charge = get_movement(tenant_id, charge_id)
        if charge is None or charge.get("type") != "charge":
            continue
        pay = min(remaining, charge.get("balance", 0))
        if pay <= 0:
            continue
        charge["balance"] = round(charge.get("balance", 0) - pay, 2)
        charge["status"] = "paid" if charge["balance"] <= 0 else "partial"
        if charge["status"] == "paid":
            charge["paidAt"] = utc_now_iso()
        charge["updatedAt"] = utc_now_iso()
        container.upsert_item(charge)
        applied_ids.append(charge_id)
        remaining = round(remaining - pay, 2)

    payment_doc = {
        "id": new_id(),
        "tenantId": tenant_id,
        "clientId": client_id,
        "clientName": client_name,
        "createdAt": utc_now_iso(),
        "createdBy": created_by,
        "type": "payment",
        "amount": amount,
        "balance": 0,
        "status": "paid",
        "currency": currency,
        "appliesTo": applied_ids,
        "comments": [],
        "custom": {},
        **fields,
    }
    return container.create_item(payment_doc)


def search_finance(
    tenant_id: str,
    client_id: Optional[str] = None,
    type_: Optional[str] = None,
    status_in: Optional[tuple[str, ...]] = None,
    period: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    clauses = []
    params: list[dict[str, Any]] = []

    if client_id:
        clauses.append("c.clientId = @clientId")
        params.append({"name": "@clientId", "value": client_id})
    if type_:
        clauses.append("c.type = @type")
        params.append({"name": "@type", "value": type_})
    if status_in:
        clauses.append("ARRAY_CONTAINS(@statuses, c.status)")
        params.append({"name": "@statuses", "value": list(status_in)})
    if period:
        clauses.append("c.period = @period")
        params.append({"name": "@period", "value": period})

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    query = f"SELECT TOP {int(limit)} * FROM c {where} ORDER BY c.createdAt DESC"

    return list(
        _container().query_items(query=query, parameters=params, partition_key=tenant_id)
    )


def get_client_balance(tenant_id: str, client_id: str) -> float:
    query = """
    SELECT VALUE SUM(c.balance) FROM c
    WHERE c.clientId = @clientId AND c.type = 'charge' AND c.status != 'void'
    """
    result = list(
        _container().query_items(
            query=query,
            parameters=[{"name": "@clientId", "value": client_id}],
            partition_key=tenant_id,
        )
    )
    return result[0] if result and result[0] is not None else 0.0


def add_comment(tenant_id: str, movement_id: str, by: str, text: str) -> dict[str, Any]:
    movement = get_movement(tenant_id, movement_id)
    if movement is None:
        raise ValueError(f"Unknown finance movement {movement_id!r} for this tenant")
    movement.setdefault("comments", []).append({"by": by, "at": utc_now_iso(), "text": text})
    movement["updatedAt"] = utc_now_iso()
    return _container().upsert_item(movement)
