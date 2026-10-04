"""Resolves a chat's role/active-tenant/record-types into a `ToolContext`.

Shared by `function_app.process_update` (once per inbound message) and
`agent.run_agent` (to refresh mid-turn after a `switch_business` /
`switch_to_platform_admin` tool call changes `activeTenantId` underneath an
already-running turn — see `agent._CONTEXT_SWITCH_TOOLS`). Both need the exact
same resolution logic against the exact same freshly-read `chats` doc, so it
lives here instead of being duplicated or reached into across modules.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from george import roles
from george.repositories import chats as chats_repo
from george.repositories import records as records_repo
from george.repositories import tenants as tenants_repo
from george.tools.common import ToolContext

logger = logging.getLogger("george.context")


def resolve_role_and_tenant(
    chat_id: str, chat: dict[str, Any]
) -> tuple[Optional[str], Optional[str], Optional[dict[str, Any]], Optional[str]]:
    """Returns (role, tenant_id, tenant, error_message). When error_message
    is not None, the caller should send it to the chat and stop — the other
    three fields are meaningless in that case.

    A chat can belong to several tenants; this resolves which one the
    conversation is operating against right now:
      - isPlatformAdmin -> role=platform_admin, no tenant, UNLESS this chat
        also has a membership of its own and explicitly switched into it
        (activeTenantId set) — dual role, see tools/membership.py's
        switch_business / switch_to_platform_admin. Default stays platform
        mode; a platform_admin chat never auto-activates a business.
      - No memberships -> not provisioned into any business yet.
      - Exactly one membership -> auto-selected (and persisted), no
        disambiguation needed — this keeps the common case frictionless.
      - Several memberships, none active yet -> PENDING_SELECTION_ROLE; the
        agent's job for this turn is just to ask which one and call
        switch_business (see prompts.py / tools/membership.py).
    """
    if chat.get("isPlatformAdmin"):
        active_tenant_id = chat.get("activeTenantId")
        if active_tenant_id:
            active_membership = chats_repo.get_membership(chat, active_tenant_id)
            if active_membership is not None:
                tenant = tenants_repo.get_active_tenant(active_membership["tenantId"])
                if tenant is not None:
                    return active_membership["role"], active_membership["tenantId"], tenant, None
                # The switched-to tenant is gone/suspended — fall back to
                # platform mode below rather than error out a platform_admin,
                # who always has a valid identity to fall back to.
        return roles.PLATFORM_ADMIN_ROLE, None, None, None

    memberships = chat.get("memberships") or []
    if not memberships:
        return (
            None,
            None,
            None,
            "Tu chat no está asociado a ningún negocio todavía. Contacta al administrador de la plataforma.",
        )

    active_tenant_id = chat.get("activeTenantId")
    active_membership = chats_repo.get_membership(chat, active_tenant_id) if active_tenant_id else None

    if active_membership is None and len(memberships) == 1:
        active_membership = memberships[0]
        try:
            chats_repo.set_active_tenant(chat_id, active_membership["tenantId"])
        except ValueError:
            logger.exception("resolve_role_and_tenant: failed to auto-set active tenant for chat %s", chat_id)

    if active_membership is None:
        return roles.PENDING_SELECTION_ROLE, None, None, None

    tenant = tenants_repo.get_active_tenant(active_membership["tenantId"])
    if tenant is None:
        return (
            None,
            None,
            None,
            "El negocio activo de tu chat ya no está disponible (fue suspendido o eliminado). "
            "Contacta al administrador de la plataforma.",
        )

    return active_membership["role"], active_membership["tenantId"], tenant, None


def build_context(
    chat_id: str,
    user_name: str,
    correlation_id: str,
    chat: Optional[dict[str, Any]] = None,
) -> tuple[Optional[ToolContext], Optional[str]]:
    """Builds a `ToolContext` from the chat's CURRENT state. Pass `chat` when
    the caller already read it this message (avoids a redundant point-read);
    omit it to force a fresh read — which is the whole point when this is
    called to refresh an already-running agent turn after `switch_business`/
    `switch_to_platform_admin` just wrote a new `activeTenantId` underneath
    it (see `agent._CONTEXT_SWITCH_TOOLS`). Returns (None, error_message) if
    the chat is gone/unauthorized or role resolution itself errors."""
    if chat is None:
        chat = chats_repo.is_authorized(chat_id)
        if chat is None:
            return None, "Tu chat ya no está autorizado."

    role, tenant_id, tenant, error_message = resolve_role_and_tenant(chat_id, chat)
    if error_message:
        return None, error_message

    record_types = tuple(records_repo.list_record_types(tenant_id)) if tenant_id else ()
    ctx = ToolContext(
        chat_id=chat_id,
        role=role,
        user_name=user_name,
        correlation_id=correlation_id,
        tenant_id=tenant_id,
        tenant=tenant,
        is_platform_admin=bool(chat.get("isPlatformAdmin")),
        record_types=record_types,
    )
    return ctx, None
