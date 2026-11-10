"""Tools for owner-defined record types — the mechanism a tenant uses to
track whatever its business needs over time (stock snapshots, attendance,
daily sales, maintenance logs, ...) beyond what clients/items, finance and
pqrs already cover. See george/record_schema.py for validation and
repositories/records.py for storage; both are intentionally generic — there
is no per-business-type code here.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from george import record_schema
from george.repositories import clients as clients_repo
from george.repositories import records as records_repo
from george.tools.common import ToolContext, ToolError, ToolSpec, require_role, require_tenant

_DEFINE_ROLES = ("owner",)
_USE_ROLES = ("owner", "admin", "walker")
# Correcting/removing an existing record (duplicates, typos, wrong values) is
# restricted tighter than logging a new one — same split as finance.py's
# write tools (owner/admin only, no walker).
_EDIT_ROLES = ("owner", "admin")
_SEARCH_LIMIT = 25


def _client_name(tenant_id: str, client_id: str) -> str:
    client = clients_repo.get_client(tenant_id, client_id)
    if client is None:
        raise ToolError(f"No existe un cliente con clientId={client_id!r}. Búscalo primero con search_clients.")
    return client.get("name", client_id)


def _slim_type(type_def: dict[str, Any]) -> dict[str, Any]:
    return {
        "typeKey": type_def["typeKey"],
        "name": type_def.get("name"),
        "mode": type_def.get("mode"),
        "status": type_def.get("status"),
        "clientLink": type_def.get("clientLink"),
        "measureField": type_def.get("measureField"),
        "groupField": type_def.get("groupField"),
        "fields": [
            {"key": f["key"], "label": f["label"], "type": f["type"], "required": f["required"], "choices": f.get("choices")}
            for f in type_def.get("fields", [])
        ],
    }


def _date_note(type_def: dict[str, Any]) -> Optional[str]:
    """since/until/period/group_by='period' use the record's system date
    (occurredAt: when it was logged), NOT the owner's own date fields. Say so
    when the type has date fields of its own, so a report by fecha_compra is
    never silently built on the load date."""
    own_dates = [f["key"] for f in type_def.get("fields", []) if f["type"] == "date"]
    if not own_dates:
        return None
    return (
        "since/until/period y group_by='period' usan la fecha en que se REGISTRO el registro, no las fechas "
        f"del negocio ({', '.join(own_dates)}). Para filtrar o agrupar por una de esas fechas usa filters "
        "(op gte/lte) y group_by con date_bucket."
    )


def _define_record_type(input_: dict[str, Any], ctx: ToolContext) -> str:
    require_role(ctx, _DEFINE_ROLES, "define_record_type")
    tenant_id = require_tenant(ctx)
    try:
        type_def = record_schema.validate_type_definition(input_)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    result = records_repo.upsert_record_type(tenant_id, type_def, ctx.chat_id, status=input_.get("status"))
    return json.dumps(_slim_type(result), ensure_ascii=False)


def _list_record_types(input_: dict[str, Any], ctx: ToolContext) -> str:
    require_role(ctx, _USE_ROLES, "list_record_types")
    tenant_id = require_tenant(ctx)
    results = records_repo.list_record_types(tenant_id, include_archived=bool(input_.get("include_archived")))
    return json.dumps({"types": [_slim_type(t) for t in results]}, ensure_ascii=False)


def _log_record(input_: dict[str, Any], ctx: ToolContext) -> str:
    require_role(ctx, _USE_ROLES, "log_record")
    tenant_id = require_tenant(ctx)
    type_key = input_["type_key"]
    type_def = records_repo.get_record_type(tenant_id, type_key)
    if type_def is None:
        raise ToolError(
            f"No existe un tipo de registro con type_key={type_key!r} en este negocio. "
            "Usa list_record_types para verlos, o define_record_type para crearlo."
        )
    if type_def.get("status") != "active":
        raise ToolError(f"El tipo de registro {type_def.get('name', type_key)!r} está archivado.")

    client_link = type_def.get("clientLink", "none")
    client_id = input_.get("client_id")
    if client_link == "required" and not client_id:
        raise ToolError(f"El tipo de registro {type_def.get('name', type_key)!r} requiere asociar un client_id.")
    if client_id and client_link == "none":
        raise ToolError(f"El tipo de registro {type_def.get('name', type_key)!r} no admite asociar un cliente.")
    client_name = _client_name(tenant_id, client_id) if client_id else None

    try:
        values, amount, group_key, occurred_at, period = record_schema.coerce_values(
            type_def, input_["values"], input_.get("occurred_at")
        )
    except ValueError as exc:
        raise ToolError(str(exc)) from exc

    record = records_repo.create_record(
        tenant_id,
        type_def["typeKey"],
        type_def["name"],
        ctx.chat_id,
        values,
        amount,
        group_key,
        occurred_at,
        period,
        client_id=client_id,
        client_name=client_name,
        notes=input_.get("notes"),
    )
    return json.dumps(
        {"id": record["id"], "typeKey": record["typeKey"], "values": record["values"], "occurredAt": record["occurredAt"]},
        ensure_ascii=False,
    )


def _get_existing_record(tenant_id: str, record_id: str) -> dict[str, Any]:
    record = records_repo.get_record(tenant_id, record_id)
    if record is None:
        raise ToolError(f"No existe un registro con id={record_id!r}. Búscalo primero con search_records.")
    return record


def _update_record(input_: dict[str, Any], ctx: ToolContext) -> str:
    require_role(ctx, _EDIT_ROLES, "update_record")
    tenant_id = require_tenant(ctx)
    record_id = input_["record_id"]
    existing = _get_existing_record(tenant_id, record_id)

    type_def = records_repo.get_record_type(tenant_id, existing["typeKey"])
    if type_def is None:
        raise ToolError(f"El tipo de registro {existing['typeKey']!r} de este registro ya no existe.")

    client_link = type_def.get("clientLink", "none")
    client_id = input_.get("client_id", existing.get("clientId"))
    if client_link == "required" and not client_id:
        raise ToolError(f"El tipo de registro {type_def.get('name', existing['typeKey'])!r} requiere asociar un client_id.")
    if client_id and client_link == "none":
        raise ToolError(f"El tipo de registro {type_def.get('name', existing['typeKey'])!r} no admite asociar un cliente.")
    client_name = _client_name(tenant_id, client_id) if client_id else None

    try:
        values, amount, group_key, occurred_at, period = record_schema.coerce_values(
            type_def, input_["values"], input_.get("occurred_at") or existing.get("occurredAt")
        )
    except ValueError as exc:
        raise ToolError(str(exc)) from exc

    updated = records_repo.update_record(
        tenant_id,
        record_id,
        values,
        amount,
        group_key,
        occurred_at,
        period,
        client_id=client_id,
        client_name=client_name,
        notes=input_.get("notes", existing.get("notes")),
    )
    return json.dumps(
        {"id": updated["id"], "typeKey": updated["typeKey"], "values": updated["values"], "occurredAt": updated["occurredAt"]},
        ensure_ascii=False,
    )


def _delete_record(input_: dict[str, Any], ctx: ToolContext) -> str:
    require_role(ctx, _EDIT_ROLES, "delete_record")
    tenant_id = require_tenant(ctx)
    record_id = input_["record_id"]
    existing = _get_existing_record(tenant_id, record_id)
    records_repo.delete_record(tenant_id, record_id)
    return json.dumps({"deleted": True, "id": record_id, "typeKey": existing.get("typeKey")}, ensure_ascii=False)


def _search_records(input_: dict[str, Any], ctx: ToolContext) -> str:
    require_role(ctx, _USE_ROLES, "search_records")
    tenant_id = require_tenant(ctx)
    type_key = input_.get("type_key")
    value_filters: list[dict[str, Any]] = []
    if input_.get("filters"):
        if not type_key:
            raise ToolError("Para usar 'filters' indica también type_key.")
        type_def = records_repo.get_record_type(tenant_id, type_key)
        if type_def is None:
            raise ToolError(f"No existe un tipo de registro con type_key={type_key!r} en este negocio.")
        try:
            _, _, value_filters = record_schema.validate_query(type_def, None, input_["filters"])
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
    query = dict(
        type_key=type_key,
        client_id=input_.get("client_id"),
        group_key=input_.get("group_key"),
        period=input_.get("period"),
        since=input_.get("since"),
        until=input_.get("until"),
        value_filters=value_filters,
    )
    results = records_repo.search_records(tenant_id, limit=_SEARCH_LIMIT + 1, **query)
    shown = results[:_SEARCH_LIMIT]
    # When there are more matches than we list, say how many, so the model
    # never reports len(records) as if it were the real count.
    total = len(results) if len(results) <= _SEARCH_LIMIT else records_repo.count_records(tenant_id, **query)
    slim = [
        {
            "id": r["id"],
            "typeKey": r.get("typeKey"),
            "clientId": r.get("clientId"),
            "clientName": r.get("clientName"),
            "occurredAt": r.get("occurredAt"),
            "values": r.get("values"),
            "notes": r.get("notes"),
        }
        for r in shown
    ]
    out: dict[str, Any] = {
        "records": slim,
        "total_matching": total,
        "listed": len(slim),
        "truncated": total > len(slim),
    }
    if type_key and (query["period"] or query["since"] or query["until"]):
        type_def = records_repo.get_record_type(tenant_id, type_key)
        note = _date_note(type_def) if type_def else None
        if note:
            out["nota"] = note
    return json.dumps(out, ensure_ascii=False)


def _summarize_records(input_: dict[str, Any], ctx: ToolContext) -> str:
    require_role(ctx, _USE_ROLES, "summarize_records")
    tenant_id = require_tenant(ctx)
    type_key = input_["type_key"]
    type_def = records_repo.get_record_type(tenant_id, type_key)
    if type_def is None:
        raise ToolError(f"No existe un tipo de registro con type_key={type_key!r} en este negocio.")
    try:
        group_by, bucket_len, value_filters = record_schema.validate_query(
            type_def, input_.get("group_by"), input_.get("filters"), input_.get("date_bucket")
        )
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    result = records_repo.summarize_records(
        tenant_id,
        type_def["typeKey"],
        type_def["mode"],
        since=input_.get("since"),
        until=input_.get("until"),
        client_id=input_.get("client_id"),
        period=input_.get("period"),
        value_filters=value_filters,
        group_by=group_by,
        type_group_field=type_def.get("groupField"),
        bucket_len=bucket_len,
    )
    out: dict[str, Any] = {
        "typeKey": type_def["typeKey"],
        "mode": type_def["mode"],
        "measureField": type_def.get("measureField"),
        "groupedBy": group_by or type_def.get("groupField") or "total",
        "summary": result["summary"],
    }
    note = _date_note(type_def)
    if note and (input_.get("since") or input_.get("until") or input_.get("period") or group_by == "period"):
        out["nota"] = note
    if result["ungrouped"]:
        out["ungrouped"] = result["ungrouped"]  # records without a value for the group_by field
    if result["truncated"]:
        out["warning"] = (
            "Hay mas registros de los que se pudieron leer: este resultado es PARCIAL. "
            "Dilo al usuario y acota con since/until o filters."
        )
    return json.dumps(out, ensure_ascii=False)


TOOLS = [
    ToolSpec(
        name="define_record_type",
        description=(
            "Crea o edita un tipo de registro propio del negocio — la forma en que este tenant declara que "
            "necesita llevar algo en el tiempo que no es un cliente/item, un movimiento financiero ni un PQR "
            "(ej. stock de un producto, asistencia diaria, ventas del dia, mantenimientos). Omite type_key para "
            "crear uno nuevo (se genera del 'name'); incluilo para editar uno existente. "
            "mode='snapshot' es para algo que tiene un valor ACTUAL que se reemplaza cada vez que se registra "
            "(ej. stock); mode='event' es para algo que ocurre y se ACUMULA en el tiempo (ej. asistencia, ventas). "
            "measure_field debe ser la key de un campo numerico de 'fields' — es el valor que se suma o se lee en "
            "los reportes. group_field (opcional) es la key de otro campo por el que se agrupan los reportes "
            "(ej. 'sabor' para agrupar stock de empanadas por sabor); si se omite, los reportes muestran un solo "
            "total. NUNCA inventes esta estructura sin que el usuario la haya confirmado — pregunta primero que "
            "campos necesita, si tiene un valor actual o eventos que se acumulan, y si se agrupa por algo."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "type_key": {"type": "string", "description": "Omite para crear uno nuevo; incluilo para editar uno existente"},
                "name": {"type": "string"},
                "mode": {"type": "string", "enum": ["snapshot", "event"]},
                "description": {"type": "string"},
                "fields": {
                    "type": "array",
                    "description": "Los campos que tiene cada registro de este tipo (maximo 12)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string", "description": "Identificador corto en minusculas, ej. 'sabor'"},
                            "label": {"type": "string"},
                            "type": {"type": "string", "enum": ["text", "number", "date", "boolean", "choice"]},
                            "required": {"type": "boolean"},
                            "choices": {"type": "array", "items": {"type": "string"}, "description": "Requerido si type='choice'"},
                            "unit": {"type": "string", "description": "Ej. 'unidades', 'kg'"},
                        },
                        "required": ["key", "type"],
                        "additionalProperties": False,
                    },
                },
                "measure_field": {"type": "string", "description": "key del campo numerico que se suma/lee en los reportes"},
                "group_field": {"type": "string", "description": "key del campo por el que se agrupan los reportes (opcional)"},
                "client_link": {
                    "type": "string",
                    "enum": ["none", "optional", "required"],
                    "description": "Si los registros de este tipo pueden o deben asociarse a un cliente existente",
                },
                "status": {"type": "string", "enum": ["active", "archived"], "description": "Usa 'archived' para dejar de usar un tipo sin borrar su historico"},
            },
            "required": ["name", "mode", "fields", "measure_field"],
            "additionalProperties": False,
        },
        allowed_roles=_DEFINE_ROLES,
        handler=_define_record_type,
    ),
    ToolSpec(
        name="list_record_types",
        description="Lista los tipos de registro propios que este negocio ya definio, con sus campos.",
        input_schema={
            "type": "object",
            "properties": {"include_archived": {"type": "boolean"}},
            "required": [],
            "additionalProperties": False,
        },
        allowed_roles=_USE_ROLES,
        handler=_list_record_types,
    ),
    ToolSpec(
        name="log_record",
        description=(
            "Carga un registro de un tipo ya definido con define_record_type. 'values' debe traer las keys de los "
            "campos de ese tipo (usa list_record_types si no las tenes a mano). occurred_at es opcional (por "
            "defecto ahora) — usalo para cargar algo retroactivo."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "type_key": {"type": "string"},
                "values": {"type": "object", "description": "Pares clave-valor segun los campos definidos para este tipo"},
                "client_id": {"type": "string", "description": "Solo si el tipo admite asociarse a un cliente"},
                "occurred_at": {"type": "string", "description": "ISO 8601; por defecto ahora"},
                "notes": {"type": "string"},
            },
            "required": ["type_key", "values"],
            "additionalProperties": False,
        },
        allowed_roles=_USE_ROLES,
        handler=_log_record,
    ),
    ToolSpec(
        name="update_record",
        description=(
            "Corrige un registro que ya existe (ej. un valor mal dictado, un duplicado que hay que dejar con los "
            "datos correctos en vez de borrarlo). Encuentra el record_id con search_records primero. 'values' debe "
            "traer el set COMPLETO de campos del tipo (igual que log_record), no solo el que cambia."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "record_id": {"type": "string"},
                "values": {"type": "object", "description": "Pares clave-valor completos segun los campos definidos para ese tipo"},
                "client_id": {"type": "string", "description": "Solo si el tipo admite asociarse a un cliente"},
                "occurred_at": {"type": "string", "description": "ISO 8601; si se omite conserva la fecha original"},
                "notes": {"type": "string"},
            },
            "required": ["record_id", "values"],
            "additionalProperties": False,
        },
        allowed_roles=_EDIT_ROLES,
        handler=_update_record,
    ),
    ToolSpec(
        name="delete_record",
        description=(
            "Elimina un registro existente — tipicamente un duplicado (el mismo evento cargado dos veces) o una "
            "carga claramente equivocada. Encuentra el record_id con search_records primero y confirma con el "
            "usuario cual es el que sobra si hay mas de uno parecido."
        ),
        input_schema={
            "type": "object",
            "properties": {"record_id": {"type": "string"}},
            "required": ["record_id"],
            "additionalProperties": False,
        },
        allowed_roles=_EDIT_ROLES,
        handler=_delete_record,
    ),
    ToolSpec(
        name="search_records",
        description=(
            "Busca el historico de registros de un tipo, opcionalmente filtrando por cliente, grupo, periodo o por "
            "cualquier campo del tipo (filters, requiere type_key). Lista hasta 25 registros; 'total_matching' es "
            "el conteo REAL de coincidencias — usalo para 'cuantos hay', nunca cuentes la lista."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "type_key": {"type": "string"},
                "client_id": {"type": "string"},
                "group_key": {"type": "string"},
                "period": {"type": "string", "description": "YYYY-MM de la fecha en que se REGISTRO (sistema), no de una fecha del negocio: para esas usa filters"},
                "since": {"type": "string", "description": "ISO 8601"},
                "until": {"type": "string", "description": "ISO 8601"},
                "filters": {
                    "type": "array",
                    "description": (
                        "Condiciones sobre campos del tipo, todas deben cumplirse. Cada una: {field, op, value}; "
                        "op = eq (defecto), ne, gt, gte, lt, lte (gt/gte/lt/lte solo en campos number o date). "
                        "Un rango son dos condiciones sobre el mismo campo. Las fechas van como YYYY-MM-DD "
                        "y comparan por dia. Ej. compras entre dos fechas de compra: "
                        "[{\"field\":\"fecha_compra\",\"op\":\"gte\",\"value\":\"2026-10-01\"},"
                        "{\"field\":\"fecha_compra\",\"op\":\"lte\",\"value\":\"2026-10-31\"}]. "
                        "Sirve para CUALQUIER campo del tipo, incluidas varias fechas distintas."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {"type": "string"},
                            "op": {"type": "string", "enum": ["eq", "ne", "gt", "gte", "lt", "lte"]},
                            "value": {},
                        },
                        "required": ["field", "value"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        allowed_roles=_USE_ROLES,
        handler=_search_records,
    ),
    ToolSpec(
        name="summarize_records",
        description=(
            "Resume los registros de un tipo por grupo: si el tipo es mode='snapshot' devuelve el valor mas "
            "reciente por grupo (ej. stock actual por sabor); si es mode='event' devuelve el total acumulado y la "
            "cantidad de registros por grupo en el rango dado (ej. asistencias del mes por perro). Por defecto "
            "agrupa por el group_field del tipo; con group_by puedes agrupar por otro campo del tipo o por "
            "'period' (mes, YYYY-MM), y con filters/client_id/period restringes que registros entran. Este es "
            "el unico origen valido para conteos y totales: no los calcules a mano ni mantengas contadores."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "type_key": {"type": "string"},
                "since": {"type": "string", "description": "ISO 8601"},
                "until": {"type": "string", "description": "ISO 8601"},
                "group_by": {
                    "type": "string",
                    "description": (
                        "key de un campo del tipo (si es de tipo date agrupa por dia/mes/año segun date_bucket) o "
                        "'period' (mes de la fecha de REGISTRO del sistema, no de las fechas del negocio)"
                    ),
                },
                "date_bucket": {
                    "type": "string",
                    "enum": ["day", "month", "year"],
                    "description": "Solo con group_by de un campo date; defecto month",
                },
                "filters": {
                    "type": "array",
                    "description": (
                        "Condiciones sobre campos del tipo, todas deben cumplirse. Cada una: {field, op, value}; "
                        "op = eq (defecto), ne, gt, gte, lt, lte (gt/gte/lt/lte solo en campos number o date). "
                        "Un rango son dos condiciones sobre el mismo campo. Las fechas van como YYYY-MM-DD "
                        "y comparan por dia. Ej. compras entre dos fechas de compra: "
                        "[{\"field\":\"fecha_compra\",\"op\":\"gte\",\"value\":\"2026-10-01\"},"
                        "{\"field\":\"fecha_compra\",\"op\":\"lte\",\"value\":\"2026-10-31\"}]. "
                        "Sirve para CUALQUIER campo del tipo, incluidas varias fechas distintas."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {"type": "string"},
                            "op": {"type": "string", "enum": ["eq", "ne", "gt", "gte", "lt", "lte"]},
                            "value": {},
                        },
                        "required": ["field", "value"],
                        "additionalProperties": False,
                    },
                },
                "client_id": {"type": "string"},
                "period": {"type": "string", "description": "YYYY-MM de la fecha en que se REGISTRO (sistema), no de una fecha del negocio: para esas usa filters"},
            },
            "required": ["type_key"],
            "additionalProperties": False,
        },
        allowed_roles=_USE_ROLES,
        handler=_summarize_records,
    ),
]
