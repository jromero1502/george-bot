from george import tool_log
from george.tool_log import format_tool_log, strip_tool_log
import function_app


def _call(name="create_expense", ok=True, **inp):
    return {"name": name, "input": inp, "ok": ok, "error": None if ok else "boom"}


def test_log_lists_each_tool_with_outcome():
    out = format_tool_log([_call(concept="Huevo", amount=13490), _call(ok=False, concept="X")], "Listo")
    assert out.startswith("\n\n" + tool_log.TOOL_LOG_PREFIX)
    assert "create_expense" in out and "Huevo" in out and "→ ok" in out
    assert "→ ERROR: boom" in out


def test_log_is_empty_for_a_plain_reply_without_tools():
    assert format_tool_log([], "Hola, ¿en qué te ayudo?") == ""
    assert format_tool_log(None, "") == ""


def test_log_flags_a_confirmation_that_ran_no_tool():
    out = format_tool_log([], "✅ Anotado: 1 botella de kéfir")
    assert "NO se ejecutó ninguna herramienta" in out


def test_log_caps_a_long_list():
    out = format_tool_log([_call(concept=f"item {i}") for i in range(40)], "ok")
    assert out.count("\n- create_expense") == 15
    assert "y 25 llamadas más" in out


def test_strip_removes_a_log_the_model_imitated():
    reply = "Listo, registré los 7 gastos.\n\n[Registro del sistema — herramientas ejecutadas en este turno:\n- x {} → ok]"
    assert strip_tool_log(reply) == "Listo, registré los 7 gastos."
    assert strip_tool_log("sin marcador") == "sin marcador"


def test_strip_never_returns_an_empty_reply():
    only_log = "[Registro del sistema — herramientas ejecutadas: - x → ok]"
    assert strip_tool_log(only_log) == only_log


def test_history_appends_the_real_tool_record_to_assistant_turns():
    docs = [
        {
            "direction": "inbound",
            "input": {"text": "Registremos por cada ítem"},
            "output": {"text": "Listo, registré los 7 gastos"},
            "toolCalls": [_call(concept="Huevo tipo A", amount=13490)],
        },
        {"direction": "inbound", "input": {"text": "hola"}, "output": {"text": "¡Hola!"}},
    ]
    msgs = function_app._history_to_messages(docs)
    assert msgs[0]["content"] == "Registremos por cada ítem"
    assert "Huevo tipo A" in msgs[1]["content"] and "→ ok" in msgs[1]["content"]
    assert msgs[3]["content"] == "¡Hola!"  # a plain turn is left untouched


def test_history_marks_a_claim_that_ran_nothing():
    docs = [{"direction": "inbound", "input": {"text": "agrega leche 4200"}, "output": {"text": "✅ Anotado: leche $4.200"}}]
    assert "NO se ejecutó ninguna herramienta" in function_app._history_to_messages(docs)[1]["content"]
