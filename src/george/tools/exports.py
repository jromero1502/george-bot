"""export_records: the whole result of a records query as a CSV/XLSX file sent
to THIS chat. The chat tools (search_records) list 25 rows at most and the model
has to summarize them; here the handler reads every row from Cosmos, builds the
file and uploads it to Telegram itself — the bytes never pass through the model,
which only gets row count and total back (same pattern as drive_send_file)."""
from __future__ import annotations

import json
from typing import Any

import httpx

from george import export_files, record_schema, telegram
from george.repositories import records as records_repo
from george.tools.common import ToolContext, ToolError, ToolSpec, require_role, require_tenant
from george.util import utc_now_iso

_ROLES = ("owner", "admin")
MAX_ROWS = 20000  # safety cap on one export; far above this chat's scale
_MAX_FILE_BYTES = 20 * 1024 * 1024  # under Telegram's 50 MB bot upload limit, with margin

_FILTERS_SCHEMA = {
    "type": "array",
    "description": (
        "Condiciones sobre campos del tipo, todas deben cumplirse (igual que en search_records): "
        "{field, op, value}; op = eq (defecto), ne, gt, gte, lt, lte. Las fechas van YYYY-MM-DD. Ej. el mes de "
        "noviembre por fecha de compra: [{\"field\":\"fecha_compra\",\"op\":\"gte\",\"value\":\"2026-11-01\"},"
        "{\"field\":\"fecha_compra\",\"op\":\"lte\",\"value\":\"2026-11-30\"}]."
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
}


def _export_records(input_: dict[str, Any], ctx: ToolContext) -> str:
    require_role(ctx, _ROLES, "export_records")
    tenant_id = require_tenant(ctx)
    type_key = (input_.get("type_key") or "").strip()
    if not type_key:
        raise ToolError("Falta type_key (usa list_record_types para ver los tipos).")
    type_def = records_repo.get_record_type(tenant_id, type_key)
    if type_def is None:
        raise ToolError(f"No existe un tipo de registro con type_key={type_key!r} en este negocio.")

    fmt = (input_.get("format") or "xlsx").lower()
    if fmt not in ("xlsx", "csv"):
        raise ToolError("format debe ser 'xlsx' o 'csv'.")

    value_filters: list[dict[str, Any]] = []
    if input_.get("filters"):
        try:
            _, _, value_filters = record_schema.validate_query(type_def, None, input_["filters"])
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    # summary_by: field keys for the XLSX "Resumen" sheet; only declared fields.
    field_keys = {f["key"] for f in type_def.get("fields", [])}
    summary_by = [k for k in (input_.get("summary_by") or []) if isinstance(k, str)]
    unknown = [k for k in summary_by if k not in field_keys]
    if unknown:
        raise ToolError(f"summary_by tiene campos que el tipo no declara: {', '.join(unknown)}.")
    if len(summary_by) > export_files.MAX_SUMMARY_DIMENSIONS:
        raise ToolError(f"summary_by admite hasta {export_files.MAX_SUMMARY_DIMENSIONS} campos.")

    rows, truncated = records_repo.read_all_records(
        tenant_id,
        type_def["typeKey"],
        MAX_ROWS,
        client_id=input_.get("client_id"),
        period=input_.get("period"),
        since=input_.get("since"),
        until=input_.get("until"),
        value_filters=value_filters,
    )
    if not rows:
        return json.dumps({"sent": False, "rows": 0, "motivo": "Ningún registro coincide con ese filtro."}, ensure_ascii=False)
    rows = export_files.sort_rows(type_def, rows)

    if fmt == "csv":
        content, mime = export_files.build_csv(type_def, rows), export_files.CSV_MIME
    else:
        content, mime = export_files.build_xlsx(type_def, rows, summary_by), export_files.XLSX_MIME
    if len(content) > _MAX_FILE_BYTES:
        raise ToolError("El archivo pesaría más de 20 MB; acota el rango con filters o since/until.")

    total = sum(float(r.get("amount") or 0) for r in rows)
    name = export_files.safe_filename(type_def["typeKey"], utc_now_iso()[:10], fmt)
    caption = f"{type_def.get('name', type_key)}: {len(rows)} registros"
    try:
        telegram.send_document(ctx.chat_id, name, content, mime, caption=caption)
    except httpx.HTTPError as exc:
        raise ToolError(f"Telegram no aceptó el archivo ({type(exc).__name__}).") from None

    out: dict[str, Any] = {
        "sent": True,
        "file_name": name,
        "rows": len(rows),
        "measureField": type_def.get("measureField"),
        "total": total,
    }
    if truncated:
        out["warning"] = (
            f"Se alcanzó el tope de {MAX_ROWS} registros: el archivo es PARCIAL. Dilo al usuario y acota con filters."
        )
    return json.dumps(out, ensure_ascii=False)


TOOLS = [
    ToolSpec(
        name="export_records",
        description=(
            "Genera un archivo Excel (xlsx, por defecto) o CSV con TODOS los registros de un tipo que cumplan los "
            "filtros y se lo envía al usuario a este chat. Úsalo cuando pida un reporte completo, 'mándame el "
            "Excel/CSV', 'todo el detalle', o cuando haya más de ~25 registros: search_records solo lista 25. El "
            "archivo no pasa por ti: solo recibes cuántos registros y el total, así que no inventes su contenido. "
            "El xlsx trae la hoja 'Detalle' (una fila por registro) y 'Resumen' (total y conteo; con summary_by, "
            "una tabla por cada campo indicado, ej. lugar y categoría; los campos de fecha se agrupan por mes). "
            "Filtra con filters igual que search_records."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "type_key": {"type": "string"},
                "format": {"type": "string", "enum": ["xlsx", "csv"], "description": "Defecto xlsx"},
                "filters": _FILTERS_SCHEMA,
                "summary_by": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Hasta 3 keys de campos del tipo para tablas de resumen en la hoja 'Resumen' (solo xlsx)",
                },
                "client_id": {"type": "string"},
                "period": {"type": "string", "description": "YYYY-MM de la fecha en que se REGISTRO (sistema), no de una fecha del negocio: para esas usa filters"},
                "since": {"type": "string", "description": "ISO 8601"},
                "until": {"type": "string", "description": "ISO 8601"},
            },
            "required": ["type_key"],
            "additionalProperties": False,
        },
        allowed_roles=_ROLES,
        handler=_export_records,
    ),
]
