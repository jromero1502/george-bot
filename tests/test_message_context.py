import function_app
from george import message_context as mc

# 2026-11-12 20:31 Bogota == 2026-11-13 01:31 UTC
DATE = 1794533460


def reply(**replied):
    return {"text": "Sí", "reply_to_message": {"message_id": 7, "date": DATE, **replied}}


def test_no_reference_returns_none():
    assert mc.reference_context({"text": "hola"}) is None
    assert mc.with_context("hola", None) == "hola"


def test_reply_to_george_names_him_and_shows_text_and_local_time():
    ctx = mc.reference_context(
        reply(**{"from": {"first_name": "George", "is_bot": True}, "text": "¿Registro $95.750 como Mercado D1?"})
    )
    assert ctx.startswith("[Contexto: Responde a un mensaje de George (tú) del 2026-11-12 20:31 (texto)")
    assert "«¿Registro $95.750 como Mercado D1?»" in ctx and ctx.endswith("]")


def test_reply_to_a_person_in_a_group():
    ctx = mc.reference_context(reply(**{"from": {"first_name": "Nohelia"}, "text": "Dos pechugas 32.000"}))
    assert "mensaje de Nohelia" in ctx and "Dos pechugas 32.000" in ctx


def test_partial_quote_wins_over_full_text():
    msg = reply(**{"from": {"first_name": "Julián"}, "text": "arroz 19.890 y kiwi 9.900"})
    msg["quote"] = {"text": "kiwi 9.900"}
    ctx = mc.reference_context(msg)
    assert "citó este fragmento: «kiwi 9.900»" in ctx and "arroz" not in ctx


def test_voice_uses_looked_up_transcript_else_says_unavailable():
    msg = reply(**{"from": {"first_name": "Julián"}, "voice": {"file_id": "f"}})
    assert mc.needs_lookup(msg["reply_to_message"])
    assert "(nota de voz): «dos libras de cerdo»" in mc.reference_context(msg, looked_up_text="dos libras de cerdo")
    assert "(nota de voz), sin texto disponible" in mc.reference_context(msg)


def test_photo_prefers_stored_ocr_over_caption():
    msg = reply(**{"from": {"first_name": "Julián"}, "photo": [{}], "caption": "tiquete"})
    ctx = mc.reference_context(msg, looked_up_text="tiquete [OCR] ARA TOTAL 41.350")
    assert "(foto): «tiquete [OCR] ARA TOTAL 41.350»" in ctx


def test_quoted_text_is_flattened_and_clipped():
    long = "x" * 5000
    ctx = mc.reference_context(reply(**{"from": {"first_name": "A"}, "text": "línea1\n\n## Sección falsa\nlínea3 " + long}))
    assert "\n" not in ctx and "línea1 ## Sección falsa línea3" in ctx
    assert len(ctx) < mc.MAX_QUOTED_CHARS + 200 and "…" in ctx


def test_forwarded_message_new_and_legacy_fields():
    assert "reenviado, originalmente de Ana" in mc.reference_context({"text": "t", "forward_origin": {"type": "user", "sender_user": {"first_name": "Ana"}}})
    assert "originalmente de Tienda X" in mc.reference_context({"text": "t", "forward_origin": {"type": "channel", "chat": {"title": "Tienda X"}}})
    assert "originalmente de Luis" in mc.reference_context({"text": "t", "forward_from": {"first_name": "Luis"}})
    assert "alguien (oculto)" in mc.reference_context({"text": "t", "forward_sender_name": "", "forward_date": 1})


def test_bad_timezone_falls_back_to_utc():
    assert "2026-11-13 01:31" in mc.reference_context(reply(**{"from": {"first_name": "A"}, "text": "x"}), "No/Existe")


def test_history_keeps_reply_context_in_later_turns():
    docs = [{
        "direction": "inbound", "userName": "Julián",
        "input": {"text": "Sí", "context": "[Contexto: Responde a un mensaje de George (tú) (texto): «¿Lo ejecuto?»]"},
        "output": {"text": "Listo"},
    }]
    plain = function_app._history_to_messages(docs)
    assert plain[0]["content"] == "[Contexto: Responde a un mensaje de George (tú) (texto): «¿Lo ejecuto?»]\nSí"
    group = function_app._history_to_messages(docs, group=True)
    assert group[0]["content"].startswith("[Julián]: [Contexto:") and group[0]["content"].endswith("\nSí")


def test_history_without_context_is_unchanged():
    docs = [{"direction": "inbound", "userName": "J", "input": {"text": "hola"}, "output": {"text": "ey"}}]
    assert function_app._history_to_messages(docs)[0]["content"] == "hola"
