import base64
import json

import httpx
import pytest

from george.tools import drive, google, registry
from george.tools.common import ToolContext, ToolError


def ctx(role="owner", tenant_id="t-1"):
    return ToolContext(chat_id="1", role=role, user_name="T", correlation_id="c", tenant_id=tenant_id, tenant={})


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


@pytest.mark.parametrize("tool", [t.name for t in drive.TOOLS])
def test_drive_tools_registered_owner_admin_only(tool):
    spec = registry._BY_NAME[tool]
    assert spec.allowed_roles == ("owner", "admin")
    with pytest.raises(ToolError):
        spec.handler({"message_id": "m", "file_id": "f", "name": "n"}, ctx("walker"))


def test_safe_filename():
    assert "\n" not in drive._safe_filename("../../etc/pass\nwd")
    cleaned = drive._safe_filename("a/b" + chr(92) + "c")
    assert "/" not in cleaned and chr(92) not in cleaned
    assert drive._safe_filename("   ") == "adjunto"


def test_search_escapes_quotes(monkeypatch):
    seen = {}
    monkeypatch.setattr(google, "_request", lambda t, m, u, **kw: seen.update(kw) or {"files": []})
    bs = chr(92)
    drive._drive_search({"name_contains": "o'brien" + bs + "x", "only_folders": True}, ctx())
    q = seen["params"]["q"]
    assert "name contains 'o" + bs + "'brien" + bs + bs + "x'" in q
    assert "trashed = false" in q and "folder" in q


def _message():
    return {
        "id": "m1",
        "payload": {
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "text/plain", "filename": "", "body": {"data": b64(b"hola")}},
                {"mimeType": "application/pdf", "filename": "Factura.pdf", "body": {"attachmentId": "a1", "size": 5}},
                {"mimeType": "image/png", "filename": "foto.png", "body": {"attachmentId": "a2", "size": 3}},
            ],
        },
    }


@pytest.fixture
def fake(monkeypatch):
    log = {"requests": [], "sends": []}

    def fake_request(tenant_id, method, url, **kw):
        log["requests"].append((method, url, kw))
        if url.endswith("/messages/m1"):
            return _message()
        if "/attachments/a1" in url:
            return {"data": b64(b"PDFDATA")}
        if "/attachments/a2" in url:
            return {"data": b64(b"PNG")}
        if "/drive/v3/files/" in url:
            return {"id": "F1", "mimeType": drive._FOLDER_MIME}
        return {}

    def fake_send(tenant_id, method, url, **kw):
        log["sends"].append((method, url, kw))
        if method == "POST":
            return httpx.Response(200, headers={"Location": "https://upload/xyz"}, json={})
        n = len([s for s in log["sends"] if s[0] == "PUT"])
        return httpx.Response(200, json={"id": f"D{n}", "name": "x", "webViewLink": f"https://drive/{n}"})

    monkeypatch.setattr(google, "_request", fake_request)
    monkeypatch.setattr(google, "_send", fake_send)
    return log


def test_uploads_all_attachments_and_bytes_stay_out_of_result(fake):
    out = json.loads(drive._gmail_attachment_to_drive({"message_id": "m1", "folder_id": "F1"}, ctx()))
    assert [u["link"] for u in out["uploaded"]] == ["https://drive/1", "https://drive/2"]
    assert out["failed"] == []
    puts = [s for s in fake["sends"] if s[0] == "PUT"]
    assert puts[0][2]["content"] == b"PDFDATA" and puts[1][2]["content"] == b"PNG"
    posts = [s for s in fake["sends"] if s[0] == "POST"]
    assert posts[0][2]["json"] == {"name": "Factura.pdf", "parents": ["F1"]}
    assert "PDFDATA" not in json.dumps(out)


def test_single_attachment_by_filename_and_rename(fake):
    out = json.loads(
        drive._gmail_attachment_to_drive({"message_id": "m1", "filename": "factura.PDF", "new_name": "Nov.pdf"}, ctx())
    )
    assert len(out["uploaded"]) == 1
    post = [s for s in fake["sends"] if s[0] == "POST"][0]
    assert post[2]["json"] == {"name": "Nov.pdf"}


