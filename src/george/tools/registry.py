"""Aggregates every tool module into one registry: the list of tool
definitions sent to the Anthropic API, and the dispatcher the agent loop
calls to execute a `tool_use` block.

Role gating is enforced twice, deliberately: each handler calls
`require_role` itself (so a handler is never accidentally reachable from
code that forgets the check), and the registry's `allowed_roles` doubles as
documentation / a single place to audit who can do what.
"""
from __future__ import annotations

from typing import Any, Optional

from george.tools import chats, clients, finance, membership, pqrs, records, reminders, tenants, utility
from george.tools.common import ToolContext, ToolError, ToolSpec

_MODULES = (utility, clients, finance, records, reminders, pqrs, chats, tenants, membership)

ALL_TOOLS: list[ToolSpec] = [tool for module in _MODULES for tool in module.TOOLS]
_BY_NAME: dict[str, ToolSpec] = {tool.name: tool for tool in ALL_TOOLS}

assert len(_BY_NAME) == len(ALL_TOOLS), "Duplicate tool name across tool modules"


def anthropic_tool_defs(only: Optional[tuple[str, ...]] = None) -> list[dict[str, Any]]:
    """Tool definitions in the shape the Anthropic Messages API expects. `only`
    restricts the surface to the named tools — used for scheduled reports
    (see agent.generate_report_message), which must not be able to write."""
    tools = ALL_TOOLS if only is None else [tool for tool in ALL_TOOLS if tool.name in only]
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
            "strict": tool.strict,
        }
        for tool in tools
    ]


def dispatch(
    name: str, tool_input: dict[str, Any], ctx: ToolContext, only: Optional[tuple[str, ...]] = None
) -> tuple[str, bool]:
    """Executes a tool call. Returns (result_text, is_error) — never raises,
    so the agent loop can always produce a tool_result block. `only`, when
    given, rejects any tool not in the allowlist even if it exists in the
    registry — the enforcement half of anthropic_tool_defs(only=...)."""
    if only is not None and name not in only:
        return f"Herramienta '{name}' no disponible en este contexto.", True
    tool = _BY_NAME.get(name)
    if tool is None:
        return f"Herramienta desconocida: {name!r}", True
    try:
        return tool.handler(tool_input, ctx), False
    except ToolError as exc:
        return str(exc), True
    except Exception as exc:  # noqa: BLE001 — must never crash the agent loop
        return f"Error interno ejecutando '{name}': {exc}", True
