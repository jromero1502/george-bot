"""Tool for recalling a chat's OWN past conversation turns — e.g. "recupera
la lista del martes". The automatic history fed into every turn
(function_app.py::_history_to_messages) is only the last HISTORY_TURNS
turns, which scrolls past a given day fast on an active chat; this is how
George reaches further back when a human explicitly asks him to.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from george.config import settings
from george.repositories import conversations as conversations_repo
from george.tools.common import ToolContext, ToolError, ToolSpec, require_role, require_tenant

_ROLES = ("owner", "admin")
_MAX_RANGE_DAYS = 31


def _parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise ToolError(f"Fecha invalida: {value!r}. Usa el formato YYYY-MM-DD.") from None


def _day_start_utc(day: date, tz_name: str) -> datetime:
    return datetime.combine(day, time.min, tzinfo=ZoneInfo(tz_name)).astimezone(ZoneInfo("UTC"))


def _recall_chat_history(input_: dict[str, Any], ctx: ToolContext) -> str:
    require_role(ctx, _ROLES, "recall_chat_history")
    tenant_id = require_tenant(ctx)

    since_date = _parse_date(input_["since_date"])
    until_date = _parse_date(input_.get("until_date") or input_["since_date"])
    if until_date < since_date:
        raise ToolError("until_date no puede ser anterior a since_date.")
    if (until_date - since_date).days > _MAX_RANGE_DAYS:
        raise ToolError(f"El rango no puede superar los {_MAX_RANGE_DAYS} dias — pedi algo mas acotado.")

    tz_name = (ctx.tenant or {}).get("timezone") or settings.default_timezone
    since_utc = _day_start_utc(since_date, tz_name)
    until_utc = _day_start_utc(until_date + timedelta(days=1), tz_name)

    docs = conversations_repo.search_by_date(
        ctx.chat_id, tenant_id, since_utc.isoformat(), until_utc.isoformat()
    )
    turns = []
    for doc in docs:
        if doc.get("direction") != "inbound":
            continue
        input_doc = doc.get("input") or {}
        user_text = input_doc.get("transcript") or input_doc.get("text")
        assistant_text = (doc.get("output") or {}).get("text")
        if not user_text and not assistant_text:
            continue
        turns.append({"ts": doc.get("ts"), "user": user_text, "george": assistant_text})

    if not turns:
        return json.dumps(
            {"turns": [], "note": "No hay mensajes de este chat en ese rango de fechas."}, ensure_ascii=False
        )
    return json.dumps({"turns": turns}, ensure_ascii=False)


TOOLS = [
    ToolSpec(
        name="recall_chat_history",
        description=(
            "Trae los mensajes reales (tuyos y del usuario) de ESTE chat en una fecha o rango de fechas pasado "
            "— usala cuando el usuario te pida recuperar, revisar o continuar algo de un dia anterior que ya no "
            "aparece en tu historial normal de la conversacion (ej. 'recupera la lista del martes', 'que paso el "
            "lunes con...'). Devuelve el texto tal cual se dijo en ese momento; no inventes ni asumas nada que no "
            "este ahi textualmente. Despues de leerlo, cruzalo con search_records/search_finance/summarize_records "
            "segun corresponda para ver que de eso quedo efectivamente guardado y que no, mostrale al usuario lo "
            "que falta, y recien despues de que confirme cargalo (log_record, create_charge, etc.) de a uno."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "since_date": {"type": "string", "description": "YYYY-MM-DD, en la zona horaria del negocio"},
                "until_date": {"type": "string", "description": "YYYY-MM-DD; omite para un solo dia"},
            },
            "required": ["since_date"],
            "additionalProperties": False,
        },
        allowed_roles=_ROLES,
        handler=_recall_chat_history,
    ),
]
