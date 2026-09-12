"""`platformConfig` container — small, singleton platform-level settings,
managed by the platform_admin. Partition key: /id, but there's only ever one
document in practice (id="default_reminders"): the reminder templates that
create_tenant seeds onto every NEW tenant (see tools/tenants.py). Editing
this doc never touches reminders already seeded on existing tenants — those
are independently owner-managed via repositories/reminders.py.
"""
from __future__ import annotations

from typing import Any

from azure.cosmos import exceptions

from george.repositories.cosmos import get_container
from george.util import utc_now_iso

_DEFAULT_REMINDERS_DOC_ID = "default_reminders"

# Used only if the platformConfig doc hasn't been seeded yet (e.g. a
# deployment created before this container existed) — keeps create_tenant
# working without requiring an out-of-band migration step.
FALLBACK_DEFAULT_REMINDER_TEMPLATES: list[dict[str, Any]] = [
    {
        "name": "Chequeo de la mañana",
        "kind": "prompt",
        "body": "Envia un buenos dias breve y pregunta si hay algo especial que tener en cuenta hoy para el trabajo del dia.",
        "schedule": {"type": "cron", "cron": "0 6 * * *"},
        "target": {"type": "role", "role": "owner"},
    },
    {
        "name": "Cierre del dia",
        "kind": "prompt",
        "body": "Pregunta como estuvo el dia y si quedo algo pendiente por registrar (cobros, novedades, etc.).",
        "schedule": {"type": "cron", "cron": "0 20 * * *"},
        "target": {"type": "role", "role": "owner"},
    },
]


def _container():
    return get_container("platformConfig")


def get_default_reminder_templates() -> list[dict[str, Any]]:
    try:
        doc = _container().read_item(item=_DEFAULT_REMINDERS_DOC_ID, partition_key=_DEFAULT_REMINDERS_DOC_ID)
    except exceptions.CosmosResourceNotFoundError:
        return FALLBACK_DEFAULT_REMINDER_TEMPLATES
    return doc.get("templates") or FALLBACK_DEFAULT_REMINDER_TEMPLATES


def set_default_reminder_templates(templates: list[dict[str, Any]], updated_by: str) -> dict[str, Any]:
    doc = {
        "id": _DEFAULT_REMINDERS_DOC_ID,
        "templates": templates,
        "updatedAt": utc_now_iso(),
        "updatedBy": updated_by,
    }
    return _container().upsert_item(doc)
