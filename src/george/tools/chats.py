"""Tools for managing a tenant's own team chats. Owner-only. `upsert_chat`
always writes the membership into ctx.tenant_id — an owner can grant or edit
a role only within their own business, never touch another tenant's
membership for that chat (a chat can belong to several tenants; this tool
only ever manages the one the calling owner belongs to)."""
from __future__ import annotations

import json
from typing import Any

from george.repositories import chats as chats_repo
from george.roles import BUSINESS_ROLES
from george.tools.common import ToolContext, ToolError, ToolSpec, require_role, require_tenant

_ROLES = ("owner",)


def _upsert_chat(input_: dict[str, Any], ctx: ToolContext) -> str:
    require_role(ctx, _ROLES, "upsert_chat")
    tenant_id = require_tenant(ctx)

    target_chat_id = input_["chat_id"]
    existing = chats_repo.get_chat(target_chat_id)
    if existing is not None and existing.get("isPlatformAdmin"):
        raise ToolError("Ese chat es un administrador de la plataforma — no se le puede asignar un rol de negocio.")

    role = input_.get("role") or "viewer"
    try:
        result = chats_repo.add_membership(
            target_chat_id,
            tenant_id,
            role,
            name=input_.get("name"),
            status=input_.get("status"),
            timezone=input_.get("timezone"),
        )
    except ValueError as exc:
        raise ToolError(str(exc)) from exc

    membership = chats_repo.get_membership(result, tenant_id)
    return json.dumps(
        {"chatId": target_chat_id, "role": membership["role"] if membership else None, "status": result.get("status")},
        ensure_ascii=False,
    )


TOOLS = [
    ToolSpec(
        name="upsert_chat",
        description=(
            "Da de alta o edita el rol de un miembro del equipo de este negocio: owner, admin, walker o viewer. "
            "Solo el owner puede usar esta herramienta, y solo afecta la membresía de ese chat en ESTE negocio — "
            "si el chat ya trabaja en otro negocio, esa otra membresía no se toca."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "chat_id": {"type": "string"},
                "name": {"type": "string"},
                "role": {"type": "string", "enum": list(BUSINESS_ROLES)},
                "status": {"type": "string", "enum": ["active", "disabled"]},
                "timezone": {"type": "string"},
            },
            "required": ["chat_id"],
            "additionalProperties": False,
        },
        allowed_roles=_ROLES,
        handler=_upsert_chat,
    ),
]
