"""The system's own record of which tools actually ran in a past turn, shown to
the model inside the conversation history.

Why it exists: `function_app._history_to_messages` replays prior turns as plain
text, so a turn that really called `create_expense` seven times looked
identical to one where the model only SAID it had. Production showed both
directions of that blindness (2026-10-04, 2026-10-26): George confirmed "anotado"
with no tool call and then, next turn, re-did work that was already saved —
seven duplicate expenses created two minutes apart, after which it blamed
"another team member". The model can't tell "I did it" from "I said I did it"
unless the history tells it.

The line is appended to the assistant turn ONLY when the history is built, from
the stored `toolCalls`; it is never stored in `output.text`. `strip_tool_log`
removes it from anything the model writes, in case it imitates the format.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

TOOL_LOG_PREFIX = "[Registro del sistema"

# Moved here from agent.py so the history builder and the agent loop share one
# definition (agent.py re-imports it under its old private name).
CONFIRMATION_RE = re.compile(
    r"(anot[ée]|anotad[oa]s?|registr[ée]|registrad[oa]s?|guard[ée]|guardad[oa]s?|carg[uqé]|cargad[oa]s?|✅|✓)",
    re.IGNORECASE,
)

_MAX_CALLS_LISTED = 15
_MAX_INPUT_CHARS = 140
_MAX_ERROR_CHARS = 100


def format_tool_log(tool_calls: Optional[list[dict[str, Any]]], assistant_text: str = "") -> str:
    """The trailing system line for one assistant turn, or "" when there is
    nothing worth saying (no tools and no confirmation-sounding text)."""
    if not tool_calls:
        if assistant_text and CONFIRMATION_RE.search(assistant_text):
            return (
                f"\n\n{TOOL_LOG_PREFIX}: en este turno NO se ejecutó ninguna herramienta, así que nada de lo que "
                "dice el mensaje anterior quedó guardado.]"
            )
        return ""

    lines = []
    for call in tool_calls[:_MAX_CALLS_LISTED]:
        args = json.dumps(call.get("input") or {}, ensure_ascii=False)
        if len(args) > _MAX_INPUT_CHARS:
            args = args[: _MAX_INPUT_CHARS - 1] + "…"
        if call.get("ok", True):
            outcome = "ok"
        else:
            outcome = "ERROR: " + (call.get("error") or "")[:_MAX_ERROR_CHARS].replace("\n", " ")
        lines.append(f"- {call.get('name')} {args} → {outcome}")
    extra = len(tool_calls) - _MAX_CALLS_LISTED
    if extra > 0:
        lines.append(f"- … y {extra} llamadas más")
    return (
        f"\n\n{TOOL_LOG_PREFIX} — herramientas ejecutadas en este turno (generado por el sistema, no lo escribas):\n"
        + "\n".join(lines)
        + "]"
    )


def strip_tool_log(text: str) -> str:
    """Drops a system tool-log line the model wrote itself (mimicking the
    history format) so it never reaches the user. Falls back to the original
    text if stripping would leave nothing."""
    if TOOL_LOG_PREFIX not in text:
        return text
    cleaned = text.split(TOOL_LOG_PREFIX, 1)[0].rstrip()
    return cleaned or text
