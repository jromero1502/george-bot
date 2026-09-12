"""Role constants shared across repositories, tools, prompts and
function_app.py. Kept dependency-free (no imports from george.*) so every
layer can import it without risking a circular import.
"""
from __future__ import annotations

# Assignable within a tenant membership (chats.memberships[].role).
BUSINESS_ROLES = ("owner", "admin", "walker", "viewer")

# Not a membership — a flag on the chat doc (chats.isPlatformAdmin). Manages
# the platform itself (creates tenants, associates chats to tenants) and
# belongs to no business.
PLATFORM_ADMIN_ROLE = "platform_admin"

# Transient: a chat with more than one membership and no activeTenantId yet.
# Only list_my_businesses / switch_business are available in this state —
# see tools/membership.py and prompts.py.
PENDING_SELECTION_ROLE = "pending_business_selection"