def test_missing_attachment_and_non_folder(fake, monkeypatch):
    with pytest.raises(ToolError, match="No encontré"):
        drive._gmail_attachment_to_drive({"message_id": "m1", "filename": "nope.pdf"}, ctx())
    orig = google._request

    def not_a_folder(t, m, u, **kw):
        if "/drive/v3/" in u:
            return {"id": "x", "mimeType": "text/plain"}
        return orig(t, m, u, **kw)

    monkeypatch.setattr(google, "_request", not_a_folder)
    with pytest.raises(ToolError, match="no es una carpeta"):
        drive._gmail_attachment_to_drive({"message_id": "m1", "folder_id": "x"}, ctx())


def test_drive_read_marks_untrusted_and_skips_binary(monkeypatch):
    meta = {"id": "d", "name": "n", "mimeType": "application/vnd.google-apps.document"}
    monkeypatch.setattr(google, "_request", lambda *a, **k: meta)
    monkeypatch.setattr(google, "_send", lambda *a, **k: httpx.Response(200, content=b"contenido"))
    out = json.loads(drive._drive_read({"file_id": "d"}, ctx()))
    assert out["UNTRUSTED_FILE_CONTENT"] == "contenido"
    meta["mimeType"] = "application/pdf"
    out = json.loads(drive._drive_read({"file_id": "d"}, ctx()))
    assert out["content_available"] is False and "UNTRUSTED_FILE_CONTENT" not in out


def test_send_file_goes_to_this_chat_and_exports_docs(monkeypatch):
    sent = {}
    monkeypatch.setattr(google, "_request", lambda *a, **k: {"id": "d", "name": "Informe", "mimeType": "application/vnd.google-apps.document", "webViewLink": "L"})
    monkeypatch.setattr(google, "_send", lambda t, m, u, **k: sent.update(export=k["params"]) or httpx.Response(200, content=b"%PDF"))
    monkeypatch.setattr(drive.telegram, "send_document", lambda chat_id, name, content, mime, caption=None: sent.update(chat=chat_id, name=name, content=content, mime=mime))
    out = json.loads(drive._drive_send_file({"file_id": "d"}, ctx()))
    assert out["sent"] and sent["chat"] == "1" and sent["name"] == "Informe.pdf"
    assert sent["mime"] == "application/pdf" and sent["export"]["mimeType"] == "application/pdf"


def test_send_file_rejects_folder_and_huge(monkeypatch):
    monkeypatch.setattr(google, "_request", lambda *a, **k: {"id": "d", "name": "x", "mimeType": drive._FOLDER_MIME})
    with pytest.raises(ToolError, match="carpeta"):
        drive._drive_send_file({"file_id": "d"}, ctx())
    monkeypatch.setattr(google, "_request", lambda *a, **k: {"id": "d", "name": "x", "mimeType": "video/mp4", "size": str(30 * 1024 * 1024)})
    with pytest.raises(ToolError, match="20 MB"):
        drive._drive_send_file({"file_id": "d"}, ctx())


def test_save_email_uploads_eml_with_dated_name(monkeypatch):
    sends = []

    def req(t, m, u, **kw):
        if kw.get("params", {}).get("format") == "raw":
            return {"raw": b64(b"From: a@b.com\r\nSubject: Recibo\r\n\r\ncuerpo")}
        return {"internalDate": "1793664000000", "payload": {"headers": [{"name": "Subject", "value": "Recibo / pago"}]}}

    def send(t, m, u, **kw):
        sends.append((m, kw))
        if m == "POST":
            return httpx.Response(200, headers={"Location": "https://u/x"}, json={})
        return httpx.Response(200, json={"id": "D1", "webViewLink": "L"})

    monkeypatch.setattr(google, "_request", req)
    monkeypatch.setattr(google, "_send", send)
    out = json.loads(drive._gmail_save_email_to_drive({"message_id": "m1"}, ctx()))
    assert out["saved"] and out["link"] == "L"
    post = sends[0][1]
    assert post["json"]["name"].endswith("Recibo _ pago.eml") and post["headers"]["X-Upload-Content-Type"] == "message/rfc822"
    assert sends[1][1]["content"].startswith(b"From: a@b.com")


def test_save_email_requires_role_and_id():
    with pytest.raises(ToolError):
        drive._gmail_save_email_to_drive({"message_id": "m"}, ctx("walker"))
    with pytest.raises(ToolError, match="message_id"):
        drive._gmail_save_email_to_drive({}, ctx())
