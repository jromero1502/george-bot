"""Validation for owner-defined record types (custom inventories, attendance
logs, stock snapshots, whatever a tenant needs to track over time) and the
values logged against them. Pure — no Cosmos, no ToolContext — same role as
scheduling.py: tools/records.py calls this and turns a ValueError into a
ToolError.

A type definition has a fixed set of typed `fields`, plus two fields singled
out for reporting: `measureField` (a number field, summed/read in reports)
and `groupField` (any field, used to bucket reports — defaults to a single
implicit "total" bucket when omitted). Both are forced `required=True` on
normalization regardless of what the caller passed, because a record missing
either one can't be aggregated — see repositories/records.py, which stores
them denormalized as `amount`/`groupKey` so report queries stay static
(no dynamic property paths in Cosmos SQL).
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

from george.util import utc_now_iso

FIELD_TYPES = ("text", "number", "date", "boolean", "choice")
CLIENT_LINK_MODES = ("none", "optional", "required")
RECORD_MODES = ("snapshot", "event")
MAX_FIELDS = 12
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_TOTAL_GROUP_KEY = "total"


def slugify_key(text: str) -> str:
    """Turns arbitrary text (usually a type/field name) into a valid key:
    lowercase, ASCII letters/digits/underscore only, starting with a letter,
    at most 40 chars."""
    slug = re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")
    if not slug:
        slug = "campo"
    if not slug[0].isalpha():
        slug = f"x_{slug}"
    return slug[:40]


def _validate_field(field_in: dict[str, Any]) -> dict[str, Any]:
    key = field_in.get("key")
    if not key or not isinstance(key, str):
        raise ValueError("Cada campo necesita una 'key'.")
    key = key.strip().lower()
    if not _KEY_RE.match(key):
        raise ValueError(
            f"Clave de campo invalida: {key!r}. Debe empezar con una letra minuscula y contener solo "
            "letras, numeros y guion bajo (maximo 40 caracteres)."
        )

    field_type = field_in.get("type")
    if field_type not in FIELD_TYPES:
        raise ValueError(f"Tipo de campo desconocido {field_type!r} en {key!r}. Usa uno de: {', '.join(FIELD_TYPES)}.")

    choices = field_in.get("choices")
    if field_type == "choice":
        if not choices or not isinstance(choices, list):
            raise ValueError(f"El campo {key!r} es de tipo 'choice' pero no tiene 'choices'.")
        choices = [str(c) for c in choices]
    else:
        choices = None

    return {
        "key": key,
        "label": field_in.get("label") or key,
        "type": field_type,
        "required": bool(field_in.get("required")),
        "choices": choices,
        "unit": field_in.get("unit") or None,
    }


def validate_type_definition(input_: dict[str, Any]) -> dict[str, Any]:
    """Normalizes and validates a type definition. Raises ValueError with a
    user-facing message on any problem. Returns the dict shape to persist
    (see repositories/records.py::upsert_record_type)."""
    name = (input_.get("name") or "").strip()
    if not name:
        raise ValueError("El tipo de registro necesita un 'name'.")

    type_key = (input_.get("type_key") or slugify_key(name)).strip().lower()
    if not _KEY_RE.match(type_key):
        raise ValueError(
            f"type_key invalido: {type_key!r}. Debe empezar con una letra minuscula y contener solo letras, "
            "numeros y guion bajo (maximo 40 caracteres)."
        )

    mode = input_.get("mode") or "event"
    if mode not in RECORD_MODES:
        raise ValueError(f"mode debe ser uno de: {', '.join(RECORD_MODES)} (recibido {mode!r}).")

    fields_in = input_.get("fields") or []
    if not fields_in:
        raise ValueError("Un tipo de registro necesita al menos un campo en 'fields'.")
    if len(fields_in) > MAX_FIELDS:
        raise ValueError(f"Maximo {MAX_FIELDS} campos por tipo de registro (recibidos {len(fields_in)}).")

    fields: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for field_in in fields_in:
        field = _validate_field(field_in)
        if field["key"] in seen_keys:
            raise ValueError(f"Clave de campo repetida: {field['key']!r}.")
        seen_keys.add(field["key"])
        fields.append(field)

    fields_by_key = {f["key"]: f for f in fields}

    measure_field = input_.get("measure_field")
    if not measure_field:
        raise ValueError("measure_field es obligatorio: indica que campo numerico se agrega en los reportes.")
    measure_def = fields_by_key.get(measure_field)
    if measure_def is None:
        raise ValueError(f"measure_field {measure_field!r} no corresponde a ningun campo declarado en 'fields'.")
    if measure_def["type"] != "number":
        raise ValueError(f"measure_field debe ser un campo de tipo 'number' (el campo {measure_field!r} es {measure_def['type']!r}).")
    measure_def["required"] = True  # a record missing the measured field can't be aggregated

    group_field = input_.get("group_field") or None
    if group_field is not None:
        group_def = fields_by_key.get(group_field)
        if group_def is None:
            raise ValueError(f"group_field {group_field!r} no corresponde a ningun campo declarado en 'fields'.")
        group_def["required"] = True  # ungrouped records would collapse into a misleading bucket

    client_link = input_.get("client_link") or "none"
    if client_link not in CLIENT_LINK_MODES:
        raise ValueError(f"client_link debe ser uno de: {', '.join(CLIENT_LINK_MODES)} (recibido {client_link!r}).")

    return {
        "typeKey": type_key,
        "name": name,
        "mode": mode,
        "description": input_.get("description") or None,
        "fields": fields,
        "measureField": measure_field,
        "groupField": group_field,
        "clientLink": client_link,
    }


def derive_period(occurred_at: str) -> str:
    """YYYY-MM out of an ISO-8601 timestamp, same convention as finance.period."""
    dt = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
    return f"{dt.year:04d}-{dt.month:02d}"


def coerce_values(
    type_def: dict[str, Any], values_in: dict[str, Any], occurred_at_in: Optional[str] = None
) -> tuple[dict[str, Any], float, str, str, str]:
    """Validates `values_in` against `type_def['fields']` and derives the
    denormalized (amount, groupKey) plus (occurredAt, period). Raises
    ValueError on any problem. Returns (values, amount, group_key,
    occurred_at, period)."""
    if not isinstance(values_in, dict):
        raise ValueError("'values' debe ser un objeto con los campos del tipo de registro.")

    fields_by_key = {f["key"]: f for f in type_def["fields"]}
    unknown = set(values_in) - set(fields_by_key)
    if unknown:
        raise ValueError(
            f"Campos desconocidos para el tipo {type_def['typeKey']!r}: {', '.join(sorted(unknown))}. "
            f"Campos validos: {', '.join(fields_by_key)}."
        )

    values: dict[str, Any] = {}
    for key, field in fields_by_key.items():
        if key not in values_in:
            if field["required"]:
                raise ValueError(f"Falta el campo obligatorio {key!r} ({field['label']}).")
            continue
        raw = values_in[key]
        if field["type"] == "number":
            try:
                values[key] = float(raw)
            except (TypeError, ValueError):
                raise ValueError(f"El campo {key!r} debe ser numerico (recibido {raw!r}).") from None
        elif field["type"] == "boolean":
            if not isinstance(raw, bool):
                raise ValueError(f"El campo {key!r} debe ser verdadero/falso (recibido {raw!r}).")
            values[key] = raw
        elif field["type"] == "choice":
            if raw not in (field["choices"] or []):
                raise ValueError(f"El campo {key!r} debe ser uno de: {', '.join(field['choices'] or [])} (recibido {raw!r}).")
            values[key] = raw
        elif field["type"] == "date":
            try:
                datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError:
                raise ValueError(f"El campo {key!r} debe ser una fecha ISO-8601 (recibido {raw!r}).") from None
            values[key] = raw
        else:  # text
            values[key] = str(raw)

    if occurred_at_in:
        try:
            datetime.fromisoformat(occurred_at_in.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"occurred_at debe ser una fecha ISO-8601 (recibido {occurred_at_in!r}).") from None
        occurred_at = occurred_at_in
    else:
        occurred_at = utc_now_iso()

    amount = float(values[type_def["measureField"]])
    group_key = str(values[type_def["groupField"]]) if type_def.get("groupField") else _TOTAL_GROUP_KEY
    period = derive_period(occurred_at)

    return values, amount, group_key, occurred_at, period
