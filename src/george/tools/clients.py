"""Tools for managing clients and their "items" — whatever generic asset the
tenant's business is organized around (pets for a dog-walking business,
vehicles for a mechanic, properties for a cleaning service, ...). All scoped
to ctx.tenant_id via require_tenant, which is also the Cosmos partition key —
a tool call can't reach another tenant's clients no matter what client_id
the model passes in.
"""
from __future__ import annotations

import json
from typing import Any

from george.repositories import clients as clients_repo
from george.tools.common import ToolContext, ToolError, ToolSpec, require_role, require_tenant

_READ_ROLES = ("owner", "admin", "walker")
_WRITE_ROLES = ("owner", "admin")


def _summarize(client: dict[str, Any]) -> dict[str, Any]:
    return {
        "clientId": client["clientId"],
        "name": client.get("name"),
        "status": client.get("status"),
        "neighborhood": (client.get("address") or {}).get("neighborhood"),
        "items": [i.get("name") for i in client.get("items", [])],
    }


def _search_clients(input_: dict[str, Any], ctx: ToolContext) -> str:
    require_role(ctx, _READ_ROLES, "search_clients")
    tenant_id = require_tenant(ctx)
    results = clients_repo.search_clients(tenant_id, input_["query"], limit=10)
    if not results:
        return json.dumps({"matches": []})
    return json.dumps({"matches": [_summarize(c) for c in results]}, ensure_ascii=False)


def _get_client(input_: dict[str, Any], ctx: ToolContext) -> str:
    require_role(ctx, _READ_ROLES, "get_client")
    tenant_id = require_tenant(ctx)
    client = clients_repo.get_client(tenant_id, input_["client_id"])
    if client is None:
        raise ToolError(f"No existe un cliente con clientId={input_['client_id']!r} en este negocio.")
    client = {k: v for k, v in client.items() if not k.startswith("_")}
    return json.dumps(client, ensure_ascii=False)


def _upsert_client(input_: dict[str, Any], ctx: ToolContext) -> str:
    require_role(ctx, _WRITE_ROLES, "upsert_client")
    tenant_id = require_tenant(ctx)
    fields = {
        "name": input_.get("name"),
        "aliases": input_.get("aliases"),
        "status": input_.get("status"),
        "address": input_.get("address"),
        "contacts": input_.get("contacts"),
        "pricing": input_.get("pricing"),
    }
    if input_.get("note"):
        client_id = input_.get("client_id")
        existing = clients_repo.get_client(tenant_id, client_id) if client_id else None
        notes = list((existing or {}).get("notes", []))
        notes.append({"by": ctx.chat_id, "at": ctx.correlation_id, "text": input_["note"]})
        fields["notes"] = notes
    result = clients_repo.upsert_client(tenant_id, input_.get("client_id"), ctx.chat_id, **fields)
    return json.dumps({"clientId": result["clientId"], "name": result.get("name")}, ensure_ascii=False)


def _upsert_item(input_: dict[str, Any], ctx: ToolContext) -> str:
    require_role(ctx, _WRITE_ROLES, "upsert_item")
    tenant_id = require_tenant(ctx)
    fields = {
        "name": input_.get("name"),
        "category": input_.get("category"),
        "attributes": input_.get("attributes"),
        "notes": input_.get("notes"),
    }
    try:
        result = clients_repo.upsert_item(tenant_id, input_["client_id"], input_.get("item_id"), **fields)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    item = next(
        i for i in result["items"] if i.get("itemId") == input_.get("item_id") or i.get("name") == fields["name"]
    )
    return json.dumps({"clientId": result["clientId"], "item": item}, ensure_ascii=False)


TOOLS = [
    ToolSpec(
        name="search_clients",
        description=(
            "Busca clientes por nombre o alias (coincidencia parcial, insensible a mayusculas). "
            "Usa esto cuando el usuario mencione a un cliente por nombre y necesites su clientId."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Nombre o parte del nombre a buscar"}
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        allowed_roles=_READ_ROLES,
        handler=_search_clients,
    ),
    ToolSpec(
        name="get_client",
        description="Obtiene el detalle completo de un cliente (items, direccion, precios, notas) por su clientId.",
        input_schema={
            "type": "object",
            "properties": {"client_id": {"type": "string"}},
            "required": ["client_id"],
            "additionalProperties": False,
        },
        allowed_roles=_READ_ROLES,
        handler=_get_client,
    ),
    ToolSpec(
        name="upsert_client",
        description=(
            "Crea un cliente nuevo (omite client_id) o actualiza uno existente (incluye client_id). "
            "Usa 'note' para agregar una nota libre sin borrar las anteriores."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "client_id": {"type": "string", "description": "Omite este campo para crear un cliente nuevo"},
                "name": {"type": "string"},
                "aliases": {"type": "array", "items": {"type": "string"}},
                "status": {"type": "string", "enum": ["active", "paused", "inactive"]},
                "address": {
                    "type": "object",
                    "properties": {
                        "raw": {"type": "string"},
                        "neighborhood": {"type": "string"},
                        "city": {"type": "string"},
                        "notes": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
                "contacts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string"},
                            "value": {"type": "string"},
                            "isPrimary": {"type": "boolean"},
                        },
                        "required": ["type", "value"],
                        "additionalProperties": False,
                    },
                },
                "pricing": {
                    "type": "object",
                    "properties": {
                        "currency": {"type": "string"},
                        "notes": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
                "note": {"type": "string", "description": "Nota libre para agregar al historial del cliente"},
            },
            "required": [],
            "additionalProperties": False,
        },
        allowed_roles=_WRITE_ROLES,
        handler=_upsert_client,
    ),
    ToolSpec(
        name="upsert_item",
        description=(
            "Crea o actualiza uno de los 'items' de un cliente — el tipo de activo alrededor del cual gira el "
            "negocio de este tenant (una mascota, un vehiculo, una propiedad, etc., segun el rubro). "
            "Omite item_id para crear uno nuevo. 'category' y 'attributes' son libres — usa lo que tenga sentido "
            "para este negocio en particular (ej. category='dog', attributes={'breed':'Beagle'} para paseo de perros; "
            "category='sedan', attributes={'plate':'ABC123'} para un taller mecanico)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "client_id": {"type": "string"},
                "item_id": {"type": "string", "description": "Omite este campo para crear un item nuevo"},
                "name": {"type": "string"},
                "category": {"type": "string"},
                "attributes": {"type": "object", "description": "Pares clave-valor libres relevantes para este rubro"},
                "notes": {"type": "string"},
            },
            "required": ["client_id"],
            "additionalProperties": False,
        },
        allowed_roles=_WRITE_ROLES,
        handler=_upsert_item,
    ),
]
