"""Tools del flujo propón -> confirma -> ejecuta (ver george/plan.py)."""
from __future__ import annotations

import json
from typing import Any

from george.repositories import plans
from george.roles import BUSINESS_ROLES, PLATFORM_ADMIN_ROLE
from george.tools.common import ToolContext, ToolError, ToolSpec, require_role

_ROLES = (*BUSINESS_ROLES, PLATFORM_ADMIN_ROLE)


def _propose_actions(input_: dict[str, Any], ctx: ToolContext) -> str:
    from george import plan  # import tardío: plan.py importa el registry, que importa este módulo

    require_role(ctx, _ROLES, "propose_actions")
    summary = (input_.get("summary") or "").strip()
    if not summary:
        raise ToolError("Falta 'summary': qué vas a hacer, en una frase.")
    try:
        actions = plan.validate_actions(input_.get("actions"), ctx)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    plans.save_plan(ctx.chat_id, ctx.tenant_id, ctx.role, ctx.user_name, summary[:500], actions)
    return json.dumps(
        {
            "saved": True,
            "executed": False,
            "actions": len(actions),
            "next": (
                "NADA se ha ejecutado. Ahora cuéntale al usuario, en tu respuesta, qué vas a hacer (cada acción "
                "con sus datos clave: montos, fechas, nombres) y pregúntale si lo ejecutas. No digas que ya quedó hecho."
            ),
        }
    )


def _confirm_pending(input_: dict[str, Any], ctx: ToolContext) -> str:
    require_role(ctx, _ROLES, "confirm_pending")
    current = plans.get_plan(ctx.chat_id)
    if current is None or plans.is_expired(current) and current.get("status") == "pending":
        raise ToolError("No hay ningún plan pendiente (o venció). Propón las acciones de nuevo.")
    if current.get("tenantId") != ctx.tenant_id:
        raise ToolError("El plan pendiente es de otro negocio. Propón las acciones de nuevo.")
    status = current.get("status")
    if status in ("confirmed", "running"):  # reintento del mismo turno: idempotente
        return json.dumps({"confirmed": True, "next": "Responde que lo estás ejecutando; el resultado llega en otro mensaje."})
    if status != "pending":
        raise ToolError("Ese plan ya se ejecutó o se canceló. Propón las acciones de nuevo si hace falta.")
    if plans.transition(current, "confirmed") is None:
        raise ToolError("El plan cambió mientras lo confirmabas. Revisa y vuelve a intentarlo.")
    return json.dumps(
        {
            "confirmed": True,
            "next": (
                "Quedó en cola. Responde SOLO con un acuse breve ('Listo, lo ejecuto') — el resultado real "
                "llega en otro mensaje cuando termine. No afirmes que ya está hecho."
            ),
        }
    )


def _cancel_pending(input_: dict[str, Any], ctx: ToolContext) -> str:
    require_role(ctx, _ROLES, "cancel_pending")
    current = plans.get_pending_plan(ctx.chat_id)
    if current is None:
        raise ToolError("No hay ningún plan pendiente.")
    plans.transition(current, "cancelled")
    return json.dumps({"cancelled": True})


_EMPTY = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}

TOOLS = [
    ToolSpec(
        name="propose_actions",
        description=(
            "Propone un plan de ESCRITURAS (las herramientas que crean, cambian o borran datos, o actúan en Gmail/"
            "Calendar/Drive) para que el usuario lo confirme. No ejecuta nada. Incluye TODAS las acciones del pedido en "
            "una sola llamada. Cada acción lleva la herramienta, su input completo (mismos campos que su definición) y un "
            "label corto para el usuario. Las acciones no pueden depender del resultado de otra del mismo plan (p. ej. "
            "un id recién creado): si hace falta, propón en dos pasos. Reemplaza cualquier plan pendiente anterior."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Qué vas a hacer, en una frase."},
                "actions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool": {"type": "string", "description": "Nombre de la herramienta de escritura."},
                            "input": {"type": "object", "description": "Input completo de esa herramienta."},
                            "label": {"type": "string", "description": "Descripción corta para el usuario."},
                        },
                        "required": ["tool", "input", "label"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["summary", "actions"],
            "additionalProperties": False,
        },
        allowed_roles=_ROLES,
        handler=_propose_actions,
    ),
    ToolSpec(
        name="confirm_pending",
        description=(
            "Confirma el plan pendiente y lo manda a ejecutar. Llámala SOLO si el mensaje actual del usuario aprueba "
            "explícitamente ese plan. Nunca en el mismo turno en que lo propones."
        ),
        input_schema=_EMPTY,
        allowed_roles=_ROLES,
        handler=_confirm_pending,
    ),
    ToolSpec(
        name="cancel_pending",
        description="Descarta el plan pendiente cuando el usuario lo rechaza o ya no lo quiere.",
        input_schema=_EMPTY,
        allowed_roles=_ROLES,
        handler=_cancel_pending,
    ),
]
