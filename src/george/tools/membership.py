"""Tools for a chat to inspect and switch between its own tenant
memberships. Not gated by require_tenant — that's the point: these are how a
chat gets *into* the PENDING_SELECTION_ROLE state's only way out, and also
how a chat with an already-active tenant can switch to a different one it
also belongs to ("cambiemos al otro negocio")."""
from __future__ import annotations

import json
from typing import Any

from george.repositories import chats as chats_repo
from george.repositories import tenants as tenants_repo
from george.roles import BUSINESS_ROLES, PENDING_SELECTION_ROLE, PLATFORM_ADMIN_ROLE
from george.tools.common import ToolContext, ToolError, ToolSpec, require_role

# platform_admin included: a platform_admin chat can also belong to a
# business of its own (dual role) and needs these to move between the two —
# see switch_to_platform_admin below for the way back.
_ROLES = (*BUSINESS_ROLES, PENDING_SELECTION_ROLE, PLATFORM_ADMIN_ROLE)


def _list_my_businesses(_input: dict[str, Any], ctx: ToolContext) -> str:
    require_role(ctx, _ROLES, "list_my_businesses")
    chat = chats_repo.get_chat(ctx.chat_id) or {}
    memberships = chat.get("memberships") or []
    active_tenant_id = chat.get("activeTenantId")

    businesses = []
    for membership in memberships:
        tenant = tenants_repo.get_tenant(membership["tenantId"])
        businesses.append(
            {
                "tenantId": membership["tenantId"],
                "role": membership.get("role"),
                "name": tenant.get("name") if tenant else None,
                "businessType": tenant.get("businessType") if tenant else None,
                "status": tenant.get("status") if tenant else "desconocido",
                "active": membership["tenantId"] == active_tenant_id,
            }
        )
    return json.dumps({"businesses": businesses}, ensure_ascii=False)


def _switch_business(input_: dict[str, Any], ctx: ToolContext) -> str:
    require_role(ctx, _ROLES, "switch_business")
    tenant_id = input_["tenant_id"]
    try:
        chats_repo.set_active_tenant(ctx.chat_id, tenant_id)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    tenant = tenants_repo.get_tenant(tenant_id)
    return json.dumps(
        {"activeTenantId": tenant_id, "name": tenant.get("name") if tenant else None}, ensure_ascii=False
    )


def _switch_to_platform_admin(_input: dict[str, Any], ctx: ToolContext) -> str:
    # Not a plain require_role check: reachability here depends on whether
    # the underlying CHAT is a platform_admin (ctx.is_platform_admin), not on
    # ctx.role — which, while this chat is operating one of its own
    # businesses, is a business role like 'owner', not 'platform_admin'.
    if not ctx.is_platform_admin:
        raise ToolError("Este chat no es administrador de la plataforma.")
    if ctx.role == PLATFORM_ADMIN_ROLE:
        return json.dumps({"ok": True, "note": "Ya estabas en modo plataforma."}, ensure_ascii=False)
    chats_repo.clear_active_tenant(ctx.chat_id)
    return json.dumps({"ok": True}, ensure_ascii=False)


TOOLS = [
    ToolSpec(
        name="list_my_businesses",
        description=(
            "Lista los negocios en los que participa este chat, con el rol que tiene en cada uno y cuál está "
            "activo ahora mismo. Úsala cuando el chat pertenezca a más de un negocio y necesites saber a cuál "
            "cambiar, o para confirmarle al usuario en cuáles trabaja."
        ),
        input_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        allowed_roles=_ROLES,
        handler=_list_my_businesses,
    ),
    ToolSpec(
        name="switch_business",
        description=(
            "Fija cuál de los negocios a los que pertenece este chat es el activo para esta conversación de aquí "
            "en adelante. Usa un tenant_id devuelto por list_my_businesses — nunca inventes uno."
        ),
        input_schema={
            "type": "object",
            "properties": {"tenant_id": {"type": "string"}},
            "required": ["tenant_id"],
            "additionalProperties": False,
        },
        allowed_roles=_ROLES,
        handler=_switch_business,
    ),
    ToolSpec(
        name="switch_to_platform_admin",
        description=(
            "Vuelve al modo administrador de la plataforma. Solo funciona si este chat es platform_admin y "
            "actualmente está operando uno de sus propios negocios (dual role) — para cualquier otro chat, "
            "esta herramienta no aplica."
        ),
        input_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        allowed_roles=(*BUSINESS_ROLES, PLATFORM_ADMIN_ROLE),
        handler=_switch_to_platform_admin,
    ),
]
