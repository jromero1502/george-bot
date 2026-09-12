"""Tools for financial movements (charges, payments, comments). Scoped to
ctx.tenant_id — see clients.py for why that's also the Cosmos partition key.
"""
from __future__ import annotations

import json
from typing import Any

from george.repositories import clients as clients_repo
from george.repositories import finance as finance_repo
from george.tools.common import ToolContext, ToolError, ToolSpec, require_role, require_tenant

_ROLES = ("owner", "admin")


def _tenant_currency(ctx: ToolContext) -> str:
    return (ctx.tenant or {}).get("currency", "COP")


def _client_name(tenant_id: str, client_id: str) -> str:
    client = clients_repo.get_client(tenant_id, client_id)
    if client is None:
        raise ToolError(f"No existe un cliente con clientId={client_id!r}. Búscalo primero con search_clients.")
    return client.get("name", client_id)


def _search_finance(input_: dict[str, Any], ctx: ToolContext) -> str:
    require_role(ctx, _ROLES, "search_finance")
    tenant_id = require_tenant(ctx)
    results = finance_repo.search_finance(
        tenant_id,
        client_id=input_.get("client_id"),
        type_=input_.get("type"),
        status_in=tuple(input_["statuses"]) if input_.get("statuses") else None,
        period=input_.get("period"),
        limit=25,
    )
    slim = [
        {
            "id": r["id"],
            "clientId": r["clientId"],
            "clientName": r.get("clientName"),
            "type": r.get("type"),
            "amount": r.get("amount"),
            "balance": r.get("balance"),
            "status": r.get("status"),
            "period": r.get("period"),
            "concept": r.get("concept"),
            "createdAt": r.get("createdAt"),
        }
        for r in results
    ]
    return json.dumps({"movements": slim}, ensure_ascii=False)


def _create_charge(input_: dict[str, Any], ctx: ToolContext) -> str:
    require_role(ctx, _ROLES, "create_charge")
    tenant_id = require_tenant(ctx)
    client_id = input_["client_id"]
    name = _client_name(tenant_id, client_id)
    extra = {}
    if input_.get("period"):
        extra["period"] = input_["period"]
    if input_.get("due_date"):
        extra["dueDate"] = input_["due_date"]
    if input_.get("description"):
        extra["description"] = input_["description"]
    charge = finance_repo.create_charge(
        tenant_id,
        client_id,
        name,
        ctx.chat_id,
        amount=input_["amount"],
        concept=input_["concept"],
        currency=_tenant_currency(ctx),
        **extra,
    )
    return json.dumps(
        {"id": charge["id"], "clientId": client_id, "amount": charge["amount"], "status": charge["status"]},
        ensure_ascii=False,
    )


def _create_expense(input_: dict[str, Any], ctx: ToolContext) -> str:
    require_role(ctx, _ROLES, "create_expense")
    tenant_id = require_tenant(ctx)
    extra = {}
    if input_.get("period"):
        extra["period"] = input_["period"]
    if input_.get("description"):
        extra["description"] = input_["description"]
    expense = finance_repo.create_expense(
        tenant_id,
        ctx.chat_id,
        amount=input_["amount"],
        concept=input_["concept"],
        currency=_tenant_currency(ctx),
        **extra,
    )
    return json.dumps({"id": expense["id"], "amount": expense["amount"], "concept": expense["concept"]}, ensure_ascii=False)


def _register_payment(input_: dict[str, Any], ctx: ToolContext) -> str:
    require_role(ctx, _ROLES, "register_payment")
    tenant_id = require_tenant(ctx)
    client_id = input_["client_id"]
    name = _client_name(tenant_id, client_id)
    extra = {}
    if input_.get("payment_method"):
        extra["paymentMethod"] = input_["payment_method"]
    payment = finance_repo.register_payment(
        tenant_id,
        client_id,
        name,
        ctx.chat_id,
        amount=input_["amount"],
        applies_to=input_.get("applies_to") or [],
        currency=_tenant_currency(ctx),
        **extra,
    )
    remaining_balance = finance_repo.get_client_balance(tenant_id, client_id)
    return json.dumps(
        {
            "id": payment["id"],
            "clientId": client_id,
            "amount": payment["amount"],
            "appliedTo": payment.get("appliesTo", []),
            "remainingBalance": remaining_balance,
        },
        ensure_ascii=False,
    )


