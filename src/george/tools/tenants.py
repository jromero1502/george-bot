"""Tools for platform-level tenant (business) management — platform_admin
only. This is the only place a brand-new business gets provisioned: chats
can't self-register a tenant (see chats.py docstring for why)."""
from __future__ import annotations

import json
from typing import Any

from george import scheduling
from george.repositories import chats as chats_repo
from george.repositories import platform_config as platform_config_repo
from george.repositories import reminders as reminders_repo
from george.repositories import tenants as tenants_repo
from george.roles import BUSINESS_ROLES
from george.tools.common import ToolContext, ToolError, ToolSpec, require_role

_ROLES = ("platform_admin",)


def _create_tenant(input_: dict[str, Any], ctx: ToolContext) -> str:
    require_role(ctx, _ROLES, "create_tenant")

    owner_chat_id = input_["owner_chat_id"]
    existing_chat = chats_repo.get_chat(owner_chat_id)
    # A platform_admin chat CAN also be the owner of its own business (dual
    # role) — but its default stays platform mode, so we don't auto-activate
    # this new membership for it the way we do for a regular chat; it has to
    # switch_business into it explicitly.
    owner_is_platform_admin = bool(existing_chat and existing_chat.get("isPlatformAdmin"))

    timezone = input_.get("timezone") or "America/Bogota"
    tenant = tenants_repo.create_tenant(
        name=input_["name"],
        business_type=input_["business_type"],
        created_by=ctx.chat_id,
        description=input_.get("description", ""),
        currency=input_.get("currency") or "COP",
        timezone=timezone,
        locale=input_.get("locale") or "es-CO",
        item_label_singular=input_.get("item_label_singular") or "cliente",
        item_label_plural=input_.get("item_label_plural") or "clientes",
    )

    # A brand-new business is almost always meant to become this chat's
    # active one immediately — set_active=True even if the chat already has
    # other memberships, rather than leaving it stuck on whatever was active
    # before (or forcing an immediate switch_business round-trip). Except for
    # a platform_admin owner: its default stays platform mode (see above).
    chats_repo.add_membership(
        owner_chat_id,
        tenant["id"],
        "owner",
        name=input_.get("owner_name"),
        timezone=timezone,
        set_active=not owner_is_platform_admin,
    )

    for reminder in platform_config_repo.get_default_reminder_templates():
        schedule = {**reminder["schedule"], "timezone": timezone}
        next_run_at = scheduling.compute_next_run(schedule)
        reminders_repo.upsert_reminder(
            tenant["id"],
            None,
            ctx.chat_id,
            name=reminder["name"],
            kind=reminder["kind"],
            body=reminder["body"],
            schedule=schedule,
            target=reminder["target"],
            status="active",
            system=True,
            nextRunAt=next_run_at,
        )

    result = {"tenantId": tenant["id"], "name": tenant["name"], "ownerChatId": owner_chat_id}
    if owner_is_platform_admin:
        result["note"] = (
            "El chat owner es administrador de la plataforma, así que sigue en modo plataforma por defecto — "
            "usa switch_business con este tenantId para operar el negocio."
        )
    return json.dumps(result, ensure_ascii=False)


def _list_tenants(input_: dict[str, Any], ctx: ToolContext) -> str:
    require_role(ctx, _ROLES, "list_tenants")
    results = tenants_repo.list_tenants(status=input_.get("status"))
    slim = [
        {"id": t["id"], "name": t.get("name"), "businessType": t.get("businessType"), "status": t.get("status")}
        for t in results
    ]
    return json.dumps({"tenants": slim}, ensure_ascii=False)


def _add_chat_to_tenant(input_: dict[str, Any], ctx: ToolContext) -> str:
    require_role(ctx, _ROLES, "add_chat_to_tenant")

    tenant_id = input_["tenant_id"]
    tenant = tenants_repo.get_active_tenant(tenant_id)
    if tenant is None:
        raise ToolError(f"No existe un negocio activo con tenantId={tenant_id!r}.")

    chat_id = input_["chat_id"]

    try:
        result = chats_repo.add_membership(
            chat_id,
            tenant_id,
            input_["role"],
            name=input_.get("name"),
            set_active=input_.get("set_active"),
        )
    except ValueError as exc:
        raise ToolError(str(exc)) from exc

    membership = chats_repo.get_membership(result, tenant_id)
    return json.dumps(
        {"chatId": chat_id, "tenantId": tenant_id, "tenantName": tenant.get("name"), "role": membership["role"]},
        ensure_ascii=False,
    )


def _get_default_reminder_templates(input_: dict[str, Any], ctx: ToolContext) -> str:
    require_role(ctx, _ROLES, "get_default_reminder_templates")
    templates = platform_config_repo.get_default_reminder_templates()
    return json.dumps({"templates": templates}, ensure_ascii=False)


def _set_default_reminder_templates(input_: dict[str, Any], ctx: ToolContext) -> str:
    require_role(ctx, _ROLES, "set_default_reminder_templates")

    templates_in = input_["templates"]
    if not templates_in:
        raise ToolError("La lista de plantillas no puede quedar vacia.")

    templates: list[dict[str, Any]] = []
    for i, template_in in enumerate(templates_in):
        schedule = {"type": "cron", "cron": template_in["schedule"]["cron"]}
        try:
            # Validate the cron expression is well-formed; the timezone used
            # here is throwaway (compute_next_run needs one to compute an
            # actual instant, but this template has no tenant/timezone of its
            # own yet — that only exists once a real tenant seeds from it).
            scheduling.compute_next_run({**schedule, "timezone": "UTC"})
        except ValueError as exc:
            raise ToolError(f"Plantilla #{i + 1} ('{template_in.get('name')}'): horario invalido: {exc}") from exc
        templates.append(
            {
                "name": template_in["name"],
                "kind": template_in["kind"],
                "body": template_in["body"],
                "schedule": schedule,
                "target": {"type": "role", "role": template_in["target_role"]},
            }
        )

    platform_config_repo.set_default_reminder_templates(templates, updated_by=ctx.chat_id)
    return json.dumps({"templates": templates}, ensure_ascii=False)


