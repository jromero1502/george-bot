"""Owner-managed business rules (extra system-prompt text for ONE tenant).
Validation and limits live in george/business_rules.py; this module is the only
writer. Owner-only: admins and walkers see the rules (they're in the prompt)
but can't change them. The rules a tenant has right now are already in George's
prompt, so there is deliberately no 'list' tool.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from george import business_rules
from george.repositories import tenants as tenants_repo
from george.tools.common import ToolContext, ToolError, ToolSpec, require_role, require_tenant
from george.util import utc_now_iso

_ROLES = ("owner",)


def _apply(ctx: ToolContext, tool: str, mutate) -> list[dict[str, Any]]:
    require_role(ctx, _ROLES, tool)
    tenant_id = require_tenant(ctx)
    try:
        return tenants_repo.update_business_rules(tenant_id, mutate)
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    except LookupError as exc:
        raise ToolError(str(exc)) from None
    except Exception as exc:  # noqa: BLE001 — a lost ETag race twice in a row
        if type(exc).__name__ == "CosmosAccessConditionFailedError":
            raise ToolError("Otra persona editó las reglas al mismo tiempo. Intenta de nuevo.") from None
        raise


def _result(rules: list[dict[str, Any]], **extra: Any) -> str:
    return json.dumps(
        {**extra, "usage": business_rules.usage(r.get("text", "") for r in rules)}, ensure_ascii=False
    )


def _find(rules: list[dict[str, Any]], rule_id: str) -> dict[str, Any]:
    for rule in rules:
        if rule.get("id") == rule_id:
            return rule
    raise ValueError(f"No existe una regla con id '{rule_id}'.")


def _add(input_: dict[str, Any], ctx: ToolContext) -> str:
    text = None
    new_id = uuid.uuid4().hex[:6]

    def mutate(rules):
        nonlocal text
        text = business_rules.validate_rule(input_.get("text", ""))
        business_rules.check_capacity((r.get("text", "") for r in rules), text)
        return [*rules, {"id": new_id, "text": text, "createdAt": utc_now_iso(), "createdBy": ctx.chat_id}]

    rules = _apply(ctx, "add_business_rule", mutate)
    return _result(rules, added={"id": new_id, "text": text})


def _update(input_: dict[str, Any], ctx: ToolContext) -> str:
    def mutate(rules):
        rule = _find(rules, input_.get("rule_id", ""))
        text = business_rules.validate_rule(input_.get("text", ""))
        business_rules.check_capacity((r.get("text", "") for r in rules), text, replacing=rule["text"])
        return [
            {**r, "text": text, "updatedAt": utc_now_iso(), "updatedBy": ctx.chat_id} if r is rule else r
            for r in rules
        ]

    rules = _apply(ctx, "update_business_rule", mutate)
    return _result(rules, updated=input_["rule_id"])


def _delete(input_: dict[str, Any], ctx: ToolContext) -> str:
    def mutate(rules):
        rule = _find(rules, input_.get("rule_id", ""))
        return [r for r in rules if r is not rule]

    rules = _apply(ctx, "delete_business_rule", mutate)
    return _result(rules, deleted=input_["rule_id"])


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


_TEXT = {
    "type": "string",
    "description": (
        f"La regla en una o dos frases, máximo {business_rules.MAX_RULE_CHARS} caracteres, tal como la dijo el "
        "dueño. Una sola idea por regla."
    ),
}

TOOLS = [
    ToolSpec(
        name="add_business_rule",
        description=(
            "Agrega una regla propia del negocio (preferencia de operación o estilo) que George seguirá en TODAS "
            f"las conversaciones de este negocio. Límite: {business_rules.MAX_RULES} reglas y "
            f"{business_rules.MAX_TOTAL_CHARS} caracteres en total. No puede cambiar permisos ni acceso a datos. "
            "Solo úsala cuando el dueño lo pida explícitamente en su mensaje actual."
        ),
        input_schema=_schema({"text": _TEXT}, ["text"]),
        allowed_roles=_ROLES,
        handler=_add,
    ),
    ToolSpec(
        name="update_business_rule",
        description="Reemplaza el texto de una regla existente (rule_id es el que aparece entre corchetes en el prompt).",
        input_schema=_schema({"rule_id": {"type": "string"}, "text": _TEXT}, ["rule_id", "text"]),
        allowed_roles=_ROLES,
        handler=_update,
    ),
    ToolSpec(
        name="delete_business_rule",
        description="Elimina una regla del negocio por su rule_id. Confirma con el dueño cuál antes de borrar.",
        input_schema=_schema({"rule_id": {"type": "string"}}, ["rule_id"]),
        allowed_roles=_ROLES,
        handler=_delete,
    ),
]