def _get_client_balance(input_: dict[str, Any], ctx: ToolContext) -> str:
    require_role(ctx, _ROLES, "get_client_balance")
    tenant_id = require_tenant(ctx)
    client_id = input_["client_id"]
    name = _client_name(tenant_id, client_id)
    balance = finance_repo.get_client_balance(tenant_id, client_id)
    return json.dumps(
        {"clientId": client_id, "clientName": name, "balance": balance, "currency": _tenant_currency(ctx)}
    )


def _add_finance_comment(input_: dict[str, Any], ctx: ToolContext) -> str:
    require_role(ctx, _ROLES, "add_finance_comment")
    tenant_id = require_tenant(ctx)
    try:
        movement = finance_repo.add_comment(tenant_id, input_["movement_id"], ctx.chat_id, input_["text"])
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return json.dumps({"id": movement["id"], "comments": len(movement.get("comments", []))})


TOOLS = [
    ToolSpec(
        name="search_finance",
        description="Busca movimientos financieros (cargos, pagos, ajustes) opcionalmente filtrando por cliente, tipo, estado o periodo (YYYY-MM).",
        input_schema={
            "type": "object",
            "properties": {
                "client_id": {"type": "string"},
                "type": {"type": "string", "enum": ["charge", "payment", "adjustment", "expense"]},
                "statuses": {"type": "array", "items": {"type": "string", "enum": ["pending", "partial", "paid", "void"]}},
                "period": {"type": "string", "description": "Formato YYYY-MM"},
            },
            "required": [],
            "additionalProperties": False,
        },
        allowed_roles=_ROLES,
        handler=_search_finance,
    ),
    ToolSpec(
        name="create_charge",
        description="Registra un cargo/cobro pendiente para un cliente (ej. plan mensual, servicio puntual).",
        input_schema={
            "type": "object",
            "properties": {
                "client_id": {"type": "string"},
                "amount": {"type": "number", "description": "Monto en la moneda del negocio"},
                "concept": {"type": "string"},
                "period": {"type": "string", "description": "YYYY-MM si aplica a un periodo"},
                "due_date": {"type": "string", "description": "Fecha limite YYYY-MM-DD"},
                "description": {"type": "string"},
            },
            "required": ["client_id", "amount", "concept"],
            "additionalProperties": False,
        },
        allowed_roles=_ROLES,
        handler=_create_charge,
    ),
    ToolSpec(
        name="create_expense",
        description=(
            "Registra un gasto operativo del negocio (alquiler, insumos, sueldos, etc.) — a diferencia de "
            "create_charge, NUNCA va ligado a un cliente, solo a un concepto y un monto. No afecta el saldo de "
            "ningun cliente."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "Monto en la moneda del negocio"},
                "concept": {"type": "string", "description": "Ej. 'Alquiler local', 'Compra de insumos'"},
                "period": {"type": "string", "description": "YYYY-MM si aplica a un periodo"},
                "description": {"type": "string"},
            },
            "required": ["amount", "concept"],
            "additionalProperties": False,
        },
        allowed_roles=_ROLES,
        handler=_create_expense,
    ),
    ToolSpec(
        name="register_payment",
        description="Registra un pago de un cliente y lo aplica a sus cargos pendientes (los mas antiguos primero, salvo que se indiquen applies_to).",
        input_schema={
            "type": "object",
            "properties": {
                "client_id": {"type": "string"},
                "amount": {"type": "number"},
                "applies_to": {"type": "array", "items": {"type": "string"}, "description": "IDs de cargos especificos a abonar"},
                "payment_method": {"type": "string"},
            },
            "required": ["client_id", "amount"],
            "additionalProperties": False,
        },
        allowed_roles=_ROLES,
        handler=_register_payment,
    ),
    ToolSpec(
        name="get_client_balance",
        description="Consulta el saldo pendiente total de un cliente.",
        input_schema={
            "type": "object",
            "properties": {"client_id": {"type": "string"}},
            "required": ["client_id"],
            "additionalProperties": False,
        },
        allowed_roles=_ROLES,
        handler=_get_client_balance,
    ),
    ToolSpec(
        name="add_finance_comment",
        description="Agrega un comentario a un movimiento financiero existente (ej. 'pidio prorroga hasta el 20').",
        input_schema={
            "type": "object",
            "properties": {
                "movement_id": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["movement_id", "text"],
            "additionalProperties": False,
        },
        allowed_roles=_ROLES,
        handler=_add_finance_comment,
    ),
]
