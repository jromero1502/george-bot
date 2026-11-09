"""Flujo propón -> confirma -> ejecuta.

En el chat, el modelo solo puede LEER y PROPONER. Las escrituras no se
ejecutan en el turno de conversación: se guardan como un plan
(`propose_actions`), el usuario lo confirma en su siguiente mensaje
(`confirm_pending`) y un worker de cola (`function_app.action_worker`) las
corre con `run_plan`, sin modelo de por medio. Así "George dijo que lo hizo
pero no llamó la tool" deja de ser posible: lo que se ejecutó es exactamente
lo que quedó guardado en el plan, y el resultado que ve el usuario sale de lo
que las tools devolvieron, no de lo que el modelo diga.
"""
from __future__ import annotations

import re
import time
from typing import Any

from george.tools import registry
from george.tools.common import ToolContext

# Tools que cambian datos o hablan con servicios externos: solo corren vía plan.
WRITE_TOOLS = frozenset(
    {
        "add_business_rule", "update_business_rule", "delete_business_rule",
        "upsert_chat", "upsert_client", "upsert_item",
        "gmail_attachment_to_drive", "gmail_save_email_to_drive", "drive_create_folder",
        "create_charge", "create_expense", "register_payment", "add_finance_comment", "delete_finance_entry",
        "calendar_create_event", "calendar_update_event", "calendar_delete_event",
        "create_pqr", "define_record_type", "log_record", "update_record", "delete_record",
        "upsert_reminder", "delete_reminder",
        "create_tenant", "add_chat_to_tenant", "set_default_reminder_templates",
    }
)

# Meta-tools del flujo: inmediatas, pero no son proponibles dentro de un plan.
PLAN_TOOLS = frozenset({"propose_actions", "confirm_pending", "cancel_pending"})

# Inmediatas: lectura, o efectos que deben ocurrir en el turno (cambiar de
# negocio refresca el contexto a mitad de turno; drive_send_file entrega el
# archivo a este mismo chat y no cambia datos).
IMMEDIATE_TOOLS = frozenset(
    {
        "search_clients", "get_client", "drive_search", "drive_read", "drive_send_file",
        "search_finance", "get_client_balance", "gmail_search", "gmail_read", "calendar_list_events",
        "recall_chat_history", "list_my_businesses", "switch_business", "switch_to_platform_admin",
        "list_pqrs", "list_record_types", "search_records", "summarize_records", "list_reminders",
        "list_tenants", "get_chat_memberships", "get_default_reminder_templates", "get_current_datetime",
    }
) | PLAN_TOOLS

MAX_ACTIONS = 25
_URL_RE = re.compile(r"https?://[^\s\"'<>\\]+")


def format_plan_for_prompt(plan: dict[str, Any]) -> str:
    lines = [f"{i}. {a.get('label') or a['tool']}  [{a['tool']}]" for i, a in enumerate(plan["actions"], 1)]
    return (
        "\n\n## Plan pendiente de confirmación (propuesto por ti, NO ejecutado todavía)\n"
        f"{plan.get('summary', '')}\n" + "\n".join(lines) + "\n"
        "Si el usuario lo aprueba en su mensaje actual (sí, dale, ok, adelante...), llama confirm_pending. Si lo "
        "rechaza, llama cancel_pending. Si lo corrige o añade algo, llama propose_actions con el plan completo "
        "actualizado (reemplaza este). Si habla de otra cosa, déjalo pendiente y atiende lo que pide."
    )


def validate_actions(actions: Any, ctx: ToolContext) -> list[dict[str, Any]]:
    """Devuelve las acciones normalizadas o lanza ValueError con un mensaje
    para el modelo. El permiso real se vuelve a comprobar al ejecutar (cada
    handler llama require_role/require_tenant)."""
    if not isinstance(actions, list) or not actions:
        raise ValueError("'actions' debe ser una lista con al menos una acción.")
    if len(actions) > MAX_ACTIONS:
        raise ValueError(f"Máximo {MAX_ACTIONS} acciones por plan; divídelo en dos.")
    clean: list[dict[str, Any]] = []
    for i, action in enumerate(actions, 1):
        if not isinstance(action, dict):
            raise ValueError(f"Acción {i}: formato inválido.")
        name, tool_input, label = action.get("tool"), action.get("input"), action.get("label")
        if name not in WRITE_TOOLS:
            hint = "Es de consulta: llámala directamente, no va en el plan." if name in IMMEDIATE_TOOLS else "No existe."
            raise ValueError(f"Acción {i}: '{name}' no se puede proponer. {hint}")
        spec = registry._BY_NAME.get(name)
        if spec is None:
            raise ValueError(f"Acción {i}: la herramienta '{name}' no existe.")
        if ctx.role not in spec.allowed_roles:
            raise ValueError(f"Acción {i}: tu rol ({ctx.role}) no puede usar '{name}'.")
        if not isinstance(tool_input, dict):
            raise ValueError(f"Acción {i} ({name}): 'input' debe ser un objeto.")
        missing = [k for k in spec.input_schema.get("required", []) if k not in tool_input]
        if missing:
            raise ValueError(f"Acción {i} ({name}): faltan campos obligatorios: {', '.join(missing)}.")
        if not isinstance(label, str) or not label.strip():
            raise ValueError(f"Acción {i} ({name}): falta 'label' (descripción corta para el usuario).")
        clean.append({"tool": name, "input": tool_input, "label": label.strip()[:200]})
    return clean


def run_plan(plan: dict[str, Any], ctx: ToolContext) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Ejecuta las acciones en orden con registry.dispatch (sin modelo). Sigue
    aunque una falle: las acciones del plan son independientes entre sí.
    Devuelve (resultados para el mensaje, entradas de auditoría tipo toolCalls)."""
    results: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    for action in plan["actions"]:
        start = time.monotonic()
        try:
            text, is_error = registry.dispatch(action["tool"], action["input"], ctx)
        except Exception as exc:  # noqa: BLE001 - dispatch no debería lanzar, pero un plan no se corta por esto
            text, is_error = f"Error inesperado: {exc}", True
        audit.append(
            {
                "name": action["tool"],
                "input": action["input"],
                "ok": not is_error,
                "resultSummary": text[:500],
                "latencyMs": round((time.monotonic() - start) * 1000, 1),
                "error": text if is_error else None,
            }
        )
        results.append({"label": action["label"], "ok": not is_error, "text": text})
    return results, audit


def format_results(results: list[dict[str, Any]]) -> str:
    """Mensaje final para el usuario, armado solo con lo que devolvieron las tools."""
    ok = sum(1 for r in results if r["ok"])
    if ok == len(results):
        head = "✅ Listo, esto es lo que quedó hecho:" if len(results) > 1 else "✅ Listo:"
    elif ok == 0:
        head = "❌ No pude ejecutar nada:"
    else:
        head = f"⚠️ Ejecuté {ok} de {len(results)} acciones:"
    lines = []
    for r in results:
        if r["ok"]:
            links = list(dict.fromkeys(_URL_RE.findall(r["text"])))[:2]
            lines.append(f"✅ {r['label']}" + "".join(f"\n   {u}" for u in links))
        else:
            lines.append(f"❌ {r['label']}: {r['text'][:300]}")
    return head + "\n" + "\n".join(lines)
