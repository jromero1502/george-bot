"""`chats` container — the whitelist of Telegram chats allowed to talk to
George, plus their tenant memberships. Partition key: /chatId. id == chatId.

A chat can belong to *several* tenants (businesses) at once, each with its
own role — `memberships: [{tenantId, role}, ...]`. `activeTenantId` is which
one the conversation is currently operating against; when it's unset and
there's more than one membership, function_app.py puts the chat into the
`PENDING_SELECTION_ROLE` state until it calls switch_business.

`isPlatformAdmin` is a separate flag, not a membership — a platform admin
manages tenants themselves and belongs to no business (see george.roles).
"""
from __future__ import annotations

from typing import Any, Optional

from azure.cosmos import exceptions

from george.repositories.cosmos import get_container
from george.roles import BUSINESS_ROLES
from george.util import utc_now_iso

__all__ = ["BUSINESS_ROLES", "get_chat", "is_authorized", "get_membership", "list_chats_by_role",
           "set_platform_admin", "add_membership", "set_active_tenant", "clear_active_tenant"]


def _container():
    return get_container("chats")


def _new_chat_skeleton(chat_id: str) -> dict[str, Any]:
    return {
        "id": chat_id,
        "chatId": chat_id,
        "isPlatformAdmin": False,
        "memberships": [],
        "activeTenantId": None,
        "createdAt": utc_now_iso(),
        "custom": {},
    }


def get_chat(chat_id: str) -> Optional[dict[str, Any]]:
    try:
        return _container().read_item(item=chat_id, partition_key=chat_id)
    except exceptions.CosmosResourceNotFoundError:
        return None


def is_authorized(chat_id: str) -> Optional[dict[str, Any]]:
    """Returns the chat document if it's whitelisted and active, else None."""
    chat = get_chat(chat_id)
    if chat is None or chat.get("status") != "active":
        return None
    return chat


def get_membership(chat: dict[str, Any], tenant_id: str) -> Optional[dict[str, Any]]:
    return next((m for m in (chat.get("memberships") or []) if m.get("tenantId") == tenant_id), None)


def list_chats_by_role(tenant_id: str, role: str) -> list[dict[str, Any]]:
    """Active chats with the given role *in this tenant* — used to resolve
    reminder targets of type 'role'. A chat's memberships in other tenants
    don't matter here."""
    query = """
    SELECT * FROM c
    WHERE c.status = 'active'
      AND EXISTS(SELECT VALUE m FROM m IN c.memberships WHERE m.tenantId = @tenantId AND m.role = @role)
    """
    return list(
        _container().query_items(
            query=query,
            parameters=[
                {"name": "@tenantId", "value": tenant_id},
                {"name": "@role", "value": role},
            ],
            enable_cross_partition_query=True,
        )
    )


def set_platform_admin(chat_id: str, name: Optional[str] = None, **fields: Any) -> dict[str, Any]:
    """Seeds or updates a platform_admin chat. Platform admins carry no
    tenant memberships."""
    chat = get_chat(chat_id) or _new_chat_skeleton(chat_id)
    chat["isPlatformAdmin"] = True
    if name is not None:
        chat["name"] = name
    chat.setdefault("status", "active")
    chat.update({k: v for k, v in fields.items() if v is not None})
    chat["updatedAt"] = utc_now_iso()
    return _container().upsert_item(chat)


def add_membership(
    chat_id: str,
    tenant_id: str,
    role: str,
    name: Optional[str] = None,
    set_active: Optional[bool] = None,
    **fields: Any,
) -> dict[str, Any]:
    """Creates the chat if it doesn't exist yet, and adds (or updates the
    role of) a membership in `tenant_id`. Existing memberships in *other*
    tenants are left untouched — a chat can belong to several businesses.

    If this is the chat's first membership (or `set_active` is truthy), it
    also becomes the active tenant, so the common one-business case needs no
    extra disambiguation step. Exception: a platform_admin chat never
    auto-activates a membership just because it has none yet — its default
    stays platform mode; it only switches into a business via an explicit
    set_active=True (or switch_business afterward)."""
    if role not in BUSINESS_ROLES:
        raise ValueError(f"Rol invalido: {role!r}. Debe ser uno de {', '.join(BUSINESS_ROLES)}.")

    chat = get_chat(chat_id) or _new_chat_skeleton(chat_id)
    memberships = chat.setdefault("memberships", [])
    existing = next((m for m in memberships if m.get("tenantId") == tenant_id), None)
    if existing is not None:
        existing["role"] = role
    else:
        memberships.append({"tenantId": tenant_id, "role": role})

    if name is not None:
        chat["name"] = name
    chat.setdefault("status", "active")
    chat.update({k: v for k, v in fields.items() if v is not None})

    if set_active or (not chat.get("isPlatformAdmin") and chat.get("activeTenantId") is None):
        chat["activeTenantId"] = tenant_id

    chat["updatedAt"] = utc_now_iso()
    return _container().upsert_item(chat)


def clear_active_tenant(chat_id: str) -> dict[str, Any]:
    """Switches a platform_admin-and-also-business-member chat back to
    platform mode — clears activeTenantId without touching its memberships.
    See tools/membership.py's switch_to_platform_admin."""
    chat = get_chat(chat_id)
    if chat is None:
        raise ValueError(f"Chat desconocido: {chat_id!r}")
    chat["activeTenantId"] = None
    chat["updatedAt"] = utc_now_iso()
    return _container().upsert_item(chat)


def set_active_tenant(chat_id: str, tenant_id: str) -> dict[str, Any]:
    """Switches which of the chat's existing memberships the conversation is
    currently operating against. Raises ValueError if the chat has no
    membership in that tenant — you can't switch into a business you don't
    belong to."""
    chat = get_chat(chat_id)
    if chat is None:
        raise ValueError(f"Chat desconocido: {chat_id!r}")
    if get_membership(chat, tenant_id) is None:
        raise ValueError("Ese chat no pertenece a ese negocio.")
    chat["activeTenantId"] = tenant_id
    chat["updatedAt"] = utc_now_iso()
    return _container().upsert_item(chat)
