"""Convierte el Markdown que escribe el modelo a la parte de HTML que Telegram
soporta (<b>, <i>, <s>, <code>, <pre>, <a>). Se usa HTML y no MarkdownV2 porque
éste último exige escapar ~18 caracteres y un solo `.` sin escapar hace que
Telegram rechace el mensaje entero; en HTML solo hay que escapar `& < >`.

Función pura, sin I/O. Si algo sale mal igual, `telegram.send_message` reintenta
en texto plano.
"""
from __future__ import annotations

import html
import re

_FENCE_RE = re.compile(r"```[^\n`]*\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_HEADER_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_BULLET_RE = re.compile(r"^([ \t]*)[-*+][ \t]+", re.MULTILINE)
_HR_RE = re.compile(r"^[ \t]*([-*_])([ \t]*\1){2,}[ \t]*$", re.MULTILINE)
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_BOLD_RE = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", re.DOTALL)
_ITALIC_STAR_RE = re.compile(r"(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![\w*])")
_ITALIC_UNDER_RE = re.compile(r"(?<![\w_])_(?=[^\s_])([^_\n]+?)(?<=[^\s_])_(?![\w_])")
_STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.DOTALL)


# Una tabla: fila de encabezado, fila separadora (|---|:--:|) y filas de datos.
_TABLE_RE = re.compile(
    r"(?:^[ \t]*\|.*\|[ \t]*\n)"
    r"(?:^[ \t]*\|[ \t:|-]*-[ \t:|-]*\|[ \t]*\n)"
    r"(?:^[ \t]*\|.*\|[ \t]*(?:\n|$))+",
    re.MULTILINE,
)
_MD_MARKS_RE = re.compile(r"\*\*|__|`|~~")
# Ancho máximo (caracteres) para dibujar la tabla alineada en monoespaciado;
# más allá, en un celular la línea se parte y queda peor que una lista.
_MAX_TABLE_WIDTH = 42


def _split_row(line: str) -> list[str]:
    return [_MD_MARKS_RE.sub("", c).strip() for c in line.strip().strip("|").split("|")]


def _render_table(block: str) -> tuple[str, bool]:
    """Devuelve (texto, es_preformateado). Alineada si cabe; si no, lista."""
    lines = [ln for ln in block.strip("\n").split("\n") if ln.strip()]
    header = _split_row(lines[0])
    rows = [_split_row(ln) for ln in lines[2:]]
    n = len(header)
    rows = [(r + [""] * n)[:n] for r in rows]
    widths = [max(len(c) for c in col) for col in zip(header, *rows)]
    if sum(widths) + 3 * (n - 1) <= _MAX_TABLE_WIDTH:
        def fmt_row(cells: list[str]) -> str:
            return " │ ".join(c.ljust(w) for c, w in zip(cells, widths)).rstrip()

        rule = "─┼─".join("─" * w for w in widths)
        return "\n".join([fmt_row(header), rule, *(fmt_row(r) for r in rows)]), True
    # Demasiado ancha: una viñeta por fila, "**Col**: valor" (se procesa como Markdown).
    out = []
    for r in rows:
        first, rest = r[0], r[1:]
        parts = [f"**{h}**: {v}" for h, v in zip(header[1:], rest) if v]
        out.append(f"- **{first}**" + (" — " + " · ".join(parts) if parts else ""))
    return "\n".join(out) + "\n", False


def markdown_to_telegram_html(text: str) -> str:
    stash: list[str] = []

    def keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    def table(m: re.Match) -> str:
        rendered, pre = _render_table(m.group(0))
        return keep(f"<pre>{html.escape(rendered)}</pre>") + "\n" if pre else rendered

    # Los bloques de código van primero (una tabla dentro de ``` no se toca).
    text = _FENCE_RE.sub(lambda m: keep(f"<pre>{html.escape(m.group(1).rstrip())}</pre>"), text)
    text = _TABLE_RE.sub(table, text if text.endswith("\n") else text + "\n").rstrip("\n")

    text = _INLINE_CODE_RE.sub(lambda m: keep(f"<code>{html.escape(m.group(1))}</code>"), text)

    text = html.escape(text, quote=False)

    # Links antes que el énfasis, y guardados, para que `_` o `*` en la URL no se toquen.
    text = _LINK_RE.sub(
        lambda m: keep(f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>'), text
    )
    text = _HR_RE.sub("────────", text)
    text = _HEADER_RE.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = _BULLET_RE.sub(lambda m: f"{m.group(1)}• ", text)
    text = _BOLD_RE.sub(lambda m: f"<b>{m.group(2)}</b>", text)
    text = _STRIKE_RE.sub(lambda m: f"<s>{m.group(1)}</s>", text)
    text = _ITALIC_STAR_RE.sub(lambda m: f"<i>{m.group(1)}</i>", text)
    text = _ITALIC_UNDER_RE.sub(lambda m: f"<i>{m.group(1)}</i>", text)

    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)
