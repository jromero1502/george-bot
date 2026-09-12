"""Tools for managing scheduled reminders (recurring via cron, or one-off).
Scoped to ctx.tenant_id — owner/admin can only list/edit/delete their own
tenant's reminders (repositories/reminders.py enforces ownership on write
even though the container's partition key is /id, not /tenantId)."""
from __future__ import annotations

import json
from typing import Any

from george import scheduling
from george.repositories import reminders as reminders_repo
from george.tools.common import ToolContext, ToolError, ToolSpec, require_role, require_tenant

_ROLES = ("owner", "admin")


def _slim(reminder: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": reminder["id"],
        "name": reminder.get("name"),
        "kind": reminder.get("kind"),
        "schedule": reminder.get("schedule"),
        "target": reminder.get("target"),
        "status": reminder.get("status"),
        "nextRunAt": reminder.get("nextRunAt"),
        "lastRunAt": reminder.get("lastRunAt"),
    }


def _list_reminders(input_: dict[str, Any], ctx: ToolContext) -> str:
    require_role(ctx, _ROLES, "list_reminders")
    tenant_id = require_tenant(ctx)
    results = reminders_repo.list_reminders(tenant_id, status=input_.get("status"))
    return json.dumps({"reminders": [_slim(r) for r in results]}, ensure_ascii=False)


def _upsert_reminder(input_: dict[str, Any], ctx: ToolContext) -> str:
    require_role(ctx, _ROLES, "upsert_reminder")
    tenant_id = require_tenant(ctx)

    schedule_in = input_["schedule"]
    schedule = {
        "type": schedule_in["type"],
        "cron": schedule_in.get("cron"),
        "runAt": schedule_in.get("run_at"),
        "timezone": schedule_in.get("timezone") or (ctx.tenant or {}).get("timezone") or "America/Bogota",
    }
    try:
        next_run_at = scheduling.compute_next_run(schedule, after=None)
    except ValueError as exc:
        raise ToolError(f"Horario invalido: {exc}") from exc

    target_in = input_["target"]
    target = {
        "type": target_in["type"],
        "role": target_in.get("role"),
        "chatId": target_in.get("chat_id"),
    }
    if target["type"] == "role" and not target["role"]:
        raise ToolError("target.role es obligatorio cuando target.type es 'role'.")
    if target["type"] == "chat" and not target["chatId"]:
        raise ToolError("target.chat_id es obligatorio cuando target.type es 'chat'.")

    try:
        reminder = reminders_repo.upsert_reminder(
            tenant_id,
            input_.get("reminder_id"),
            ctx.chat_id,
            name=input_["name"],
            kind=input_["kind"],
            body=input_["body"],
            schedule=schedule,
            target=target,
            status=input_.get("status") or "active",
            nextRunAt=next_run_at,
        )
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return json.dumps(_slim(reminder), ensure_ascii=False)


def _delete_reminder(input_: dict[str, Any], ctx: ToolContext) -> str:
    require_role(ctx, _ROLES, "delete_reminder")
    tenant_id = require_tenant(ctx)
    deleted = reminders_repo.delete_reminder(tenant_id, input_["reminder_id"])
    if not deleted:
        raise ToolError(f"No existe un recordatorio con id={input_['reminder_id']!r} en este negocio.")
    return json.dumps({"deleted": True, "id": input_["reminder_id"]})


TOOLS = [
    ToolSpec(
        name="list_reminders",
        description="Lista los recordatorios configurados para este negocio, opcionalmente filtrando por estado.",
        input_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["active", "paused", "completed"]}
            },
            "required": [],
            "additionalProperties": False,
        },
        allowed_roles=_ROLES,
        handler=_list_reminders,
    ),
    ToolSpec(
        name="upsert_reminder",
        description=(
            "Crea o actualiza un recordatorio (omite reminder_id para crear uno nuevo). "
            "kind='message' envia el texto de 'body' tal cual; kind='prompt' usa 'body' como instruccion "
            "para que George redacte el mensaje al momento de enviarlo. "
            "schedule.type='cron' usa una expresion cron estandar (ej. '0 20 * * *' = todos los dias 8pm); "
            "schedule.type='once' usa run_at (fecha-hora ISO) y se marca como completado tras ejecutarse una vez."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "reminder_id": {"type": "string"},
                "name": {"type": "string"},
                "kind": {"type": "string", "enum": ["message", "prompt"]},
                "body": {"type": "string"},
                "schedule": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["cron", "once"]},
                        "cron": {"type": "string", "description": "Requerido si type='cron'"},
                        "run_at": {"type": "string", "description": "ISO 8601, requerido si type='once'"},
                        "timezone": {"type": "string", "description": "IANA tz; por defecto la del negocio"},
                    },
                    "required": ["type"],
                    "additionalProperties": False,
                },
                "target": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["role", "chat"]},
                        "role": {"type": "string", "enum": ["owner", "admin", "walker", "viewer"]},
                        "chat_id": {"type": "string"},
                    },
                    "required": ["type"],
                    "additionalProperties": False,
                },
                "status": {"type": "string", "enum": ["active", "paused"]},
            },
            "required": ["name", "kind", "body", "schedule", "target"],
            "additionalProperties": False,
        },
        allowed_roles=_ROLES,
        handler=_upsert_reminder,
    ),
    ToolSpec(
        name="delete_reminder",
        description="Elimina un recordatorio de este negocio por su id.",
        input_schema={
            "type": "object",
            "properties": {"reminder_id": {"type": "string"}},
            "required": ["reminder_id"],
            "additionalProperties": False,
        },
        allowed_roles=_ROLES,
        handler=_delete_reminder,
    ),
]
