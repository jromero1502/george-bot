import io
import json
from datetime import date

import pytest
from openpyxl import load_workbook

from george import export_files
from george.tools import exports, registry
from george.tools.common import ToolContext, ToolError

TYPE_DEF = {
    "typeKey": "compras",
    "name": "Compras",
    "mode": "event",
    "measureField": "monto",
    "fields": [
        {"key": "producto", "label": "Producto", "type": "text", "required": True},
        {"key": "monto", "label": "Monto", "type": "number", "required": True, "unit": "COP"},
        {"key": "fecha_compra", "label": "Fecha de compra", "type": "date", "required": True},
        {"key": "lugar", "label": "Lugar", "type": "text", "required": True},
        {"key": "agrupado", "label": "Agrupado", "type": "boolean", "required": True},
    ],
}


def rec(producto, monto, fecha, lugar, agrupado=False, **extra):
    return {
        "id": producto,
        "occurredAt": "2026-11-10T01:10:05Z",
        "values": {"producto": producto, "monto": monto, "fecha_compra": fecha, "lugar": lugar, "agrupado": agrupado},
        "amount": monto,
        "clientName": None,
        "notes": None,
        **extra,
    }


ROWS = [
    rec("Leche", 7900, "2026-11-03", "D1"),
    rec("Piña", 12243, "2026-10-31", "Zapatoca"),
    rec("Huevos", 13490, "2026-11-02", "D1", agrupado=True),
]


def ctx(role="owner", tenant_id="t-1"):
    return ToolContext(chat_id="42", role=role, user_name="T", correlation_id="c", tenant_id=tenant_id, tenant={})


def test_csv_has_bom_accents_and_neutralizes_formulas():
    rows = ROWS + [rec("=HYPERLINK(\"http://x\")", 1, "2026-11-04", "+cmd")]
    raw = export_files.build_csv(TYPE_DEF, rows)
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM so Excel reads UTF-8
    text = raw.decode("utf-8-sig")
    assert "Piña" in text and "Monto (COP)" in text and "Sí" in text
    assert "'=HYPERLINK" in text and "'+cmd" in text
    assert not any(line.startswith(("=", "+")) for line in text.splitlines())


def test_sort_rows_oldest_business_date_first():
    assert [r["id"] for r in export_files.sort_rows(TYPE_DEF, ROWS)] == ["Piña", "Huevos", "Leche"]


def test_xlsx_detail_dates_and_summary_sheet():
    data = export_files.build_xlsx(TYPE_DEF, export_files.sort_rows(TYPE_DEF, ROWS), ["lugar", "fecha_compra"])
    wb = load_workbook(io.BytesIO(data))
    assert wb.sheetnames == ["Detalle", "Resumen"]
    detail = wb["Detalle"]
    assert [c.value for c in detail[1]][:3] == ["Producto", "Monto (COP)", "Fecha de compra"]
    assert detail["C2"].value.date() == date(2026, 10, 31)  # a real date, not text
    assert detail.max_row == 4
    flat = []
    for row in wb["Resumen"].iter_rows():
        vals = [c.value for c in row]
        while vals and vals[-1] is None:
            vals.pop()
        flat.append(vals)
    assert ["Registros", 3] in flat
    assert ["Total Monto", 33633] in flat
    assert ["D1", 2, 21390] in flat and ["Zapatoca", 1, 12243] in flat  # by lugar
    assert flat.index(["D1", 2, 21390]) < flat.index(["Zapatoca", 1, 12243])  # biggest total first
    assert ["2026-10", 1, 12243] in flat and ["2026-11", 2, 21390] in flat  # date dimension bucketed by month


def test_xlsx_neutralizes_formulas_in_cells():
    data = export_files.build_xlsx(TYPE_DEF, [rec("=1+1", 5, "2026-11-01", "@x")], ["lugar"])
    wb = load_workbook(io.BytesIO(data))
    assert wb["Detalle"]["A2"].value == "'=1+1"
    assert wb["Detalle"]["D2"].value == "'@x"
    assert wb["Detalle"]["A2"].data_type != "f"


def test_safe_filename():
    assert export_files.safe_filename("../a b/c", "2026-11-14", "xlsx") == "a_b_c_2026-11-14.xlsx"
    assert export_files.safe_filename("???", "2026-11-14", "csv") == "registros_2026-11-14.csv"


@pytest.fixture
def wired(monkeypatch):
    sent = {}
    monkeypatch.setattr(exports.records_repo, "get_record_type", lambda t, k: dict(TYPE_DEF) if k == "compras" else None)
    monkeypatch.setattr(exports.records_repo, "read_all_records", lambda t, k, max_rows, **kw: sent.update(read=kw) or (list(ROWS), False))
    monkeypatch.setattr(
        exports.telegram, "send_document",
        lambda chat_id, name, content, mime, caption=None: sent.update(chat=chat_id, name=name, content=content, mime=mime, caption=caption),
    )
    return sent


def test_registered_owner_admin_only_and_immediate():
    spec = registry._BY_NAME["export_records"]
    assert spec.allowed_roles == ("owner", "admin")
    with pytest.raises(ToolError):
        spec.handler({"type_key": "compras"}, ctx("walker"))


def test_export_sends_file_to_this_chat_and_reports_only_counts(wired):
    out = json.loads(exports._export_records({"type_key": "compras", "summary_by": ["lugar"]}, ctx()))
    assert out["sent"] and out["rows"] == 3 and out["total"] == 33633
    assert wired["chat"] == "42" and wired["name"].startswith("compras_") and wired["name"].endswith(".xlsx")
    assert wired["mime"] == export_files.XLSX_MIME and wired["caption"] == "Compras: 3 registros"
    assert "content" not in out and "warning" not in out


def test_export_csv_and_filters_validated(wired):
    out = json.loads(
        exports._export_records(
            {"type_key": "compras", "format": "csv", "filters": [{"field": "fecha_compra", "op": "gte", "value": "2026-11-01"}]}, ctx()
        )
    )
    assert out["sent"] and wired["name"].endswith(".csv") and wired["mime"] == "text/csv"
    assert wired["read"]["value_filters"][0]["key"] == "fecha_compra"
    with pytest.raises(ToolError):
        exports._export_records({"type_key": "compras", "filters": [{"field": "no_existe", "value": 1}]}, ctx())


def test_export_rejects_bad_input(wired):
    with pytest.raises(ToolError, match="No existe"):
        exports._export_records({"type_key": "nada"}, ctx())
    with pytest.raises(ToolError, match="format"):
        exports._export_records({"type_key": "compras", "format": "pdf"}, ctx())
    with pytest.raises(ToolError, match="summary_by"):
        exports._export_records({"type_key": "compras", "summary_by": ["zzz"]}, ctx())
    with pytest.raises(ToolError, match="hasta"):
        exports._export_records({"type_key": "compras", "summary_by": ["producto", "lugar", "fecha_compra", "monto"]}, ctx())


def test_export_no_rows_sends_nothing(monkeypatch, wired):
    monkeypatch.setattr(exports.records_repo, "read_all_records", lambda *a, **k: ([], False))
    out = json.loads(exports._export_records({"type_key": "compras"}, ctx()))
    assert out["sent"] is False and out["rows"] == 0 and "chat" not in wired


def test_export_warns_when_safety_cap_hit(monkeypatch, wired):
    monkeypatch.setattr(exports.records_repo, "read_all_records", lambda *a, **k: (list(ROWS), True))
    out = json.loads(exports._export_records({"type_key": "compras"}, ctx()))
    assert out["sent"] and "PARCIAL" in out["warning"]
