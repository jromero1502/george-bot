"""The Haiku agent loop. Deliberately a manual `while` loop (not the SDK's
beta tool_runner): every tool_use block needs to pass through role gating
and produce an audit record, and a hand-rolled loop makes that inspection
point explicit rather than hidden inside SDK internals.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import anthropic

from george.config import settings
from george.prompts import build_system_prompt
from george.tools import registry
from george.tools.common import ToolContext

logger = logging.getLogger("george.agent")

# Safety cap — stop looping even if the model keeps requesting tools forever.
MAX_TOOL_TURNS = 8

# kind='report' reminders (function_app.py::reminder_worker) run through
# generate_report_message below with ONLY these tools available — a scheduled
# report must never be able to write anything, even though it runs with
# ctx.role='owner' to be able to read every record type.
_REPORT_TOOL_NAMES = ("list_record_types", "search_records", "summarize_records")


def _reminder_system_prompt(tenant: Optional[dict[str, Any]]) -> str:
    tenant = tenant or {}
    business_name = tenant.get("name") or "este negocio"
    business_type = tenant.get("businessType") or ""
    business_line = f"'{business_name}'" + (f" ({business_type})" if business_type else "")
    return (
        f"Eres George, el asistente del negocio {business_line}. Vas a redactar un mensaje de Telegram breve y "
        "natural en espanol, siguiendo la instruccion que recibas. "
        "No agregues saludos genericos ('Hola equipo') salvo que la instruccion lo pida explicitamente. "
        "Responde unicamente con el texto del mensaje final, sin comillas ni explicaciones sobre lo que hiciste."
    )


@dataclass
class AgentResult:
    reply_text: str
    turns: int
    input_tokens: int
    output_tokens: int
    stop_reason: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    latency_ms: float = 0.0


_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    return _client


def run_agent(
    ctx: ToolContext,
    history: list[dict[str, Any]],
    user_message: str,
    tool_names: Optional[tuple[str, ...]] = None,
    system_prompt: Optional[str] = None,
) -> AgentResult:
    """Runs the tool-use loop for one user turn: call Haiku, execute any
    tool_use blocks via the registry, feed the results back, and repeat
    until the model produces a final (`end_turn`) response or MAX_TOOL_TURNS
    is hit. `history` is prior turns as Anthropic message dicts (see
    repositories/conversations.py), oldest first.

    `tool_names` restricts the tool surface to that allowlist (see
    generate_report_message) and `system_prompt` overrides the normal
    per-tenant/per-role prompt from build_system_prompt — both default to the
    regular chat behavior when omitted.
    """
    client = _get_client()
    system_prompt = system_prompt or build_system_prompt(ctx)
    tools = registry.anthropic_tool_defs(only=tool_names)

    messages: list[dict[str, Any]] = [*history, {"role": "user", "content": user_message}]

    total_input = 0
    total_output = 0
    tool_calls: list[dict[str, Any]] = []
    turns = 0
    start = time.monotonic()
    response = None

    while turns < MAX_TOOL_TURNS:
        turns += 1
        response = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=1024,
            system=system_prompt,
            tools=tools,
            messages=messages,
        )
        total_input += response.usage.input_tokens
        total_output += response.usage.output_tokens

        if response.stop_reason != "tool_use":
            break

        messages.append({"role": "assistant", "content": response.content})

        tool_results: list[dict[str, Any]] = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            call_start = time.monotonic()
            result_text, is_error = registry.dispatch(block.name, block.input, ctx, only=tool_names)
            call_latency_ms = (time.monotonic() - call_start) * 1000
            tool_calls.append(
                {
                    "name": block.name,
                    "input": block.input,
                    "ok": not is_error,
                    "resultSummary": result_text[:500],
                    "latencyMs": round(call_latency_ms, 1),
                    "error": result_text if is_error else None,
                }
            )
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text,
                    "is_error": is_error,
                }
            )
        messages.append({"role": "user", "content": tool_results})
    else:
        # We hit the safety cap while the model still wanted to call tools —
        # `response` above is that last (unbroken-from) turn, whose text (if
        # any) was written BEFORE it saw the tool_results we just appended to
        # `messages`. Returning that text as-is risks the reply describing an
        # intended action ("voy a registrar...") or, worse, a stale success
        # claim while the actual tool result (possibly an error) never gets
        # surfaced. Force one more call with tools disabled so the final
        # reply is grounded in what the tools actually returned.
        logger.warning("agent: hit MAX_TOOL_TURNS=%d for chat %s", MAX_TOOL_TURNS, ctx.chat_id)
        response = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=1024,
            system=system_prompt,
            tool_choice={"type": "none"},
            tools=tools,
            messages=messages,
        )
        total_input += response.usage.input_tokens
        total_output += response.usage.output_tokens

    latency_ms = (time.monotonic() - start) * 1000
    reply_text = ""
    if response is not None:
        reply_text = next((b.text for b in response.content if b.type == "text"), "")
    if not reply_text:
        reply_text = (
            "Listo, hice los cambios pero no tengo un resumen para darte."
            if tool_calls
            else "No logré generar una respuesta — intenta reformular tu mensaje."
        )

    return AgentResult(
        reply_text=reply_text,
        turns=turns,
        input_tokens=total_input,
        output_tokens=total_output,
        stop_reason=response.stop_reason if response is not None else "max_turns_exceeded",
        tool_calls=tool_calls,
        latency_ms=latency_ms,
    )


def generate_reminder_message(instruction: str, tenant: Optional[dict[str, Any]] = None) -> str:
    """One-shot generation for kind='prompt' reminders — no tools, no history.
    `tenant` (the tenant document) grounds the message in that business's
    name/type instead of a generic or hardcoded one — see repositories/tenants.py."""
    client = _get_client()
    response = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=300,
        system=_reminder_system_prompt(tenant),
        messages=[{"role": "user", "content": instruction}],
    )
    text = next((b.text for b in response.content if b.type == "text"), "").strip()
    return text or instruction


def _report_system_prompt(tenant: Optional[dict[str, Any]]) -> str:
    tenant = tenant or {}
    business_name = tenant.get("name") or "este negocio"
    business_type = tenant.get("businessType") or ""
    business_line = f"'{business_name}'" + (f" ({business_type})" if business_type else "")
    return (
        f"Eres George, el asistente del negocio {business_line}. Vas a redactar un reporte breve para Telegram "
        "siguiendo la instruccion que recibas, basandote EXCLUSIVAMENTE en datos reales que obtengas llamando a "
        "list_record_types, search_records y/o summarize_records — nunca inventes numeros. Si la instruccion "
        "menciona un tipo de registro que no existe en este negocio, decilo en vez de inventar datos. "
        "Responde unicamente con el texto final del reporte, sin comillas ni explicaciones sobre lo que hiciste."
    )


def generate_report_message(instruction: str, ctx: ToolContext) -> AgentResult:
    """Tool-use generation for kind='report' reminders (function_app.py::
    reminder_worker). Runs the same loop as run_agent but restricted to
    _REPORT_TOOL_NAMES — a scheduled report must never be able to write
    anything, even though `ctx.role` here is 'owner' so it can read every
    record type. Returns the full AgentResult (not just the text) so the
    caller can audit it in `conversations` like any other turn."""
    return run_agent(
        ctx, [], instruction, tool_names=_REPORT_TOOL_NAMES, system_prompt=_report_system_prompt(ctx.tenant)
    )
