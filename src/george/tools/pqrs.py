"""Tools for PQRS (peticiones, quejas, reclamos, sugerencias). Scoped to
ctx.tenant_id."""
from __future__ import annotations

import json
from typing import Any

from george.repositories import pqrs as pqrs_repo
from george.tools.common import ToolContext, ToolSpec, require_role, require_tenant

_CREATE_ROLES = ("owner", "admin", "walker", "viewer")
_LIST_ROLES = ("owner", "admin")


def _create_pqr(input_: dict[str, Any], ctx: ToolContext) -> str:
    require_role(ctx, _CREATE_ROLES, "create_pqr")
    tenant_id = require_tenant(ctx)
    reported_by = {"chatId": ctx.chat_id, "userName": ctx.user_name, "role": ctx.role}
    doc = pqrs_repo.create_pqr(
        tenant_id,
        ctx.chat_id,
        reported_by,
        input_["type"],
        input_["title"],
        input_["description"],
        severity=input_.get("severity") or "medium",
    )
    return json.dumps({"id": doc["id"], "type": doc["type"], "status": doc["status"]}, ensure_ascii=False)


def _list_pqrs(input_: dict[str, Any], ctx: ToolContext) -> str:
    require_role(ctx, _LIST_ROLES, "list_pqrs")
    tenant_id = require_tenant(ctx)
    results = pqrs_repo.list_pqrs(tenant_id, status=input_.get("status"))
    slim = [
        {
            "id": r["id"],
            "type": r.get("type"),
            "title": r.get("title"),
            "status": r.get("status"),
            "severity": r.get("severity"),
            "createdAt": r.get("createdAt"),
        }
        for r in results
    ]
    return json.dumps({"pqrs": slim}, ensure_ascii=False)


TOOLS = [
    ToolSpec(
        name="create_pqr",
        description=(
            "Registra una peticion, queja, reclamo o sugerencia. Usa esto proactivamente cada vez que detectes "
            "que algo no le cuadra al usuario, reporte un problema, o sugiera una funcionalidad — aunque no te lo pida explicitamente."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["peticion", "queja", "reclamo", "sugerencia", "bug"]},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "severity": {"type": "string", "enum": ["low", "medium", "high"]},
            },
            "required": ["type", "title", "description"],
            "additionalProperties": False,
        },
        allowed_roles=_CREATE_ROLES,
        handler=_create_pqr,
    ),
    ToolSpec(
        name="list_pqrs",
        description="Lista las PQRS de este negocio, opcionalmente filtrando por estado.",
        input_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["open", "in_progress", "resolved", "wont_fix"]}
            },
            "required": [],
            "additionalProperties": False,
        },
        allowed_roles=_LIST_ROLES,
        handler=_list_pqrs,
    ),
]
