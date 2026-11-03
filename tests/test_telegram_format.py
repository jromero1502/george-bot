from george.telegram_format import markdown_to_telegram_html as fmt


def test_bold_and_italic():
    assert fmt("**hola** y *mundo*") == "<b>hola</b> y <i>mundo</i>"


def test_bullets_and_headers():
    assert fmt("# Título\n- uno\n* dos") == "<b>Título</b>\n• uno\n• dos"


def test_escapes_html():
    assert fmt("a < b & c > d") == "a &lt; b &amp; c &gt; d"


def test_code_not_interpreted():
    assert fmt("`**x**`") == "<code>**x**</code>"
    assert fmt("```\n<a> **b**\n```") == "<pre>&lt;a&gt; **b**</pre>"


def test_snake_case_untouched():
    assert fmt("usa mi_variable_larga aquí") == "usa mi_variable_larga aquí"


def test_link():
    assert fmt("[web](https://x.com/a_b_c)") == '<a href="https://x.com/a_b_c">web</a>'


def test_multiplication_not_italic():
    assert fmt("2 * 3 * 4") == "2 * 3 * 4"


def test_narrow_table_becomes_aligned_pre():
    out = fmt("| Item | Cant |\n|---|---|\n| Pan | 2 |\n| Leche | 10 |")
    assert out == "<pre>Item  │ Cant\n──────┼─────\nPan   │ 2\nLeche │ 10</pre>"


def test_wide_table_becomes_bullets():
    out = fmt(
        "| Cliente | Concepto | Valor | Estado |\n|---|---|---|---|\n"
        "| Andrea Martínez | Paseo mensual | $120.000 | Pendiente |"
    )
    assert out == "• <b>Andrea Martínez</b> — <b>Concepto</b>: Paseo mensual · <b>Valor</b>: $120.000 · <b>Estado</b>: Pendiente"


def test_text_around_table_preserved():
    out = fmt("Resumen:\n| a | b |\n|---|---|\n| 1 | 2 |\nListo")
    assert out.startswith("Resumen:\n<pre>") and out.endswith("</pre>\nListo")
