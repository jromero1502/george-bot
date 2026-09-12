"""Shared types for the tools package."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional


class ToolError(Exception):
    """Raised by a tool handler for an expected, user-facing failure (bad
    input, not found, not authorized). The agent loop turns this into an
    `is_error: true` tool_result instead of crashing the whole turn."""


@dataclass(frozen=True)
class ToolContext:
    """Everything a tool handler needs to know about who is calling it.

    tenant_id is None only for role='platform_admin' — every other role
    belongs to exactly one tenant (business), and every business-data
    handler must pass it as the Cosmos partition key rather than trusting
    anything the model puts in the tool call's input."""

    chat_id: str
    role: str
    user_name: str
    correlation_id: str
    tenant_id: Optional[str] = None
    # Full tenant document (currency, businessType, itemLabel, ...) — fetched
    # once per message in function_app.py rather than re-read by every tool
    # that needs a tenant-level default (e.g. finance's currency fallback).
    tenant: Optional[dict[str, Any]] = None
    # Whether the underlying CHAT doc has isPlatformAdmin=true — independent
    # of `role`, which reflects the mode active THIS turn. A platform_admin
    # chat that switched into one of its own businesses has role='owner' (or
    # whatever) here, but is_platform_admin stays True so switch_to_platform_admin
    # can find its way back (see tools/membership.py).
    is_platform_admin: bool = False


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    allowed_roles: tuple[str, ...]
    handler: Callable[[dict[str, Any], ToolContext], str]
    # Anthropic caps combined optional parameters across all strict tools in a
    # single request at 24 ("grammar compilation" limit) — we have ~47 across
    # 16 tools, well over it. Runtime validation (require_role + ToolError,
    # defensive .get() in every handler) already covers correctness, so strict
    # mode isn't worth fragmenting the tool surface over. Off by default.
    strict: bool = False


def require_role(ctx: ToolContext, allowed_roles: tuple[str, ...], tool_name: str) -> None:
    if ctx.role not in allowed_roles:
        raise ToolError(
            f"Tu rol ({ctx.role}) no tiene permiso para usar '{tool_name}'. "
            f"Roles permitidos: {', '.join(allowed_roles)}."
        )


def require_tenant(ctx: ToolContext) -> str:
    """Every tenant-scoped tool calls this to get the partition key for its
    Cosmos queries. Raising here (rather than letting a None propagate into
    a query) turns a role-gating bug into a loud error instead of a query
    that silently scans nothing or, worse, everything."""
    if ctx.tenant_id is None:
        raise ToolError(
            "Tu chat no está asociado a ningún negocio todavía — pedile al administrador "
            "de la plataforma que te registre en un tenant."
        )
    return ctx.tenant_id
