"""Build CSV / XLSX files from record-type rows. Pure: no Cosmos, no Telegram
(same role as scheduling.py / record_schema.py), so it is unit-testable.

The bytes are produced inside the tool handler and sent straight to the chat —
they never pass through the model, which only learns row count and total.
That is the point: the chat tools cap listings at 25 rows and the model has to
summarize; a file has no such limit.

Cells come from third-party text (OCR of receipts, dictation), so every string
is neutralized against spreadsheet formula injection (`=`, `+`, `-`, `@`...).
"""
from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from datetime import date
from typing import Any, Optional

MAX_SUMMARY_DIMENSIONS = 3
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_BAD_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")

CSV_MIME = "text/csv"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def safe_text(value: str) -> str:
    """Prefix a leading formula character with ' so Excel/Sheets show it as text."""
    return "'" + value if value.startswith(_FORMULA_START) else value


def safe_filename(type_key: str, today: str, ext: str) -> str:
    base = _BAD_FILENAME_CHARS.sub("_", type_key).strip("._") or "registros"
    return f"{base}_{today}.{ext}"


def columns(type_def: dict[str, Any], has_client: bool) -> list[tuple[str, str]]:
    """(header, source) pairs. source is a field key, or '@client'/'@logged'/'@notes'."""
    cols: list[tuple[str, str]] = []
    for f in type_def.get("fields", []):
        label = f.get("label") or f["key"]
        if f.get("unit"):
            label = f"{label} ({f['unit']})"
        cols.append((label, f["key"]))
    if has_client:
        cols.append(("Cliente", "@client"))
    cols.append(("Fecha de registro", "@logged"))
    cols.append(("Notas", "@notes"))
    return cols


def _cell(record: dict[str, Any], source: str) -> Any:
    if source == "@client":
        return record.get("clientName")
    if source == "@logged":
        return (record.get("occurredAt") or "")[:10] or None
    if source == "@notes":
        return record.get("notes")
    return (record.get("values") or {}).get(source)


def sort_rows(type_def: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Oldest first by the type's first date field (the business date), falling
    back to the system date; stable for ties."""
    date_keys = [f["key"] for f in type_def.get("fields", []) if f["type"] == "date"]

    def key(r: dict[str, Any]) -> str:
        if date_keys:
            v = (r.get("values") or {}).get(date_keys[0])
            if v:
                return str(v)
        return r.get("occurredAt") or ""

    return sorted(rows, key=key)


def _display(value: Any, ftype: Optional[str], for_xlsx: bool) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return "Sí" if value else "No"
    if isinstance(value, (int, float)):
        return value
    text = str(value)
    if ftype == "date" and _DATE_RE.match(text[:10]):
        if for_xlsx:
            return date.fromisoformat(text[:10])  # real date: Excel can filter/sort/group it
        return text[:10]
    return safe_text(text)


def _field_types(type_def: dict[str, Any]) -> dict[str, str]:
    return {f["key"]: f["type"] for f in type_def.get("fields", [])}


def summary_tables(
    type_def: dict[str, Any], rows: list[dict[str, Any]], dimensions: list[str]
) -> list[tuple[str, list[tuple[str, int, float]]]]:
    """For each dimension (a field key; date fields are bucketed by month):
    [(value, count, total of the type's measure)] ordered by total desc."""
    labels = {f["key"]: f.get("label") or f["key"] for f in type_def.get("fields", [])}
    types = _field_types(type_def)
    out = []
    for dim in dimensions:
        acc: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
        for r in rows:
            raw = (r.get("values") or {}).get(dim)
            if raw is None or raw == "":
                k = "(sin dato)"
            elif types.get(dim) == "date":
                k = str(raw)[:7]
            elif isinstance(raw, bool):
                k = "Sí" if raw else "No"
            else:
                k = str(raw)
            acc[k][0] += 1
            acc[k][1] += float(r.get("amount") or 0)
        title = labels.get(dim, dim) + (" (por mes)" if types.get(dim) == "date" else "")
        ordered = sorted(acc.items(), key=lambda kv: (types.get(dim) == "date", -kv[1][1] if types.get(dim) != "date" else 0, kv[0]))
        out.append((title, [(k, int(v[0]), v[1]) for k, v in ordered]))
    return out


def build_csv(type_def: dict[str, Any], rows: list[dict[str, Any]]) -> bytes:
    cols = columns(type_def, any(r.get("clientName") for r in rows))
    types = _field_types(type_def)
    buf = io.StringIO(newline="")
    w = csv.writer(buf)
    w.writerow([safe_text(h) for h, _ in cols])
    for r in rows:
        w.writerow(["" if (v := _display(_cell(r, s), types.get(s), False)) is None else v for _, s in cols])
    return buf.getvalue().encode("utf-8-sig")  # BOM: Excel reads the accents correctly


def build_xlsx(
    type_def: dict[str, Any], rows: list[dict[str, Any]], dimensions: Optional[list[str]] = None
) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    cols = columns(type_def, any(r.get("clientName") for r in rows))
    types = _field_types(type_def)
    wb = Workbook()
    ws = wb.active
    ws.title = "Detalle"
    ws.append([safe_text(h) for h, _ in cols])
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in rows:
        ws.append([_display(_cell(r, s), types.get(s), True) for _, s in cols])
    for idx, (_, src) in enumerate(cols, 1):
        letter = get_column_letter(idx)
        if types.get(src) == "date":
            for cell in ws[letter][1:]:
                cell.number_format = "yyyy-mm-dd"
        elif types.get(src) == "number":
            for cell in ws[letter][1:]:
                cell.number_format = "#,##0.##"
        longest = max((len(str(c.value)) for c in ws[letter] if c.value is not None), default=8)
        ws.column_dimensions[letter].width = min(max(longest + 2, 10), 48)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    dims = [d for d in (dimensions or []) if d in types][:MAX_SUMMARY_DIMENSIONS]
    measure = type_def.get("measureField")
    mlabel = next((f.get("label") or f["key"] for f in type_def.get("fields", []) if f["key"] == measure), "Total")
    total = sum(float(r.get("amount") or 0) for r in rows)
    rs = wb.create_sheet("Resumen")
    rs.append(["Registros", len(rows)])
    rs.append([f"Total {mlabel}", total])
    rs["A1"].font = rs["A2"].font = Font(bold=True)
    rs["B2"].number_format = "#,##0.##"
    for title, table in summary_tables(type_def, rows, dims):
        rs.append([])
        rs.append([safe_text(title), "Registros", f"Total {mlabel}"])
        for c in rs[rs.max_row]:
            c.font = Font(bold=True)
        for k, n, t in table:
            rs.append([safe_text(k), n, t])
            rs.cell(rs.max_row, 3).number_format = "#,##0.##"
    rs.column_dimensions["A"].width = 36
    rs.column_dimensions["B"].width = 14
    rs.column_dimensions["C"].width = 22

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