TOOLS = [
    ToolSpec(
        name="create_tenant",
        description=(
            "Crea un nuevo negocio (tenant) en la plataforma y registra su primer chat owner. "
            "Tambien siembra los recordatorios por defecto configurados en la plataforma (ver "
            "get_default_reminder_templates / set_default_reminder_templates), dirigidos al owner. "
            "Solo el platform_admin puede usar esta herramienta — un negocio nunca se crea solo."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Nombre del negocio"},
                "business_type": {
                    "type": "string",
                    "description": "Rubro, ej. 'Paseo de perros', 'Peluqueria canina', 'Plomeria'",
                },
                "description": {
                    "type": "string",
                    "description": "Descripcion breve de a que se dedica el negocio, para el prompt del agente",
                },
                "owner_chat_id": {"type": "string", "description": "chatId de Telegram del primer owner del negocio"},
                "owner_name": {"type": "string"},
                "currency": {"type": "string", "description": "Codigo ISO, ej. COP, USD. Por defecto COP"},
                "timezone": {"type": "string", "description": "IANA tz. Por defecto America/Bogota"},
                "locale": {"type": "string", "description": "Por defecto es-CO"},
                "item_label_singular": {
                    "type": "string",
                    "description": "Como se llama en singular lo que gestiona el negocio (ej. 'mascota', 'vehiculo'). Por defecto 'cliente'",
                },
                "item_label_plural": {"type": "string", "description": "Plural de item_label_singular"},
            },
            "required": ["name", "business_type", "owner_chat_id"],
            "additionalProperties": False,
        },
        allowed_roles=_ROLES,
        handler=_create_tenant,
    ),
    ToolSpec(
        name="list_tenants",
        description="Lista los negocios (tenants) registrados en la plataforma.",
        input_schema={
            "type": "object",
            "properties": {"status": {"type": "string", "enum": ["active", "suspended"]}},
            "required": [],
            "additionalProperties": False,
        },
        allowed_roles=_ROLES,
        handler=_list_tenants,
    ),
    ToolSpec(
        name="add_chat_to_tenant",
        description=(
            "Asocia un chat (nuevo o existente) a un negocio ya creado, con un rol dado. Esta es la ÚNICA forma "
            "de sumar un chat a un negocio que no sea el momento de create_tenant — un owner puede sumar gente "
            "a SU PROPIO negocio con upsert_chat, pero conectar un chat a un negocio por primera vez, o sumarlo "
            "a un negocio adicional, es exclusivo del platform_admin. Un mismo chat puede pertenecer a varios "
            "negocios a la vez, cada uno con su propio rol — incluso un chat que es platform_admin puede tener "
            "membership en un negocio propio; en ese caso su modo por defecto sigue siendo plataforma, y usa "
            "switch_business/switch_to_platform_admin para cambiar de uno a otro."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "chat_id": {"type": "string", "description": "chatId de Telegram a asociar"},
                "tenant_id": {"type": "string"},
                "role": {"type": "string", "enum": list(BUSINESS_ROLES)},
                "name": {"type": "string"},
                "set_active": {
                    "type": "boolean",
                    "description": "Si es true, este negocio queda como el activo para ese chat de inmediato",
                },
            },
            "required": ["chat_id", "tenant_id", "role"],
            "additionalProperties": False,
        },
        allowed_roles=_ROLES,
        handler=_add_chat_to_tenant,
    ),
    ToolSpec(
        name="get_default_reminder_templates",
        description=(
            "Muestra la plantilla de recordatorios por defecto que create_tenant siembra en cada negocio "
            "nuevo (nombre, instruccion/mensaje, horario cron y rol destino de cada uno)."
        ),
        input_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        allowed_roles=_ROLES,
        handler=_get_default_reminder_templates,
    ),
    ToolSpec(
        name="set_default_reminder_templates",
        description=(
            "Reemplaza por completo la plantilla de recordatorios por defecto que create_tenant va a sembrar "
            "de ahora en adelante en cada negocio NUEVO — no modifica los recordatorios de negocios que ya "
            "existen (esos los administra cada owner con upsert_reminder/delete_reminder). Pasa la lista "
            "completa de plantillas que quieras dejar activas (usa get_default_reminder_templates primero para "
            "ver las actuales y partir de ahi)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "templates": {
                    "type": "array",
                    "description": "Lista completa de plantillas; reemplaza a las anteriores.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "kind": {"type": "string", "enum": ["message", "prompt"]},
                            "body": {
                                "type": "string",
                                "description": (
                                    "Si kind='message', el texto tal cual. Si kind='prompt', la instruccion "
                                    "para que George redacte el mensaje al momento de enviarlo."
                                ),
                            },
                            "schedule": {
                                "type": "object",
                                "properties": {
                                    "cron": {
                                        "type": "string",
                                        "description": "Expresion cron estandar, ej. '0 6 * * *' = todos los dias 6am",
                                    }
                                },
                                "required": ["cron"],
                                "additionalProperties": False,
                            },
                            "target_role": {
                                "type": "string",
                                "enum": list(BUSINESS_ROLES),
                                "description": "A que rol del negocio se le envia este recordatorio",
                            },
                        },
                        "required": ["name", "kind", "body", "schedule", "target_role"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["templates"],
            "additionalProperties": False,
        },
        allowed_roles=_ROLES,
        handler=_set_default_reminder_templates,
    ),
]
